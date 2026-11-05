"""Eligibility checks: every reason an output must pass through unchanged."""

from __future__ import annotations

import os
import re
from typing import Optional

from fovea import shell
from fovea.config import Config

HARNESS_STDERR_PREFIX = "Shell cwd was reset to "

# A failing command reports at the end of its output, so only the tail is checked,
# and only for the shape of an error message, not the bare word.
ERROR_TAIL_LINES = 30
ERROR_PATTERNS = re.compile(
    r"Traceback \(most recent call last\)"
    r"|^\s*(?:error|Error|ERROR)(?:\[[^\]\n]*\])?: "
    r"|^\s*ERROR\b"
    r"|^FAILED\b|\bFAILED$|^FAIL\b"
    r"|panicked at"
    r"|^fatal: "
    r"|^\S*(?:Exception|Error): ",
    re.MULTILINE,
)


def has_error_output(stdout: str) -> bool:
    tail = "\n".join(stdout.splitlines()[-ERROR_TAIL_LINES:])
    return bool(ERROR_PATTERNS.search(tail))


SECRET_OUTPUT = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|github_pat_"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[abp]-"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)

SECRET_WORDS = {"op", "security", "printenv"}
SECRET_PATH_PARTS = (".env", ".ssh/", ".pem", "id_rsa", "id_ed25519")
GH_AUTH_TOKEN = re.compile(r"\bgh\s+auth\s+token\b")
SED_RANGE = re.compile(r"\bsed\b[^|;&\n]*?\s(?:-[A-Za-z]*n|--quiet\b|--silent\b)")
PIPE_HEAD_TAIL = re.compile(r"\|&?\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:\S*/)?(?:head|tail)\b")


def stderr_is_empty(stderr: str) -> bool:
    kept = [ln for ln in stderr.split("\n") if not ln.startswith(HARNESS_STDERR_PREFIX)]
    return not "\n".join(kept).strip()


def is_recovery_read(command: str, cfg: Config) -> bool:
    if "FOVEA_HOME" in command or "~/.fovea" in command or ".fovea" in command:
        return True
    home = cfg.home
    if home and home in command:
        return True
    user_home = os.path.expanduser("~")
    if home.startswith(user_home + os.sep):
        tilde = "~" + home[len(user_home):]
        if tilde in command or ("$HOME" + home[len(user_home):]) in command:
            return True
    return False


# A saved output's file name: 8 hex digits of sha1(tool_use_id) + ".txt" (see store.out_name).
SAVED_OUTPUT_ID = re.compile(r"(?<![0-9A-Za-z])([0-9a-f]{8})\.txt\b")


def recovered_id(command: str, cfg: Config) -> Optional[str]:
    """The id of the saved output a recovery read refers to (the first one), or None."""
    if not is_recovery_read(command, cfg):
        return None
    m = SAVED_OUTPUT_ID.search(command)
    return m.group(1) if m else None


def _bare_env(command: str) -> bool:
    for seg in shell.segments(command):
        for stage in shell.pipeline_stages(seg):
            ws = shell.strip_prefix(shell.words(stage))
            if ws and os.path.basename(ws[0]) == "env" and all(w.startswith("-") for w in ws[1:]):
                return True
    return False


def is_secret_command(command: str) -> bool:
    ws = shell.words(command)
    for w in ws:
        if os.path.basename(w) in SECRET_WORDS:
            return True
    if GH_AUTH_TOKEN.search(command):
        return True
    if any(part in command for part in SECRET_PATH_PARTS):
        return True
    if "keychain" in command.lower():
        return True
    return _bare_env(command)


def range_chosen(command: str) -> bool:
    if SED_RANGE.search(command) or PIPE_HEAD_TAIL.search(command):
        return True
    name, _ = shell.first_command(command)
    return name in ("head", "tail")


def check(payload: dict, cfg: Config) -> Optional[str]:
    """Return the passthrough reason, or None when the output is eligible."""
    if payload.get("tool_name") != "Bash":
        return "not_bash"
    if not cfg.mode_valid:
        return "invalid_mode"
    if cfg.mode == "off":
        return "mode_off"
    if not cfg.api_key:
        return "no_api_key"
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        command = ""
    # Checked before the size: a recovery is usually a short range of a saved output,
    # and it must be logged as a recovery to count as a signal that the selection fell short.
    if is_recovery_read(command, cfg):
        return "recovery_read"
    response = payload.get("tool_response")
    if not isinstance(response, dict) or not isinstance(response.get("stdout"), str):
        return "unexpected_shape"
    stdout = response["stdout"]
    if len(stdout) < cfg.min_chars:
        return "small_output"
    if len(stdout.encode("utf-8", "replace")) >= cfg.harness_limit:  # the cut is in UTF-8 bytes
        # Claude Code already cut this output and saved the full text itself; it shows the model
        # only a 2 KB preview of whatever we return, so a selection would be cut off.
        return "harness_persisted"
    if response.get("interrupted") is True:
        return "interrupted"
    if response.get("isImage") is True:
        return "image"
    stderr = response.get("stderr")
    if stderr is not None and not isinstance(stderr, str):
        return "unexpected_shape"
    if stderr and not stderr_is_empty(stderr):
        return "stderr"
    if has_error_output(stdout):
        return "error_output"
    if "FOVEA_BYPASS=1" in command:
        return "bypass"
    if is_secret_command(command):
        return "secret_command"
    if SECRET_OUTPUT.search(stdout):
        return "secret_output"
    if range_chosen(command):
        return "range_chosen"
    return None

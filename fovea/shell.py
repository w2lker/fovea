"""Light shell-command parsing: words, segments and the first real command word."""

from __future__ import annotations

import os
import re
import shlex
from typing import List, Tuple

_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;\n]|(?<![|>&])&(?![&>])")
_PIPE_SPLIT = re.compile(r"\|&?")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_FALLBACK_WORD = re.compile(r"[^\s;&|()<>`]+")

# Prefix words that do not change what the command prints.
_PREFIX_WORDS = {"sudo", "time", "command", "nohup", "exec", "builtin"}


def words(text: str) -> List[str]:
    """Split text into shell words; tolerate unbalanced quotes."""
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return [w.strip("'\"") for w in _FALLBACK_WORD.findall(text)]


def segments(command: str) -> List[str]:
    """Split a command line into list segments (&&, ||, ;, newline, &)."""
    return [s for s in (p.strip() for p in _SEGMENT_SPLIT.split(command)) if s]


def pipeline_stages(segment: str) -> List[str]:
    return [s.strip() for s in _PIPE_SPLIT.split(segment) if s.strip()]


def strip_prefix(ws: List[str]) -> List[str]:
    """Drop env assignments and prefix words such as sudo (with its flags)."""
    i = 0
    while i < len(ws):
        w = ws[i]
        if _ASSIGNMENT.match(w):
            i += 1
        elif w in _PREFIX_WORDS:
            i += 1
            while i < len(ws) and ws[i].startswith("-"):
                i += 1
        else:
            break
    return ws[i:]


def first_command(command: str) -> Tuple[str, List[str]]:
    """Return (basename of the first real command word, its argument words).

    Leading `cd ... &&` segments, env assignments and `sudo` are skipped; only the
    first pipeline stage of the first real segment is considered.
    """
    for seg in segments(command):
        stage = pipeline_stages(seg)
        if not stage:
            continue
        ws = strip_prefix(words(stage[0]))
        if not ws:
            continue
        name = os.path.basename(ws[0])
        if name in ("cd", "pushd", "popd"):
            continue
        return name, ws[1:]
    return "", []


def git_subcommand(args: List[str]) -> str:
    """Skip git global options and return the subcommand."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return a
    return ""

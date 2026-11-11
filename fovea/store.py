"""Saved full outputs, the daily cleanup, and the JSONL decision log."""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Optional

CLEANUP_MARKER = ".last-cleanup"
DAY = 86400


def _ensure_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _write_private(path: str, data: bytes, append: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)


def out_name(tool_use_id: str, text: str) -> str:
    seed = tool_use_id if tool_use_id else "content:" + text
    return hashlib.sha1(seed.encode("utf-8", "surrogatepass")).hexdigest()[:8] + ".txt"


def out_id(path: str) -> str:
    """The id shown in the header: the saved file's name without `.txt`."""
    name = os.path.basename(path)
    return name[:-4] if name.endswith(".txt") else name


def save_output(home: str, tool_use_id: str, text: str) -> str:
    _ensure_dir(home)
    out_dir = os.path.join(home, "out")
    _ensure_dir(out_dir)
    path = os.path.join(out_dir, out_name(tool_use_id, text))
    _write_private(path, text.encode("utf-8", "surrogatepass"))
    return path


def display_path(path: str) -> str:
    user_home = os.path.expanduser("~")
    if user_home and user_home != "/" and path.startswith(user_home + os.sep):
        return "~" + path[len(user_home):]
    return path


def cleanup(home: str, retention_days: float, max_store_mb: float, now: Optional[float] = None,
            keep: Optional[str] = None) -> None:
    """At most once a day: drop files past retention, then the oldest until under the cap."""
    now = time.time() if now is None else now
    marker = os.path.join(home, CLEANUP_MARKER)
    try:
        if now - os.path.getmtime(marker) < DAY:
            return
    except OSError:
        pass
    _ensure_dir(home)
    _write_private(marker, b"")
    os.utime(marker, (now, now))
    out_dir = os.path.join(home, "out")
    try:
        names = os.listdir(out_dir)
    except OSError:
        return
    files = []
    for name in names:
        path = os.path.join(out_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not os.path.isfile(path):
            continue
        if now - st.st_mtime > retention_days * DAY:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        files.append((st.st_mtime, st.st_size, path))
    files.sort()
    total = sum(size for _, size, _ in files)
    limit = max_store_mb * 1024 * 1024
    for _, size, path in files:
        if total < limit:
            break
        if keep and path == keep:
            continue  # the output the agent was just pointed to
        try:
            os.remove(path)
            total -= size
        except OSError:
            pass


LOG_FIELDS = (
    "ts", "session_id", "tool_use_id", "cwd", "command", "task", "mode", "version", "config",
    "action", "reason",
    "splitter", "n_blocks", "block_lines", "scores", "kept", "chars_total", "chars_kept",
    "latency_ms", "cost", "out_path", "out_id", "transcript_path", "recovered_id", "error",
    "persisted", "persisted_bytes", "persisted_unreadable", "budget_dropped",
)


def append_private(home: str, path: str, data: bytes) -> None:
    """Append to a private file under the Fovea home, creating the home if needed."""
    _ensure_dir(home)
    _ensure_dir(os.path.dirname(path))
    _write_private(path, data, append=True)


def write_log(home: str, record: dict) -> None:
    """Append one decision line. Logging never raises."""
    try:
        from datetime import datetime

        now = datetime.now().astimezone()
        line = {field: record.get(field) for field in LOG_FIELDS}
        line["ts"] = now.isoformat(timespec="milliseconds")
        log_dir = os.path.join(home, "log")
        _ensure_dir(home)
        _ensure_dir(log_dir)
        path = os.path.join(log_dir, now.strftime("%Y-%m-%d") + ".jsonl")
        data = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8", "surrogatepass")
        _write_private(path, data, append=True)
    except Exception:
        pass

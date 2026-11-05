"""Build the task text from the call and the tail of the transcript."""

from __future__ import annotations

import json
import os
from typing import Optional, Tuple

COMMAND_LIMIT = 300
ASSISTANT_LIMIT = 600
USER_LIMIT = 600
TAIL_STEPS = (256 * 1024, 1024 * 1024, 4 * 1024 * 1024)
NOT_REAL_PREFIXES = (
    "<system-reminder>",
    "<task-notification>",
    "<local-command",
    "<command-",
)


def _assistant_text(record: dict) -> Optional[str]:
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    for item in reversed(content):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def _user_text(record: dict) -> Optional[str]:
    if record.get("isMeta") is True:
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_result":
                return None
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        text = "\n".join(parts)
    else:
        return None
    text = text.strip()
    if not text or text.startswith(NOT_REAL_PREFIXES):
        return None
    return text


def _scan(path: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (latest assistant text, latest real user message) from the file tail."""
    size = os.path.getsize(path)
    assistant = user = None
    with open(path, "rb") as fh:
        for step in TAIL_STEPS:
            start = max(0, size - step)
            fh.seek(start)
            data = fh.read(size - start)
            lines = data.split(b"\n")
            if start > 0:
                lines = lines[1:]  # first line is partial
            assistant = user = None
            for raw in reversed(lines):
                if assistant is not None and user is not None:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                kind = record.get("type")
                if kind == "assistant" and assistant is None:
                    assistant = _assistant_text(record)
                elif kind == "user" and user is None:
                    user = _user_text(record)
            if (assistant is not None and user is not None) or start == 0:
                break
    return assistant, user


def _resolve(transcript_path, cwd) -> Optional[str]:
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    path = os.path.expanduser(transcript_path)
    if os.path.isfile(path):
        return path
    if not os.path.isabs(path) and isinstance(cwd, str) and cwd:
        joined = os.path.join(cwd, path)
        if os.path.isfile(joined):
            return joined
    return None


def build(payload: dict) -> str:
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    description = tool_input.get("description")
    command = tool_input.get("command")
    parts = []
    if isinstance(description, str) and description.strip():
        parts.append(description.strip())
    if isinstance(command, str) and command.strip():
        parts.append(command.strip()[:COMMAND_LIMIT].strip())
    assistant = user = None
    try:
        path = _resolve(payload.get("transcript_path"), payload.get("cwd"))
        if path:
            assistant, user = _scan(path)
    except OSError:
        assistant = user = None
    if assistant:
        parts.append(assistant[:ASSISTANT_LIMIT].strip())
    if user:
        parts.append(user[:USER_LIMIT].strip())
    return "\n".join(parts)

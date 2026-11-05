#!/usr/bin/env python3
"""Fovea SessionStart hook: tells the agent once per session how to read and report filtered outputs.

Prints the hook answer unless FOVEA_MODE is off. Does not read stdin. Always exits 0.
"""

from __future__ import annotations

import json
import os
import sys

if __package__ in (None, ""):
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path and os.path.abspath(sys.path[0] or ".") == os.path.dirname(os.path.abspath(__file__)):
        sys.path[0] = _ROOT  # never let package modules shadow the standard library
    elif _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

from fovea import config  # noqa: E402

CONTEXT = (
    "Bash outputs marked `[fovea <id>: …]` are filtered to the blocks relevant to your task; "
    "`[skip a-b]` lines are omitted ranges of the full file shown in the header. If the filtering "
    "hid something you needed or misled you, run `fovea note <id> \"<what went wrong>\"` — only when "
    "it really went wrong."
)


def answer(env=None):
    """The hook answer as a dict, or None when Fovea is off."""
    if config.load(env).mode == "off":
        return None
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": CONTEXT}}


def main() -> int:
    try:
        data = answer()
        if data is not None:
            text = json.dumps(data)  # ASCII-only
            stream = getattr(sys.stdout, "buffer", None)
            if stream is not None:
                stream.write(text.encode("ascii"))
            else:
                sys.stdout.write(text)
            sys.stdout.flush()
    except BaseException:
        pass
    return 0


if __name__ == "__main__":
    main()
    sys.exit(0)

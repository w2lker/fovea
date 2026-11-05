"""Render what the agent sees: kept ranges verbatim, dropped runs as one skip line."""

from __future__ import annotations

import re
import shlex
from typing import List, Set

from fovea.split import Block, Split

HINT_CHARS = 24
HINT_BLOCKS = 3


def _days(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else ("%g" % value)


# Header lines that say nothing about a block's content (git log metadata).
_NOT_A_HINT = re.compile(r"^(commit [0-9a-f]{7,}|Author: |Date: |Merge: )")


def _hint(sp: Split, block: Block) -> str:
    for line in sp.lines[block.start:block.end]:
        text = line.strip()
        if text and not _NOT_A_HINT.match(text):
            return text if len(text) <= HINT_CHARS else text[:HINT_CHARS] + "…"
    return ""


def _skip_line(sp: Split, run: List[Block]) -> str:
    first, last = run[0].first_line, run[-1].last_line
    hints = [h for h in (_hint(sp, b) for b in run[:HINT_BLOCKS]) if h]
    if len(run) > HINT_BLOCKS:
        hints.append("+%d" % (len(run) - HINT_BLOCKS))
    if not hints:
        return "[skip %d-%d]" % (first, last)
    return "[skip %d-%d: %s]" % (first, last, "; ".join(hints))


def _cat_target(display: str, absolute: str) -> str:
    if shlex.quote(display) == display:
        return display
    if display.startswith("~/"):
        rest = display[2:]
        if shlex.quote(rest) == rest:
            return display
        return "~/" + shlex.quote(rest)
    return shlex.quote(absolute)


def render(sp: Split, labels: List[str], kept: Set[str], chars_total: int, chars_kept: int,
           out_id: str, display: str, absolute: str, retention_days: float) -> str:
    pct = int(round(100.0 * chars_kept / chars_total)) if chars_total else 0
    out = [
        "[fovea %s: %d/%d blocks, %d%% · full: cat %s · kept %sd]"
        % (out_id, len(kept), len(labels), pct, _cat_target(display, absolute),
           _days(retention_days))
    ]
    i = 0
    n = len(sp.blocks)
    while i < n:
        keep = labels[i] in kept
        j = i
        while j + 1 < n and (labels[j + 1] in kept) == keep:
            j += 1
        run = sp.blocks[i:j + 1]
        if keep:
            out.append("[%d-%d]" % (run[0].first_line, run[-1].last_line))
            out.extend(sp.lines[run[0].start:run[-1].end])
        else:
            out.append(_skip_line(sp, run))
        i = j + 1
    return "\n".join(out)

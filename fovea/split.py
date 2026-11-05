"""Split a Bash output into blocks.

Every splitter works on one list of lines (`text.split("\\n")` of the text that is
saved to disk) and returns block start indices, so blocks are contiguous line spans
that cover the whole output exactly once.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from fovea import shell

MIN_BLOCK_CHARS = 200
MAX_BLOCK_CHARS = 2500
WINDOW_LINES = 40
GREP_GROUP = 20

DIFF_START = re.compile(r"^diff --git ")
HUNK_START = re.compile(r"^@@")
COMMIT_START = re.compile(r"^commit [0-9a-f]{7,}")
MD_HEADING = re.compile(r"^#{1,3} ")
FENCE = re.compile(r"^\s*(```|~~~)")
PATH_LINE = re.compile(r"^(?P<path>[^\s:]+):(?P<line>\d+)[:-]")
GREP_PATH = re.compile(r"^(?P<path>[^\s:]+)[:-]")
TREE_TOP = re.compile(r"^[├└]── ")
CODE_DECL = re.compile(
    r"^(?:(?:export|default|pub(?:\([^)]*\))?|async|static|public|private|protected|"
    r"internal|abstract|final|sealed|open|override|unsafe|extern|inline|virtual)\s+)*"
    r"(?:def|class|function|func|fn|struct|enum|impl|trait|interface|type|const|let|var|"
    r"module|object|fun|val|record|namespace|template|union|typedef|macro_rules!)\b"
)
CODE_PREAMBLE = re.compile(r"^(?:@|#\[|#!|//|/\*|\*|#(?!include|define|if|else|endif|pragma))")

MARKDOWN_EXT = {".md", ".markdown", ".mdx"}
CODE_EXT = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java",
    ".kt", ".kts", ".scala", ".swift", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".cs", ".m", ".mm", ".lua", ".sh", ".bash", ".zsh", ".ex", ".exs", ".dart", ".vue",
    ".svelte", ".sql", ".r", ".jl", ".hs", ".ml", ".clj", ".groovy", ".pl", ".pm",
}


@dataclass
class Block:
    start: int  # 0-based, inclusive
    end: int  # 0-based, exclusive

    @property
    def first_line(self) -> int:
        return self.start + 1

    @property
    def last_line(self) -> int:
        return self.end


@dataclass
class Split:
    text: str  # the text that is saved and that line numbers refer to
    lines: List[str]
    blocks: List[Block]
    splitter: str

    def block_text(self, block: Block) -> str:
        return "\n".join(self.lines[block.start:block.end])

    def block_chars(self, block: Block) -> int:
        return len(self.block_text(block))


def to_lines(text: str) -> List[str]:
    lines = text.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()  # a trailing newline does not start a new line
    return lines


def _span_chars(lines: List[str], start: int, end: int) -> int:
    return sum(len(lines[i]) for i in range(start, end)) + max(0, end - start - 1)


def _blocks_from_starts(starts: List[int], n: int) -> List[Block]:
    clean = sorted({s for s in starts if 0 < s < n})
    edges = [0] + clean + [n]
    return [Block(a, b) for a, b in zip(edges, edges[1:]) if b > a]


# ---- boundary finders -------------------------------------------------------

def _starts_matching(lines: List[str], pattern, lo: int = 0, hi: Optional[int] = None) -> List[int]:
    hi = len(lines) if hi is None else hi
    return [i for i in range(lo, hi) if pattern.match(lines[i])]


def _paragraph_starts(lines: List[str]) -> List[int]:
    return [i for i in range(1, len(lines)) if lines[i].strip() and not lines[i - 1].strip()]


def _window_starts(lines: List[str]) -> List[int]:
    return list(range(WINDOW_LINES, len(lines), WINDOW_LINES))


def _diff_starts(lines: List[str]) -> List[int]:
    files = _starts_matching(lines, DIFF_START)
    starts = list(files)
    edges = files + [len(lines)]
    for a, b in zip(edges, edges[1:]):
        if _span_chars(lines, a, b) > MAX_BLOCK_CHARS:
            hunks = _starts_matching(lines, HUNK_START, a, b)
            starts.extend(hunks[1:])  # the file header stays with the first hunk
    return starts


def _grep_starts(lines: List[str], loose: bool) -> List[int]:
    starts = []
    current = None
    matches = 0
    for i, line in enumerate(lines):
        m = PATH_LINE.match(line) or (GREP_PATH.match(line) if loose else None)
        if not m:
            continue  # separators and other lines stay with the current group
        path = m.group("path")
        if path != current:
            current = path
            matches = 0
            starts.append(i)
        elif matches and matches % GREP_GROUP == 0:
            starts.append(i)
        matches += 1
    return starts


def _find_starts(lines: List[str]) -> List[int]:
    starts = []
    prev = None
    for i, line in enumerate(lines):
        d = os.path.dirname(line.strip().rstrip("/"))
        if d != prev:
            starts.append(i)
            prev = d
    return starts


def _markdown_starts(lines: List[str]) -> List[int]:
    starts = []
    in_fence = False
    for i, line in enumerate(lines):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and MD_HEADING.match(line):
            starts.append(i)
    return starts


def _code_starts(lines: List[str]) -> List[int]:
    starts = []
    for i, line in enumerate(lines):
        if not CODE_DECL.match(line):
            continue
        j = i
        while j > 0 and lines[j - 1] and CODE_PREAMBLE.match(lines[j - 1]):
            j -= 1  # keep decorators and leading comments with the declaration
        starts.append(j)
    return starts


def _json_split(stdout: str) -> Optional[Tuple[str, List[int]]]:
    stripped = stdout.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(data, (list, dict)):
        return None
    text = json.dumps(data, indent=1, ensure_ascii=False)
    lines = text.split("\n")
    depth = 1
    if len(data) == 1:
        only = next(iter(data.values())) if isinstance(data, dict) else data[0]
        if isinstance(only, (list, dict)) and len(only) > 1:
            depth = 2  # descend through a single wrapper key or item
    prefix = " " * depth
    starts = [
        i for i, line in enumerate(lines)
        if line.startswith(prefix) and len(line) > depth and line[depth] not in " }]"
    ]
    return text, starts


# ---- detection --------------------------------------------------------------

def _looks_like_path_lines(lines: List[str]) -> bool:
    nonempty = [ln for ln in lines if ln.strip()]
    if len(nonempty) < 3:
        return False
    hits = 0
    for ln in nonempty:
        m = PATH_LINE.match(ln)
        if m and ("/" in m.group("path") or "." in m.group("path")):
            hits += 1
    return hits * 2 >= len(nonempty)


def _file_ext(args: List[str]) -> str:
    for a in args:
        if a in ("|", ">", ">>", "<", ";", "&&", "||", "&"):
            break
        if a.startswith("-"):
            continue
        return os.path.splitext(a)[1].lower()
    return ""


def _detect(command: str, stdout: str) -> Tuple[str, str, List[str], List[int]]:
    """Return (splitter name, text, lines, block starts)."""
    lines = to_lines(stdout)
    # Content checks first: diff, log and JSON win over the command word.
    if any(DIFF_START.match(ln) for ln in lines):
        return "diff", stdout, lines, _diff_starts(lines)
    commits = _starts_matching(lines, COMMIT_START)
    if len(commits) >= 2:
        return "git_log", stdout, lines, commits
    parsed = _json_split(stdout)
    if parsed is not None:
        text, starts = parsed
        return "json", text, text.split("\n"), starts

    name, args = shell.first_command(command)
    if name in ("grep", "egrep", "fgrep", "rg", "ag"):
        return "grep", stdout, lines, _grep_starts(lines, loose=True)
    if name == "git":
        sub = shell.git_subcommand(args)
        if sub == "log":
            return "git_log", stdout, lines, commits
        if sub in ("diff", "show"):
            return "diff", stdout, lines, _diff_starts(lines)
    if name == "ls":
        return "ls", stdout, lines, _paragraph_starts(lines)
    if name in ("find", "fd"):
        return "find", stdout, lines, _find_starts(lines)
    if name == "tree":
        return "tree", stdout, lines, _starts_matching(lines, TREE_TOP)
    if name in ("cat", "less", "more", "bat"):
        ext = _file_ext(args)
        if ext in MARKDOWN_EXT:
            return "markdown", stdout, lines, _markdown_starts(lines)
        if ext in CODE_EXT:
            return "code", stdout, lines, _code_starts(lines)
    if _looks_like_path_lines(lines):
        return "grep", stdout, lines, _grep_starts(lines, loose=False)
    return "paragraphs", stdout, lines, _paragraph_starts(lines)


# ---- normalization ----------------------------------------------------------

def _merge_small(lines: List[str], blocks: List[Block]) -> List[Block]:
    out: List[Block] = []
    cur: Optional[Block] = None
    for b in blocks:
        if cur is None:
            cur = Block(b.start, b.end)
        elif _span_chars(lines, cur.start, cur.end) < MIN_BLOCK_CHARS:
            cur = Block(cur.start, b.end)
        else:
            out.append(cur)
            cur = Block(b.start, b.end)
    if cur is not None:
        if out and _span_chars(lines, cur.start, cur.end) < MIN_BLOCK_CHARS:
            out[-1] = Block(out[-1].start, cur.end)
        else:
            out.append(cur)
    return out


def _split_large(lines: List[str], block: Block) -> List[Block]:
    size = _span_chars(lines, block.start, block.end)
    if size <= MAX_BLOCK_CHARS or block.end - block.start < 2:
        return [block]
    pieces = math.ceil(size / MAX_BLOCK_CHARS)
    target = size / pieces
    out = []
    start = block.start
    acc = 0
    for i in range(block.start, block.end):
        add = len(lines[i]) + (1 if i > start else 0)
        if i > start and (acc >= target or acc + add > MAX_BLOCK_CHARS):
            out.append(Block(start, i))
            start, acc = i, len(lines[i])
        else:
            acc += add
    out.append(Block(start, block.end))
    return out


def split(command: str, stdout: str) -> Split:
    splitter, text, lines, starts = _detect(command, stdout)
    blocks = _blocks_from_starts(starts, len(lines))
    if len(blocks) <= 1 and splitter != "paragraphs":
        splitter = "paragraphs"
        blocks = _blocks_from_starts(_paragraph_starts(lines), len(lines))
    if len(blocks) <= 1:
        splitter = "windows"
        blocks = _blocks_from_starts(_window_starts(lines), len(lines))
    blocks = _merge_small(lines, blocks)
    final: List[Block] = []
    for b in blocks:
        final.extend(_split_large(lines, b))
    return Split(text=text, lines=lines, blocks=final, splitter=splitter)

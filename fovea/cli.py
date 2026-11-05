#!/usr/bin/env python3
"""Fovea command line: `fovea note` (the agent reports a bad filter) and `fovea report`.

Everything is read from and written to FOVEA_HOME; nothing leaves the machine.
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

if __package__ in (None, ""):
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path and os.path.abspath(sys.path[0] or ".") == os.path.dirname(os.path.abspath(__file__)):
        sys.path[0] = _ROOT  # never let package modules shadow the standard library
    elif _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

from fovea import config, store  # noqa: E402

USAGE = """\
usage: fovea note <id> "<what went wrong>"
       fovea report [--days N]

  note    record that a filtered output (header `[fovea <id>: ...]`) hid something
          you needed or misled you; saved to FOVEA_HOME/notes.jsonl
  report  summary of the last N days (default 7) from the local logs, notes and transcripts
"""

NOTES_FILE = "notes.jsonl"
DEFAULT_DAYS = 7
CHARS_PER_TOKEN = 3.5
LOSS_LOOKAHEAD = 20  # assistant records after the tool result
LOSS_MIN_CHARS = 25  # non-space characters for a line to count as a quote
SHADOW_ACTIONS = ("shadow", "sampled_shadow")


def _usage_error() -> int:
    sys.stderr.write(USAGE)
    return 2


# ---- logs -------------------------------------------------------------------

def _read_jsonl(path: str) -> Iterable[dict]:
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def load_logs(home: str) -> List[dict]:
    records: List[dict] = []
    for path in sorted(glob.glob(os.path.join(home, "log", "*.jsonl"))):
        records.extend(_read_jsonl(path))
    return records


def record_id(rec: dict) -> Optional[str]:
    """The output id of a log line: `out_id`, or the name of `out_path` for older lines."""
    oid = rec.get("out_id")
    if isinstance(oid, str) and oid:
        return oid
    path = rec.get("out_path")
    if isinstance(path, str) and path:
        return store.out_id(path)
    return None


def index_by_id(records: List[dict]) -> Dict[str, dict]:
    """id -> the latest log line for it, preferring a `replaced` line."""
    index: Dict[str, dict] = {}
    for rec in records:  # in file order, so later lines win
        oid = record_id(rec)
        if not oid:
            continue
        prev = index.get(oid)
        if prev is None or rec.get("action") == "replaced" or prev.get("action") != "replaced":
            index[oid] = rec
    return index


def _parse_ts(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.astimezone()
    return ts


def _in_window(rec: dict, since: datetime) -> bool:
    ts = _parse_ts(rec.get("ts"))
    return ts is not None and ts >= since


# ---- note ---------------------------------------------------------------------

_ID_PREFIX = re.compile(r"^\[?fovea\s+", re.IGNORECASE)


def normalize_id(raw: str) -> str:
    """Accept `7f3a2c1e`, `7f3a2c1e.txt`, a path to the saved file, or `[fovea 7f3a2c1e:`."""
    text = _ID_PREFIX.sub("", raw.strip()).rstrip(":]").strip()
    text = text.rsplit("/", 1)[-1]
    if text.endswith(".txt"):
        text = text[:-4]
    return text.lower()


def cmd_note(args: List[str], cfg) -> int:
    if len(args) == 1 and args[0] in ("-h", "--help"):
        sys.stdout.write(USAGE)
        return 0
    if len(args) < 2 or args[0].startswith("-"):
        return _usage_error()
    oid = normalize_id(args[0])
    note = " ".join(args[1:]).strip()
    if not oid or not note:
        return _usage_error()

    match = index_by_id(load_logs(cfg.home)).get(oid)
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "id": oid,
        "note": note,
        "cwd": os.getcwd() if _cwd_ok() else None,
        "matched": match is not None,
        "session_id": match.get("session_id") if match else None,
        "command": match.get("command") if match else None,
        "task": match.get("task") if match else None,
    }
    data = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8", "surrogatepass")
    try:
        store.append_private(cfg.home, os.path.join(cfg.home, NOTES_FILE), data)
    except OSError as exc:
        sys.stderr.write("fovea: could not save the note: %s\n" % exc)
        return 1
    if match is not None:
        command = _short(match.get("command") or "", 60)
        _out("fovea: note saved for %s%s\n" % (oid, " (%s)" % command if command else ""))
    else:
        _out("fovea: note saved, but no logged output has id %s (matched: false)\n" % oid)
    return 0


def _cwd_ok() -> bool:
    try:
        os.getcwd()
        return True
    except OSError:
        return False


# ---- report: losses -----------------------------------------------------------

def _load_transcript(path: str, cache: Dict[str, Optional[List[dict]]]) -> Optional[List[dict]]:
    if path not in cache:
        cache[path] = list(_read_jsonl(path)) if os.path.isfile(path) else None
    return cache[path]


def _tool_result_text(rec: dict, tool_use_id: str) -> Optional[str]:
    message = rec.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return None
    for item in content:
        if not (isinstance(item, dict) and item.get("type") == "tool_result"
                and item.get("tool_use_id") == tool_use_id):
            continue
        result = rec.get("toolUseResult")
        if isinstance(result, dict) and isinstance(result.get("stdout"), str):
            return result["stdout"]  # exactly what the hook saw
        body = item.get("content")
        if isinstance(body, str):
            return body
        if isinstance(body, list):
            return "\n".join(part.get("text", "") for part in body
                             if isinstance(part, dict) and isinstance(part.get("text"), str))
        return None
    return None


def _strings(value) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _assistant_haystack(records: List[dict], start: int, session_id) -> str:
    """Text and tool inputs of the next assistant records after `start`."""
    parts: List[str] = []
    seen = 0
    for rec in records[start + 1:]:
        if seen >= LOSS_LOOKAHEAD:
            break
        if rec.get("type") != "assistant":
            continue
        sid = rec.get("sessionId")
        if session_id and sid and sid != session_id:
            continue
        seen += 1
        message = rec.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            parts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif item.get("type") == "tool_use":
                parts.extend(_strings(item.get("input")))
    return "\n".join(parts)


def _quoted(block_lines: List[str], haystack: str) -> bool:
    for line in block_lines:
        text = line.strip()
        if len(re.sub(r"\s", "", text)) >= LOSS_MIN_CHARS and text in haystack:
            return True
    return False


def _shadow_id(rec: dict) -> str:
    oid = record_id(rec)
    if oid:
        return oid
    tuid = rec.get("tool_use_id")
    return store.out_id(store.out_name(tuid, "")) if isinstance(tuid, str) and tuid else "-"


def _dropped(rec: dict, threshold: float) -> List[Tuple[str, float]]:
    """(label, score) of the blocks the filter did not keep, in label order."""
    scores = rec.get("scores")
    if not isinstance(scores, dict):
        return []
    kept = rec.get("kept")
    out = []
    for label in sorted(scores, key=lambda lab: (len(lab), lab)):  # A999 before A1000
        score = scores[label]
        if not isinstance(score, (int, float)):
            continue
        dropped = label not in kept if isinstance(kept, list) else score < threshold
        if dropped:
            out.append((label, float(score)))
    return out


def measure_losses(records: List[dict], threshold: float):
    """Return (measured, unmeasured, outputs with a loss, [(id, command, label, score)])."""
    from fovea import split

    cache: Dict[str, Optional[List[dict]]] = {}
    measured = unmeasured = lossy = 0
    losses: List[Tuple[str, str, str, float]] = []
    for rec in records:
        tuid = rec.get("tool_use_id")
        tpath = rec.get("transcript_path")
        block_lines = rec.get("block_lines")
        if not (isinstance(tuid, str) and tuid and isinstance(tpath, str) and tpath
                and isinstance(block_lines, dict) and block_lines):
            unmeasured += 1
            continue
        transcript = _load_transcript(tpath, cache)
        found = None
        if transcript is not None:
            for i, entry in enumerate(transcript):
                text = _tool_result_text(entry, tuid)
                if text is not None:
                    found = (i, text)
                    break
        if found is None:
            unmeasured += 1
            continue
        index, output = found
        lines = split.split("", output).lines  # the same text the hook cut (JSON is re-printed)
        spans = {}
        for label, span in block_lines.items():
            if (isinstance(span, list) and len(span) == 2
                    and all(isinstance(x, int) for x in span) and 1 <= span[0] <= span[1]):
                spans[label] = span
        if not spans or max(b for _, b in spans.values()) > len(lines):
            unmeasured += 1  # the transcript holds a different text (e.g. a preview)
            continue
        measured += 1
        haystack = _assistant_haystack(transcript, index, rec.get("session_id"))
        found_loss = False
        for label, score in _dropped(rec, threshold):
            span = spans.get(label)
            if span and _quoted(lines[span[0] - 1:span[1]], haystack):
                losses.append((_shadow_id(rec), rec.get("command") or "", label, score))
                found_loss = True
        if found_loss:
            lossy += 1
    return measured, unmeasured, lossy, losses


# ---- report -------------------------------------------------------------------

def _percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int(math.ceil(p * len(ordered))))
    return ordered[rank - 1]


def _ms(value: Optional[float]) -> str:
    return "-" if value is None else "%d" % value


def _pct(part: float, whole: float) -> str:
    return "%d%%" % int(round(100.0 * part / whole)) if whole else "n/a"


def _num(n: int) -> str:
    return "{:,}".format(int(n))


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def build_report(cfg, days: int, now: Optional[datetime] = None) -> str:
    now = now or datetime.now().astimezone()
    since = now - timedelta(days=days)
    all_logs = load_logs(cfg.home)
    by_id = index_by_id(all_logs)
    logs = [r for r in all_logs if _in_window(r, since)]
    out: List[str] = []
    w = out.append

    w("Fovea report: last %d day%s (since %s), home %s"
      % (days, "" if days == 1 else "s", since.strftime("%Y-%m-%d %H:%M"),
         store.display_path(cfg.home)))
    w("")

    # Calls
    eligible = [r for r in logs if r.get("splitter")]
    replaced = [r for r in logs if r.get("action") == "replaced"]
    passthrough = Counter(str(r.get("reason") or "unknown") for r in logs
                          if r.get("action") == "passthrough")
    judge_errors = sum(1 for r in logs if r.get("reason") == "judge_error")
    judged_latency = [r["latency_ms"] for r in eligible if isinstance(r.get("latency_ms"), (int, float))]
    all_latency = [r["latency_ms"] for r in logs if isinstance(r.get("latency_ms"), (int, float))]
    costs = [r["cost"] for r in logs if isinstance(r.get("cost"), (int, float))]
    w("Calls")
    w("  seen %d, eligible %d, replaced %d" % (len(logs), len(eligible), len(replaced)))
    if passthrough:
        w("  passthrough by reason: " + ", ".join(
            "%s %d" % (reason, n) for reason, n in sorted(passthrough.items(), key=lambda x: (-x[1], x[0]))))
    else:
        w("  passthrough by reason: none")
    w("  judge errors: %d" % judge_errors)
    w("  latency p50/p95: %s/%s ms on eligible calls; %s/%s ms on all calls"
      % (_ms(_percentile(judged_latency, 0.5)), _ms(_percentile(judged_latency, 0.95)),
         _ms(_percentile(all_latency, 0.5)), _ms(_percentile(all_latency, 0.95))))
    w("  total cost: %s" % ("$%.4f" % sum(costs) if costs else "unknown"))
    w("")

    # Savings
    total = sum(_int(r.get("chars_total")) for r in replaced)
    kept = sum(_int(r.get("chars_kept")) for r in replaced)
    w("Savings on replaced outputs")
    w("  chars total %s, kept %s, saved %s" % (_num(total), _num(kept), _pct(total - kept, total)))
    w("  ~%s tokens saved (chars / %g)" % (_num(round((total - kept) / CHARS_PER_TOKEN)), CHARS_PER_TOKEN))
    w("")

    # Losses
    shadow = [r for r in logs if r.get("action") in SHADOW_ACTIONS]
    measured, unmeasured, lossy, losses = measure_losses(shadow, cfg.threshold)
    w("Losses (shadow and sampled_shadow outputs)")
    w("  measured %d, unmeasured %d" % (measured, unmeasured))
    w("  used blocks that would have been dropped: %d, in %d of %d measured outputs (%s)"
      % (len(losses), lossy, measured, _pct(lossy, measured)))
    for oid, command, label, score in losses:
        w("    %s  %s  p=%.3f  %s" % (oid, label, score, _short(command, 80)))
    w("")

    # Recoveries
    recoveries = [r for r in logs if isinstance(r.get("recovered_id"), str) and r.get("recovered_id")]
    reads = Counter(r["recovered_id"] for r in recoveries)
    replaced_ids = {record_id(r) for r in replaced}
    w("Recoveries")
    w("  %d read%s of %d output%s; %s of replaced outputs were read back"
      % (len(recoveries), "" if len(recoveries) == 1 else "s", len(reads), "" if len(reads) == 1 else "s",
         _pct(len(replaced_ids & set(reads)), len(replaced))))
    for r in recoveries:
        rid = r["recovered_id"]
        original = by_id.get(rid)
        command = original.get("command") if original else None
        w("    %s  %s  (output of: %s)" % (rid, _short(r.get("command") or "", 80),
                                         _short(command, 60) if command else "not in the logs"))
    w("")

    # Notes
    notes = [n for n in _read_jsonl(os.path.join(cfg.home, NOTES_FILE)) if _in_window(n, since)]
    w("Notes")
    w("  %d" % len(notes))
    for n in notes:
        oid = str(n.get("id") or "-")
        ts = _parse_ts(n.get("ts"))
        w("    %s  %s  %s" % (ts.strftime("%Y-%m-%d %H:%M") if ts else "-", oid,
                              json.dumps(str(n.get("note") or ""), ensure_ascii=False)))
        rec = by_id.get(oid)
        command = n.get("command") or (rec.get("command") if rec else None)
        task = n.get("task") or (rec.get("task") if rec else None)
        if command:
            w("      command: %s" % _short(command, 100))
        if task:
            w("      task: %s" % _short(task, 160))
        if rec is None:
            w("      no logged output with this id")
            continue
        n_blocks = rec.get("n_blocks")
        kept_labels = rec.get("kept") if isinstance(rec.get("kept"), list) else []
        w("      blocks: kept %d/%s" % (len(kept_labels), n_blocks if isinstance(n_blocks, int) else "?"))
        dropped = _dropped(rec, cfg.threshold)
        w("      dropped: %s" % (", ".join("%s %.3f" % (lab, p) for lab, p in dropped) or "none"))
    return "\n".join(out) + "\n"


def cmd_report(args: List[str], cfg) -> int:
    days = DEFAULT_DAYS
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-h", "--help"):
            sys.stdout.write(USAGE)
            return 0
        if arg == "--days" and i + 1 < len(args):
            value = args[i + 1]
            i += 2
        elif arg.startswith("--days="):
            value = arg[len("--days="):]
            i += 1
        else:
            return _usage_error()
        try:
            days = int(value)
        except ValueError:
            return _usage_error()
        if days < 1:
            return _usage_error()
    _out(build_report(cfg, days))
    return 0


# ---- entry --------------------------------------------------------------------

def _out(text: str) -> None:
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        enc = sys.stdout.encoding or "utf-8"
        stream.write(text.encode(enc, "replace"))
        stream.flush()
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        return _usage_error()
    command, rest = argv[0], argv[1:]
    if command in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0
    cfg = config.load()
    if command == "note":
        return cmd_note(rest, cfg)
    if command == "report":
        return cmd_report(rest, cfg)
    return _usage_error()


if __name__ == "__main__":
    sys.exit(main())

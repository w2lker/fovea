"""Client for a decisions endpoint: labeled blocks in `state`, one `noul` question per block."""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Dict, List, Optional, Tuple

STATE_HEADER = "Tool output split into labeled blocks.\n\n"
MAX_STATE_CHARS = 100_000


class JudgeError(Exception):
    pass


def label(index: int) -> str:
    """Label for the 1-based block index: A001…A999, then A1000…"""
    return "A%03d" % index


def question(task: str, lab: str) -> dict:
    return {
        "type": "noul",
        "instructions": (
            "The coding agent's current task: %s\n"
            "Does block %s (the text between [%s] and [/%s]) contain information "
            "the agent needs for this task?" % (task, lab, lab, lab)
        ),
    }


def _wrap(lab: str, text: str) -> str:
    return "[%s]\n%s\n[/%s]" % (lab, text, lab)


def chunk(items: List[Tuple[str, str]]) -> List[List[Tuple[str, str]]]:
    """Group (label, text) pairs so that each request's state stays under the limit."""
    chunks: List[List[Tuple[str, str]]] = []
    current: List[Tuple[str, str]] = []
    size = len(STATE_HEADER)
    for lab, text in items:
        add = len(_wrap(lab, text)) + 2
        if current and size + add > MAX_STATE_CHARS:
            chunks.append(current)
            current, size = [], len(STATE_HEADER)
        current.append((lab, text))
        size += add
    if current:
        chunks.append(current)
    return chunks


def build_body(model: str, task: str, items: List[Tuple[str, str]]) -> dict:
    state = STATE_HEADER + "\n\n".join(_wrap(lab, text) for lab, text in items)
    return {
        "model": model,
        "state": state,
        "questions": {lab: question(task, lab) for lab, _ in items},
    }


def parse_answers(data, labels: List[str]) -> Tuple[Dict[str, float], Optional[float]]:
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise JudgeError("malformed answer: no answers object")
    answers = data["answers"]
    scores: Dict[str, float] = {}
    for lab in labels:
        entry = answers.get(lab)
        value = entry.get("noul") if isinstance(entry, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise JudgeError("malformed answer for %s" % lab)
        value = float(value)
        if math.isnan(value) or value < 0.0 or value > 1.0:
            raise JudgeError("probability out of range for %s" % lab)
        scores[lab] = value
    cost = None
    usage = data.get("usage")
    if isinstance(usage, dict):
        raw = usage.get("cost")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            cost = float(raw)
    return scores, cost


def _post(url: str, key: str, body: dict, timeout: float):
    import urllib.request

    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise JudgeError("malformed answer: not JSON")


def score(url: str, key: str, model: str, task: str, items: List[Tuple[str, str]],
          timeout: float) -> Tuple[Dict[str, float], Optional[float]]:
    """Score every block; any failure in any request raises JudgeError.

    Requests run in daemon threads under one overall deadline, so a slow endpoint can
    never hold the hook past `timeout` (plus a small margin).
    """
    chunks = chunk(items)
    results: List[Optional[Tuple[Dict[str, float], Optional[float]]]] = [None] * len(chunks)
    errors: List[Optional[str]] = [None] * len(chunks)

    def run(i: int, part: List[Tuple[str, str]]) -> None:
        try:
            data = _post(url, key, build_body(model, task, part), timeout)
            results[i] = parse_answers(data, [lab for lab, _ in part])
        except JudgeError as exc:
            errors[i] = str(exc)
        except Exception as exc:  # network, HTTP status, timeout
            errors[i] = "%s: %s" % (type(exc).__name__, str(exc)[:200])

    threads = [threading.Thread(target=run, args=(i, part), daemon=True) for i, part in enumerate(chunks)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout + 0.5
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    if any(t.is_alive() for t in threads):
        raise JudgeError("timeout")
    for err in errors:
        if err:
            raise JudgeError(err)
    scores: Dict[str, float] = {}
    cost_total: Optional[float] = None
    for res in results:
        if res is None:
            raise JudgeError("missing result")
        part_scores, cost = res
        scores.update(part_scores)
        if cost is not None:
            cost_total = (cost_total or 0.0) + cost
    return scores, cost_total

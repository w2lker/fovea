#!/usr/bin/env python3
"""Fovea PostToolUse hook: reads the hook JSON on stdin, prints a replacement or nothing.

Always exits 0. On any failure the output passes through unchanged and the error is logged.
"""

from __future__ import annotations

import json
import os
import sys
import time

if __package__ in (None, ""):
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path and os.path.abspath(sys.path[0] or ".") == os.path.dirname(os.path.abspath(__file__)):
        sys.path[0] = _ROOT  # never let package modules shadow the standard library
    elif _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

from fovea import __version__, config, guards, store  # noqa: E402

KEPT_SHARE_LIMIT = 0.8
# Fields Claude Code adds when it saved the full output itself. Left in a replacement, they make
# it wrap whatever the hook returns in <persisted-output> and show only a 2 KB preview of it.
PERSISTED_KEYS = ("persistedOutputPath", "persistedOutputSize")
# A replacement must stay under the harness limit, or it costs more than the preview it replaces.
BUDGET_MARGIN = 2000


def _base_record(payload: dict, cfg) -> dict:
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    transcript = payload.get("transcript_path")
    return {
        "session_id": payload.get("session_id"),
        "tool_use_id": payload.get("tool_use_id"),
        "cwd": payload.get("cwd"),
        "command": command[:200] if isinstance(command, str) else None,
        "mode": cfg.mode,
        "version": __version__,
        # Settings that change decisions; without them a log can't be split by configuration.
        "config": {"threshold": cfg.threshold, "min_top": cfg.min_top, "min_chars": cfg.min_chars,
                   "shadow_sample": cfg.shadow_sample, "model": cfg.model},
        "action": "passthrough",
        "transcript_path": transcript if isinstance(transcript, str) else None,
    }


def with_full_text(payload: dict, cfg, record: dict):
    """Swap the cut stdout for the full output Claude Code persisted.

    Above BASH_MAX_OUTPUT_LENGTH Claude Code cuts stdout before the hook and saves the full
    output to tool_response.persistedOutputPath; the file already exists when the hook runs.
    Returns (payload, too_large): the payload with the full text, or unchanged when there is
    no path, the file is unreadable, or it is larger than FOVEA_MAX_PERSISTED_BYTES.
    """
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        return payload, False
    path = response.get("persistedOutputPath")
    if not isinstance(path, str) or not path:
        return payload, False
    record["persisted"] = True
    try:
        size = os.path.getsize(path)
        record["persisted_bytes"] = size
        if size > cfg.max_persisted_bytes:
            return payload, True
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        record["persisted_unreadable"] = True
        return payload, False
    full = {k: v for k, v in response.items() if k not in PERSISTED_KEYS}
    full["stdout"] = data.decode("utf-8", "replace")
    new = dict(payload)
    new["tool_response"] = full
    new[guards.FULL_TEXT_KEY] = True
    return new, False


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8", "replace"))


def process(payload: dict, cfg, record: dict):
    """Decide for one call; return the replacement tool output or None."""
    payload, too_large = with_full_text(payload, cfg, record)
    reason = guards.check(payload, cfg)
    if reason == "harness_persisted" and too_large:
        reason = "persisted_too_large"
    if reason:
        record["reason"] = reason
        if reason == "recovery_read":
            record["recovered_id"] = guards.recovered_id(payload["tool_input"]["command"], cfg)
        return None

    from fovea import judge, render, split, task

    response = payload["tool_response"]
    command = payload["tool_input"]["command"] if isinstance(payload.get("tool_input"), dict) else ""
    command = command if isinstance(command, str) else ""
    task_text = task.build(payload)
    record["task"] = task_text[:300]

    sp = split.split(command, response["stdout"])
    labels = [judge.label(i + 1) for i in range(len(sp.blocks))]
    texts = [sp.block_text(b) for b in sp.blocks]
    sizes = [len(t) for t in texts]
    chars_total = len(sp.text)
    record.update({
        "splitter": sp.splitter,
        "n_blocks": len(sp.blocks),
        "block_lines": {lab: [b.first_line, b.last_line] for lab, b in zip(labels, sp.blocks)},
        "chars_total": chars_total,
    })

    sampled = False
    if cfg.mode == "active" and cfg.shadow_sample > 0:
        import random

        sampled = random.random() < cfg.shadow_sample

    try:
        scores, cost = judge.score(cfg.base_url, cfg.api_key, cfg.model, task_text,
                                   list(zip(labels, texts)), cfg.timeout)
    except judge.JudgeError as exc:
        record["reason"] = "judge_error"
        record["error"] = str(exc)[:300]
        return None
    record["scores"] = scores
    record["cost"] = cost

    kept = [lab for lab in labels if scores[lab] >= cfg.threshold]
    chars_kept = sum(size for lab, size in zip(labels, sizes) if lab in set(kept))
    record["kept"] = kept
    record["chars_kept"] = chars_kept
    if not kept:
        record["reason"] = "nothing_relevant"
        return None
    if max(scores.values()) < cfg.min_top:
        record["reason"] = "low_contrast"
        return None
    if chars_total and chars_kept >= KEPT_SHARE_LIMIT * chars_total:
        record["reason"] = "not_worth_it"
        return None
    if cfg.mode == "shadow":
        record["action"] = "shadow"
        return None
    if sampled:
        record["action"] = "sampled_shadow"
        return None

    path = store.save_output(cfg.home, str(payload.get("tool_use_id") or ""), sp.text)
    record["out_path"] = path
    record["out_id"] = store.out_id(path)
    # An output at the harness limit is read back by range: a cat of it would be cut and saved again.
    ranged = _utf8_len(sp.text) >= cfg.harness_limit

    def draw(keep: set) -> str:
        kept_chars = sum(size for lab, size in zip(labels, sizes) if lab in keep)
        return render.render(sp, labels, keep, chars_total, kept_chars, record["out_id"],
                             store.display_path(path), path, cfg.retention_days, ranged=ranged)

    budget = max(cfg.harness_limit - BUDGET_MARGIN, 1000)
    keep = set(kept)
    stdout = draw(keep)
    if _utf8_len(stdout) >= budget:
        # Keep the highest-scoring blocks that fit; ties keep the output order.
        by_score = sorted(kept, key=lambda lab: -scores[lab])
        byte_size = {lab: _utf8_len(t) for lab, t in zip(labels, texts)}
        keep, used = set(), 0
        for lab in by_score:
            if used + byte_size[lab] < budget:
                keep.add(lab)
                used += byte_size[lab]
        stdout = draw(keep)
        for lab in reversed(by_score):  # skip lines and range markers add a little
            if _utf8_len(stdout) < budget or not keep:
                break
            if lab in keep:
                keep.discard(lab)
                stdout = draw(keep)
        if not keep:
            record["reason"] = "over_budget"
            return None
        record["budget_dropped"] = len(kept) - len(keep)
        record["kept"] = [lab for lab in labels if lab in keep]
        record["chars_kept"] = sum(size for lab, size in zip(labels, sizes) if lab in keep)
    replacement = {k: v for k, v in response.items() if k not in PERSISTED_KEYS}
    replacement["stdout"] = stdout
    record["action"] = "replaced"
    return replacement


def _read_stdin():
    stream = getattr(sys.stdin, "buffer", None)
    return stream.read() if stream is not None else sys.stdin.read()


def _write_stdout(data: str) -> None:
    # ASCII-only JSON, so the text path is safe when stdout has no buffer.
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        stream.write(data.encode("ascii"))
        stream.flush()
    else:
        sys.stdout.write(data)
        sys.stdout.flush()


def main() -> int:
    started = time.monotonic()
    cfg = None
    record: dict = {"action": "passthrough"}
    output = None
    try:
        cfg = config.load()
        record["mode"] = cfg.mode
        payload = json.loads(_read_stdin())
        if not isinstance(payload, dict):
            raise ValueError("hook input is not a JSON object")
        record = _base_record(payload, cfg)
        output = process(payload, cfg, record)
    except BaseException as exc:  # never break the tool call
        output = None
        record["action"] = "passthrough"
        record["reason"] = "exception"
        record["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    if output is not None:
        try:
            data = json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "updatedToolOutput": output,
                }
            })
            _write_stdout(data)
        except BaseException as exc:
            record["action"] = "passthrough"
            record["reason"] = "exception"
            record["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    record["latency_ms"] = int(round((time.monotonic() - started) * 1000))
    try:
        home = cfg.home if cfg is not None else config.load().home
        store.write_log(home, record)
    except BaseException:
        pass
    if cfg is not None and cfg.mode_valid and cfg.mode != "off":
        try:
            store.cleanup(cfg.home, cfg.retention_days, cfg.max_store_mb, keep=record.get("out_path"))
        except BaseException:
            pass
    return 0


if __name__ == "__main__":
    main()
    try:
        sys.stdout.flush()
    finally:
        # Exit at once: judge threads are daemons and must not delay the tool call.
        os._exit(0)

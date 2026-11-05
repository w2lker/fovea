"""Fovea v1.1 contract tests for `fovea report`, derived only from docs/DESIGN.md."""
import json
import os
import re
import time
import unittest

from test_v11_feedback import V11Base, CLI, today

TH = 0.2


def line(tag, n=40):
    return ("%s " % tag + "verbatim-content-word " * 10)[:n + 40].strip()


class ReportBase(V11Base):
    def report(self, *args):
        rc, out, err = self.run_py(CLI, ["report"] + list(args))
        self.assertEqual(rc, 0, err)
        return out.replace(",", "")

    def transcript_with(self, tool_use_id, output, later_assistant_texts, session="sess-r", pad=0):
        recs = [{"type": "user", "sessionId": session, "message": {"role": "user", "content": "do the task"}},
                {"type": "assistant", "sessionId": session, "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": {"command": "x"}}]}},
                {"type": "user", "sessionId": session, "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": output}]}}]
        for _ in range(pad):
            recs.append({"type": "assistant", "sessionId": session,
                         "message": {"role": "assistant", "content": [{"type": "text", "text": "thinking"}]}})
        for t in later_assistant_texts:
            recs.append({"type": "assistant", "sessionId": session,
                         "message": {"role": "assistant", "content": [{"type": "text", "text": t}]}})
        path = os.path.join(self.tmp, "tr-%s.jsonl" % tool_use_id)
        with open(path, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        return path

    def shadow_record(self, tuid, command, transcript, scores, block_lines, action="sampled_shadow", **extra):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session_id": "sess-r",
               "tool_use_id": tuid, "cwd": "/w", "command": command, "task": "t", "mode": "active",
               "action": action, "reason": "", "splitter": "paragraph", "n_blocks": len(scores),
               "block_lines": block_lines, "scores": scores,
               "kept": [k for k, v in scores.items() if v >= TH], "chars_total": 1000, "chars_kept": 400,
               "latency_ms": 100, "cost": 0.0006, "out_id": "aaaaaaaa", "transcript_path": transcript}
        rec.update(extra)
        return rec


class LossTests(ReportBase):
    # block A001: lines 1-3 (low score), A002: lines 5-7 (high), A003: lines 9-11 (low, never quoted)
    def output(self):
        L = {}
        lines = []
        for i, tag in enumerate(["LOSTBLK", "x", "x", None, "USEDOK", "x", "x", None, "NEVERQ", "x", "x"]):
            if tag is None:
                lines.append("")
            elif tag == "x":
                lines.append("short")
            else:
                lines.append("%s line number one is long enough to be quoted verbatim ok" % tag)
        return "\n".join(lines), lines

    BL = {"A001": [1, 3], "A002": [5, 7], "A003": [9, 11]}
    SC = {"A001": 0.05, "A002": 0.9, "A003": 0.01}

    def test_low_scored_quoted_block_is_loss(self):
        out, lines = self.output()
        quoted = lines[0]
        self.assertGreaterEqual(len(quoted.replace(" ", "")), 25)
        tr = self.transcript_with("toolu_loss", out, ["Based on the output: %s -- so I fix it." % quoted])
        self.write_log([self.shadow_record("toolu_loss", "cmd-lossy", tr, self.SC, self.BL)])
        rep = self.report()
        self.assertIn("cmd-lossy", rep)
        self.assertIn("A001", rep)
        self.assertIn("0.05", rep)
        self.assertNotIn("A003", rep)

    def test_quoted_block_above_threshold_is_not_loss(self):
        out, lines = self.output()
        tr = self.transcript_with("toolu_fine", out, ["I see: %s" % lines[4]])
        self.write_log([self.shadow_record("toolu_fine", "cmd-fine", tr, self.SC, self.BL)])
        rep = self.report()
        self.assertNotIn("cmd-fine", rep)

    def test_quote_in_tool_input_counts(self):
        out, lines = self.output()
        tr = self.transcript_with("toolu_ti", out, [])
        with open(tr, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "sessionId": "sess-r", "message": {
                "role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "Bash",
                                                  "input": {"command": "grep -n '%s' f" % lines[0]}}]}}) + "\n")
        self.write_log([self.shadow_record("toolu_ti", "cmd-toolinput", tr, self.SC, self.BL)])
        rep = self.report()
        self.assertIn("cmd-toolinput", rep)

    def test_short_line_quote_does_not_count(self):
        out = "\n".join(["tiny line", "x", "x", "", "KEEP " + "k" * 60])
        tr = self.transcript_with("toolu_short", out, ["tiny line is here"])
        self.write_log([self.shadow_record("toolu_short", "cmd-short", tr, {"A001": 0.01, "A002": 0.9},
                                           {"A001": [1, 3], "A002": [5, 5]})])
        self.assertNotIn("cmd-short", self.report())

    def test_quote_after_window_does_not_count(self):
        out, lines = self.output()
        tr = self.transcript_with("toolu_late", out, ["found: %s" % lines[0]], pad=25)
        # the quote sits after 25 filler records: outside the next 20 assistant records
        self.write_log([self.shadow_record("toolu_late", "cmd-late", tr, self.SC, self.BL)])
        self.assertNotIn("cmd-late", self.report())

    def test_shadow_action_also_measured(self):
        out, lines = self.output()
        tr = self.transcript_with("toolu_sh", out, ["use %s" % lines[0]])
        self.write_log([self.shadow_record("toolu_sh", "cmd-shadowmode", tr, self.SC, self.BL, action="shadow")])
        self.assertIn("cmd-shadowmode", self.report())

    def test_missing_transcript_is_unmeasured(self):
        self.write_log([self.shadow_record("toolu_gone", "cmd-gone", os.path.join(self.tmp, "nope.jsonl"),
                                           self.SC, self.BL)])
        rep = self.report()
        self.assertRegex(rep.lower(), r"unmeasured\D{0,20}1|1\D{0,20}unmeasured")

    def test_missing_tool_result_is_unmeasured(self):
        out, lines = self.output()
        tr = self.transcript_with("toolu_other", out, ["use %s" % lines[0]])
        self.write_log([self.shadow_record("toolu_notinfile", "cmd-noresult", tr, self.SC, self.BL)])
        rep = self.report()
        self.assertRegex(rep.lower(), r"unmeasured\D{0,20}1|1\D{0,20}unmeasured")
        self.assertNotIn("cmd-noresult", rep)


class SummaryTests(ReportBase):
    def test_savings_match_log_sums(self):
        recs = [self.shadow_record("t1", "cmd-a", "", {"A001": 0.9}, {"A001": [1, 2]}, action="replaced",
                                   chars_total=10000, chars_kept=2000),
                self.shadow_record("t2", "cmd-b", "", {"A001": 0.9}, {"A001": [1, 2]}, action="replaced",
                                   chars_total=5000, chars_kept=1000),
                self.shadow_record("t3", "cmd-c", "", {"A001": 0.9}, {"A001": [1, 2]}, action="sampled_shadow",
                                   chars_total=777777, chars_kept=1)]
        self.write_log(recs)
        rep = self.report()
        self.assertIn("15000", rep)
        self.assertIn("3000", rep)
        self.assertRegex(rep, r"\b80(\.0+)?\s*%")
        self.assertRegex(rep, r"\b342[89]\b")  # (15000-3000)/3.5
        self.assertNotIn("777777", rep)

    def test_passthrough_reasons_listed(self):
        self.write_log([{"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session_id": "s",
                         "tool_use_id": "p1", "command": "x", "action": "passthrough",
                         "reason": "judge_unreachable_zz"}])
        self.assertIn("judge_unreachable_zz", self.report())

    def test_days_window(self):
        old = time.gmtime(time.time() - 30 * 86400)
        rec = self.shadow_record("old1", "cmd-old", "", {"A001": 0.9}, {"A001": [1, 2]}, action="replaced",
                                 chars_total=424242, chars_kept=1)
        rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", old)
        self.write_log([rec], day=time.strftime("%Y-%m-%d", old))
        self.assertNotIn("424242", self.report("--days", "7"))
        self.assertIn("424242", self.report("--days", "60"))

    def test_recoveries_and_notes_listed(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.write_log([
            self.shadow_record("r1", "cmd-replaced-orig", "", {"A001": 0.9, "A002": 0.04},
                               {"A001": [1, 5], "A002": [6, 9]}, action="replaced", out_id="feedc0de"),
            {"ts": now, "session_id": "sess-r", "tool_use_id": "r2", "command": "cat /h/out/feedc0de.txt",
             "action": "passthrough", "reason": "recovery_read", "recovered_id": "feedc0de"}])
        with open(os.path.join(self.home, "notes.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now, "id": "feedc0de", "note": "hid the stack frame NOTE-TEXT-QQ",
                                "cwd": "/w", "session_id": "sess-r", "command": "cmd-replaced-orig",
                                "task": "task-text-zz", "matched": True}) + "\n")
        rep = self.report()
        self.assertIn("feedc0de", rep)
        self.assertIn("cat /h/out/feedc0de.txt", rep)
        self.assertIn("NOTE-TEXT-QQ", rep)
        self.assertIn("cmd-replaced-orig", rep)
        self.assertIn("task-text-zz", rep)
        self.assertIn("A002", rep)
        self.assertIn("0.04", rep)

    def test_empty_home_does_not_crash(self):
        rc, out, err = self.run_py(CLI, ["report"])
        self.assertEqual(rc, 0, err)


if __name__ == "__main__":
    unittest.main()

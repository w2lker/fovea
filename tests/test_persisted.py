"""Outputs above BASH_MAX_OUTPUT_LENGTH: Claude Code cuts stdout and saves the full text itself.

Observed live (Claude Code 2.1.319, 7 Nov 2026): tool_response carries persistedOutputPath and
persistedOutputSize, and the file is complete when the hook runs. A replacement that keeps those
fields is wrapped in <persisted-output> with a 2 KB preview; without them it is shown whole.
"""
import os
import unittest

from helpers import HookCase, NEEDLE, KEPT_MARKER, make_output, parse_view

LIMIT = 30000
PERSISTED_KEYS = {"persistedOutputPath", "persistedOutputSize"}


class PersistedOutputTests(HookCase):
    def persisted_payload(self, full, name="b0persist.txt", **kw):
        path = os.path.join(self.tmp, "tool-results", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(full)
        cut = full.encode("utf-8")[:LIMIT].decode("utf-8", "ignore")
        size = len(full.encode("utf-8"))
        return self.payload(stdout=cut, noOutputExpected=False, persistedOutputPath=path,
                            persistedOutputSize=size, **kw), path

    def test_filters_the_full_file_not_the_cut(self):
        full = make_output(n=200, needles=(180,))
        self.assertGreater(full.index(NEEDLE), LIMIT, "the needle must lie beyond the cut")
        p, _ = self.persisted_payload(full)
        r = self.run_hook(p)
        new = r.json()["hookSpecificOutput"]["updatedToolOutput"]
        self.assertFalse(PERSISTED_KEYS & set(new), "persisted fields make Claude Code show a 2 KB preview")
        self.assertIn("noOutputExpected", new)
        self.assertIn(KEPT_MARKER, new["stdout"])
        header, _ = parse_view(new["stdout"])
        self.assertIn("full: sed -n A,Bp", new["stdout"].split("\n")[0])
        log = self.read_log()[-1]
        self.assertEqual(log["action"], "replaced")
        self.assertTrue(log["persisted"])
        self.assertEqual(log["chars_total"], len(full))
        with open(self.out_path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), full)

    def test_missing_file_passes_through(self):
        p, path = self.persisted_payload(make_output(n=200))
        os.remove(path)
        r = self.run_hook(p)
        self.assertEqual(r.stdout, "")
        log = self.read_log()[-1]
        self.assertEqual(log["reason"], "harness_persisted")
        self.assertTrue(log["persisted_unreadable"])

    def test_file_above_the_size_cap_passes_through(self):
        p, _ = self.persisted_payload(make_output(n=200))
        r = self.run_hook(p, FOVEA_MAX_PERSISTED_BYTES="40000")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.read_log()[-1]["reason"], "persisted_too_large")
        self.assertEqual(self.stub.requests, [])

    def test_secret_beyond_the_cut_is_caught(self):
        full = make_output(n=200) + "\n\ntoken: ghp_" + "a" * 30 + "\n\n" + make_output(n=20)
        self.assertGreater(full.index("ghp_"), LIMIT)
        p, _ = self.persisted_payload(full)
        r = self.run_hook(p)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.read_log()[-1]["reason"], "secret_output")
        self.assertEqual(self.stub.requests, [])

    def test_error_in_the_real_tail_is_caught(self):
        full = make_output(n=200) + "\nTraceback (most recent call last)\n  File \"x.py\", line 1\nValueError: boom"
        p, _ = self.persisted_payload(full)
        r = self.run_hook(p)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.read_log()[-1]["reason"], "error_output")

    def test_selection_is_trimmed_to_the_budget(self):
        # ~150 KB; the judge keeps 40% of the blocks, more than fits under the harness limit.
        full = make_output(n=500, needles=(450,))
        self.stub.score = lambda lab, text: 0.9 if NEEDLE in text else (0.5 if int(lab[1:]) % 5 < 2 else 0.0)
        p, _ = self.persisted_payload(full)
        r = self.run_hook(p)
        new = r.json()["hookSpecificOutput"]["updatedToolOutput"]
        self.assertLess(len(new["stdout"].encode("utf-8")), LIMIT - 2000)
        self.assertIn(KEPT_MARKER, new["stdout"], "the highest-scoring block survives trimming")
        parse_view(new["stdout"])
        log = self.read_log()[-1]
        self.assertGreater(log["budget_dropped"], 0)
        self.assertLess(log["chars_kept"], LIMIT)


if __name__ == "__main__":
    unittest.main()

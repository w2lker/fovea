import json
import os
import re
import stat
import subprocess
import unittest

from helpers import (HookCase, NEEDLE, KEPT_MARKER, DROPPED_MARKER, parse_view, json_array_output,
                     make_output, request_blocks)


def merge_ranges(ranges):
    out = []
    for a, b in sorted(ranges):
        if out and out[-1][1] + 1 == a:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


class ReplacementJsonTests(HookCase):
    def test_envelope_and_tool_response_preserved(self):
        p = self.payload(stderr="\nShell cwd was reset to /tmp/x", noOutputExpected=False,
                         customExtra={"a": [1, 2, {"b": None}]})
        r = self.run_hook(p)
        self.assertEqual(r.rc, 0)
        out = r.json()
        self.assertEqual(set(out.keys()), {"hookSpecificOutput"})
        hso = out["hookSpecificOutput"]
        self.assertEqual(set(hso.keys()), {"hookEventName", "updatedToolOutput"})
        self.assertEqual(hso["hookEventName"], "PostToolUse")
        new = hso["updatedToolOutput"]
        old = p["tool_response"]
        self.assertEqual(set(new.keys()), set(old.keys()))
        for k in old:
            if k != "stdout":
                self.assertEqual(new[k], old[k], k)
        self.assertNotEqual(new["stdout"], old["stdout"])
        self.assertLess(len(new["stdout"]), len(old["stdout"]))

    def test_cwd_reset_note_alone_does_not_block_replacement(self):
        p = self.payload(stderr="\nShell cwd was reset to /Users/someone/project")
        r = self.run_hook(p)
        self.assertNotEqual(r.stdout, "")
        self.assertEqual(r.json()["hookSpecificOutput"]["updatedToolOutput"]["stderr"],
                         p["tool_response"]["stderr"])

    def test_empty_stderr_replaces(self):
        r = self.run_hook(self.payload(stderr=""))
        self.assertNotEqual(r.stdout, "")

    def test_zero_errors_line_is_not_an_error(self):
        out = make_output() + "\n\n0 errors, 0 warnings in 3 files"
        r = self.run_hook(self.payload(stdout=out))
        self.assertNotEqual(r.stdout, "")

    def test_word_containing_op_in_command_is_not_secret(self):
        r = self.run_hook(self.payload(command="cat develop-notes.txt"))
        self.assertNotEqual(r.stdout, "")

    def test_short_sk_prefix_is_not_secret(self):
        out = make_output() + "\n\nthe sk-short value"
        r = self.run_hook(self.payload(stdout=out))
        self.assertNotEqual(r.stdout, "")


class RenderingTests(HookCase):
    def _run(self, stdout=None, command="python3 report.py", expect_json=False, **env):
        p = self.payload(stdout=stdout, command=command)
        r = self.run_hook(p, **env)
        self.assertEqual(r.rc, 0)
        self.assertNotEqual(r.stdout, "", "expected a replacement")
        view = r.json()["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
        header, segs = parse_view(view)
        return p, view, header, segs

    def _check_cover(self, header, segs, saved):
        n = len(saved.splitlines())
        nxt = 1
        for s in segs:
            self.assertEqual(s["a"], nxt, "ranges must be contiguous and in order")
            self.assertGreaterEqual(s["b"], s["a"])
            nxt = s["b"] + 1
        self.assertEqual(nxt, n + 1, "ranges must cover lines 1..N exactly")
        slines = saved.splitlines()
        for s in segs:
            if s["type"] == "kept":
                self.assertEqual(s["lines"], slines[s["a"] - 1:s["b"]])
                self.assertEqual("\n".join(s["lines"]), "\n".join(slines[s["a"] - 1:s["b"]]))

    def test_kept_and_skip_cover_saved_file_exactly_once(self):
        p, view, header, segs = self._run()
        saved = open(self.out_path(), encoding="utf-8").read()
        self.assertEqual(saved, p["tool_response"]["stdout"])
        self._check_cover(header, segs, saved)
        kinds = [s["type"] for s in segs]
        self.assertIn("kept", kinds)
        self.assertIn("skip", kinds)
        self.assertNotIn(NEEDLE + "-never", view)
        self.assertIn(KEPT_MARKER, view)
        self.assertNotIn(DROPPED_MARKER, view)

    def test_adjacent_kept_blocks_merge_into_one_range(self):
        self.stub.score = lambda l, t: 0.9 if (NEEDLE in t or "short 035" in t or "paragraph 036" in t) else 0.01
        out = make_output(needles=(34,))
        p, view, header, segs = self._run(stdout=out)
        saved = open(self.out_path(), encoding="utf-8").read()
        self._check_cover(header, segs, saved)
        kept = [s for s in segs if s["type"] == "kept"]
        self.assertEqual(len(kept), 1)
        for a, b in zip(segs, segs[1:]):
            self.assertFalse(a["type"] == b["type"], "adjacent segments of the same type must merge")

    def test_header_counts_and_percentage(self):
        p, view, header, segs = self._run()
        reqs = self.stub.requests
        n_blocks = sum(len(r["body"]["questions"]) for r in reqs)
        self.assertEqual(header["total"], n_blocks)
        scores = {}
        for r in reqs:
            for label, text in request_blocks(r):
                scores[label] = self.stub.score(label, text)
        self.assertEqual(header["kept"], sum(1 for v in scores.values() if v >= 0.2))
        log = self.read_log()[-1]
        self.assertEqual(header["pct"], round(100.0 * log["chars_kept"] / log["chars_total"]))
        self.assertEqual(log["chars_total"], len(p["tool_response"]["stdout"]))
        self.assertLess(header["pct"], 80)
        self.assertEqual(header["days"], 30)

    def test_kept_ranges_equal_merged_block_lines_from_log(self):
        p, view, header, segs = self._run()
        log = self.read_log()[-1]
        bl = log["block_lines"]
        ordered = sorted(bl.items())
        nxt = 1
        for _, (a, b) in ordered:
            self.assertEqual(a, nxt)
            nxt = b + 1
        kept_ranges = merge_ranges([tuple(bl[k]) for k in log["kept"]])
        shown = [(s["a"], s["b"]) for s in segs if s["type"] == "kept"]
        self.assertEqual(shown, kept_ranges)

    def test_retention_days_in_header(self):
        p, view, header, segs = self._run(FOVEA_RETENTION_DAYS="7")
        self.assertEqual(header["days"], 7)

    def test_saved_file_mode_600_and_dir_700(self):
        self._run()
        path = self.out_path()
        self.assertTrue(os.path.exists(path), path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)

    def test_saved_file_name_is_sha1_prefix_of_tool_use_id(self):
        p = self.payload(tool_use_id="toolu_custom_id_42")
        self.run_hook(p)
        self.assertTrue(os.path.exists(self.out_path("toolu_custom_id_42")))
        files = os.listdir(os.path.join(self.home, "out"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^[0-9a-f]{8}\.txt$")

    def test_header_path_literal_when_outside_home_and_cat_resolves(self):
        p, view, header, segs = self._run()
        self.assertFalse(header["path"].startswith("~"))
        self.assertEqual(os.path.realpath(header["path"]), os.path.realpath(self.out_path()))
        cat = subprocess.run(["sh", "-c", "cat " + header["path"]], capture_output=True,
                             env={"HOME": self.userhome, "PATH": os.environ["PATH"]})
        self.assertEqual(cat.stdout.decode("utf-8"), p["tool_response"]["stdout"])

    def test_header_path_uses_tilde_when_under_home_and_cat_resolves(self):
        home_dir = self.tmp  # make FOVEA_HOME live under HOME
        fh = os.path.join(home_dir, ".fovea-under-home")
        p = self.payload()
        r = self.run_hook(p, HOME=home_dir, FOVEA_HOME=fh)
        view = r.json()["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
        header, segs = parse_view(view)
        self.assertTrue(header["path"].startswith("~/"), header["path"])
        self.assertEqual(header["path"], "~/.fovea-under-home/out/" + os.path.basename(
            self.out_path()))
        cat = subprocess.run(["sh", "-c", "cat " + header["path"]], capture_output=True,
                             env={"HOME": home_dir, "PATH": os.environ["PATH"]})
        self.assertEqual(cat.stdout.decode("utf-8"), p["tool_response"]["stdout"])

    def test_default_fovea_home_is_dot_fovea_under_home(self):
        p = self.payload()
        r = self.run_hook(p, FOVEA_HOME=None)
        self.assertNotEqual(r.stdout, "")
        out_dir = os.path.join(self.userhome, ".fovea", "out")
        self.assertTrue(os.path.isdir(out_dir))
        self.assertEqual(len(os.listdir(out_dir)), 1)

    def test_skip_hints_format(self):
        p, view, header, segs = self._run()
        log = self.read_log()[-1]
        saved = open(self.out_path(), encoding="utf-8").read().splitlines()
        blocks = sorted(log["block_lines"].values())
        checked = 0
        for s in segs:
            if s["type"] != "skip":
                continue
            inside = [(a, b) for a, b in blocks if a >= s["a"] and b <= s["b"]]
            self.assertTrue(inside)
            items = s["hint"].split("; ")
            shown = items[:3]
            for (a, b), item in zip(inside, shown):
                first = next(l for l in saved[a - 1:b] if l.strip()).strip()
                expect = first if len(first) <= 24 else first[:24] + "…"
                self.assertEqual(item, expect)
            if len(inside) > 3:
                self.assertEqual(items[3], "+%d" % (len(inside) - 3))
                self.assertEqual(len(items), 4)
            else:
                self.assertEqual(len(items), len(inside))
            checked += 1
        self.assertGreaterEqual(checked, 1)

    def test_json_output_saved_pretty_printed_and_ranges_match(self):
        raw = json_array_output()
        p, view, header, segs = self._run(stdout=raw, command="curl -s https://api.example.com/items")
        saved = open(self.out_path(), encoding="utf-8").read()
        self.assertEqual(saved.rstrip("\n"), json.dumps(json.loads(raw), indent=1))
        self._check_cover(header, segs, saved)
        self.assertIn(NEEDLE, view)

    def test_large_output_goes_as_parallel_requests_and_still_covers(self):
        out = make_output(n=900, needles=(450,))
        self.assertGreater(len(out), 200000)
        # Only reachable when the user raises Claude Code's Bash output limit.
        p, view, header, segs = self._run(stdout=out, BASH_MAX_OUTPUT_LENGTH="1000000")
        for r in self.stub.requests:
            self.assertLessEqual(len(r["body"]["state"]), 100000 + 200)
        self.assertGreaterEqual(len(self.stub.requests), 2)
        saved = open(self.out_path(), encoding="utf-8").read()
        self.assertEqual(saved, out)
        self._check_cover(header, segs, saved)
        self.assertIn(NEEDLE, view)


if __name__ == "__main__":
    unittest.main()

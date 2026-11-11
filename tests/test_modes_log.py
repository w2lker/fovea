import json
import os
import re
import unittest

from helpers import HookCase, KEPT_MARKER, DROPPED_MARKER, API_KEY, NEEDLE, make_output, request_blocks

LOG_FIELDS = ["ts", "session_id", "tool_use_id", "cwd", "command", "task", "mode", "version", "config",
              "action", "reason",
              "splitter", "n_blocks", "block_lines", "scores", "kept", "chars_total", "chars_kept",
              "latency_ms", "cost", "out_path", "error"]


class ModeTests(HookCase):
    def test_shadow_never_replaces_and_logs(self):
        p = self.payload()
        r = self.run_hook(p, FOVEA_MODE="shadow")
        self.assertEqual((r.rc, r.stdout), (0, ""))
        self.assertEqual(len(self.stub.requests), 1)
        lines = self.read_log()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["action"], "shadow")
        self.assertEqual(lines[0]["mode"], "shadow")
        self.assertTrue(lines[0]["scores"])
        self.assertTrue(lines[0]["kept"])

    def test_shadow_ignores_sample_setting(self):
        for s in ("0", "1"):
            r = self.run_hook(self.payload(), FOVEA_MODE="shadow", FOVEA_SHADOW_SAMPLE=s)
            self.assertEqual(r.stdout, "")
        self.assertEqual([l["action"] for l in self.read_log()], ["shadow", "shadow"])

    def test_active_sample_1_never_replaces(self):
        r = self.run_hook(self.payload(), FOVEA_MODE="active", FOVEA_SHADOW_SAMPLE="1")
        self.assertEqual((r.rc, r.stdout), (0, ""))
        lines = self.read_log()
        self.assertEqual(lines[-1]["action"], "sampled_shadow")
        self.assertEqual(lines[-1]["mode"], "active")
        self.assertTrue(lines[-1]["scores"])

    def test_active_sample_0_replaces_when_eligible(self):
        r = self.run_hook(self.payload(), FOVEA_MODE="active", FOVEA_SHADOW_SAMPLE="0")
        self.assertNotEqual(r.stdout, "")
        self.assertEqual(self.read_log()[-1]["action"], "replaced")

    def test_default_mode_is_active(self):
        r = self.run_hook(self.payload(), FOVEA_MODE=None)
        self.assertNotEqual(r.stdout, "")
        self.assertEqual(self.read_log()[-1]["mode"], "active")

    def test_sample_between_is_random_share(self):
        n = 40
        replaced = 0
        for i in range(n):
            r = self.run_hook(self.payload(tool_use_id="toolu_s%d" % i), FOVEA_SHADOW_SAMPLE="0.5")
            replaced += 1 if r.stdout else 0
        self.assertGreater(replaced, 3)
        self.assertLess(replaced, n - 3)
        actions = {l["action"] for l in self.read_log()}
        self.assertEqual(actions, {"replaced", "sampled_shadow"})


class LogTests(HookCase):
    def test_log_fields_on_replaced_call(self):
        cmd = "python3 report.py --verbose " + "q" * 300
        p = self.payload(command=cmd)
        r = self.run_hook(p)
        self.assertNotEqual(r.stdout, "")
        files = os.listdir(os.path.join(self.home, "log"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^\d{4}-\d{2}-\d{2}\.jsonl$")
        lines = self.read_log()
        self.assertEqual(len(lines), 1)
        L = lines[0]
        for f in LOG_FIELDS:
            self.assertIn(f, L, f)
        self.assertEqual(L["session_id"], p["session_id"])
        self.assertEqual(L["tool_use_id"], p["tool_use_id"])
        self.assertEqual(L["cwd"], p["cwd"])
        self.assertEqual(L["command"], cmd[:200])
        self.assertLessEqual(len(L["task"]), 300)
        self.assertTrue(L["task"])
        self.assertEqual(L["mode"], "active")
        self.assertRegex(L["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(sorted(L["config"]), ["min_chars", "min_top", "model", "shadow_sample", "threshold"])
        self.assertEqual(L["action"], "replaced")
        self.assertTrue(L["splitter"])
        req = self.stub.requests[0]
        labels = [l for l, _ in request_blocks(req)]
        self.assertEqual(L["n_blocks"], len(labels))
        self.assertEqual(sorted(L["block_lines"]), labels)
        self.assertEqual(sorted(L["scores"]), labels)
        for lab in L["kept"]:
            self.assertGreaterEqual(L["scores"][lab], 0.2)
        self.assertEqual(sorted(L["kept"]), sorted(l for l, v in L["scores"].items() if v >= 0.2))
        self.assertGreater(L["chars_total"], 0)
        self.assertGreater(L["chars_kept"], 0)
        self.assertLess(L["chars_kept"], L["chars_total"])
        self.assertIsInstance(L["latency_ms"], (int, float))
        self.assertEqual(os.path.realpath(os.path.expanduser(L["out_path"])), os.path.realpath(self.out_path()))
        self.assertTrue(re.match(r"^\d{4}-\d{2}-\d{2}", str(L["ts"])) or isinstance(L["ts"], (int, float)))

    def test_log_never_contains_output_text(self):
        for kw in ({}, {"FOVEA_MODE": "shadow"}, {"FOVEA_SHADOW_SAMPLE": "1"}):
            self.run_hook(self.payload(tool_use_id="toolu_%d" % len(kw)), **kw)
        text = self.log_text()
        self.assertTrue(text)
        for needle in (KEPT_MARKER, DROPPED_MARKER, NEEDLE + " lorem", "lorem ipsum dolor sit amet lorem"):
            self.assertNotIn(needle, text)
        self.assertNotIn("second line of paragraph", text)

    def test_log_never_contains_output_text_even_for_secret_output(self):
        secret = "sk-" + "Zq9" * 10
        out = make_output() + "\n\nvalue " + secret + " MARKER-SECRET-LINE"
        self.run_hook(self.payload(stdout=out))
        text = self.log_text()
        self.assertTrue(text)
        self.assertNotIn(secret, text)
        self.assertNotIn("MARKER-SECRET-LINE", text)
        self.assertNotIn(DROPPED_MARKER, text)

    def test_log_never_contains_api_key(self):
        self.run_hook(self.payload())
        self.assertNotIn(API_KEY, self.log_text())

    def test_passthrough_logged_with_reason(self):
        self.run_hook(self.payload(), FOVEA_MODE="off")
        self.run_hook(self.payload(command="FOVEA_BYPASS=1 python3 x.py"))
        lines = self.read_log()
        self.assertEqual(len(lines), 2)
        for L in lines:
            self.assertEqual(L["action"], "passthrough")
            self.assertTrue(L["reason"])
        self.assertNotEqual(lines[0]["reason"], lines[1]["reason"])

    def test_judge_failure_records_error_field(self):
        self.stub.status = 500
        self.run_hook(self.payload())
        L = self.read_log()[-1]
        self.assertEqual(L["action"], "passthrough")
        self.assertTrue(L["error"])

    def test_log_is_append_only_one_line_per_call(self):
        for i in range(3):
            self.run_hook(self.payload(tool_use_id="toolu_%d" % i))
        lines = self.read_log()
        self.assertEqual([l["tool_use_id"] for l in lines], ["toolu_0", "toolu_1", "toolu_2"])

    def test_command_truncated_to_200_in_log(self):
        cmd = "python3 report.py " + "w" * 500
        self.run_hook(self.payload(command=cmd), FOVEA_MODE="shadow")
        self.assertEqual(len(self.read_log()[0]["command"]), 200)


if __name__ == "__main__":
    unittest.main()

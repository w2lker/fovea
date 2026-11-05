"""Fovea v1.1 contract tests: header id, recovery reads, `fovea note`, SessionStart.

Derived only from docs/DESIGN.md ("v1.1: feedback and report" and the v1 amendments).
"""
import json
import os
import re
import subprocess
import sys
import time
import unittest

from helpers import HookCase, ROOT, make_output

CLI = os.path.join(ROOT, "fovea", "cli.py")
SESSION_START = os.path.join(ROOT, "fovea", "session_start.py")
NEW_HEADER_RE = re.compile(r"^\[fovea ([0-9a-f]+): (\d+)/(\d+) blocks, (\d+)% · full: cat (.+?) · kept (\d+)d\]$")


def today():
    return time.strftime("%Y-%m-%d", time.gmtime())


class V11Base(HookCase):
    def run_py(self, script, args=(), stdin="", **env_over):
        p = subprocess.run([sys.executable, script] + list(args), input=stdin.encode("utf-8"),
                           capture_output=True, env=self.env(**env_over), cwd=self.tmp, timeout=60)
        return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8", "replace")

    def write_log(self, records, day=None):
        d = os.path.join(self.home, "log")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, (day or today()) + ".jsonl"), "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def notes(self):
        path = os.path.join(self.home, "notes.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]


class HeaderTests(V11Base):
    def test_header_carries_output_id_and_log_fields(self):
        p, r = self.replace_run()
        self.assertEqual(r.rc, 0)
        first = r.json()["hookSpecificOutput"]["updatedToolOutput"]["stdout"].split("\n")[0]
        m = NEW_HEADER_RE.match(first)
        self.assertTrue(m, "bad header: %r" % first)
        out_file = self.out_path()
        self.assertTrue(os.path.exists(out_file))
        out_id = os.path.basename(out_file)[:-len(".txt")]
        self.assertTrue(first.startswith("[fovea %s: " % out_id))
        self.assertIn(out_id + ".txt", first)
        rec = self.read_log()[-1]
        self.assertEqual(rec["action"], "replaced")
        self.assertEqual(rec["out_id"], out_id)
        self.assertEqual(rec["transcript_path"], p["transcript_path"])

    def test_passthrough_log_has_transcript_path(self):
        p = self.payload(stdout=make_output(), command="python3 x.py")
        self.run_hook(p, FOVEA_MODE="shadow")
        rec = self.read_log()[-1]
        self.assertEqual(rec["transcript_path"], p["transcript_path"])


class RecoveryTests(V11Base):
    ID = "abcd1234"

    def recovery(self, command):
        p = self.payload(stdout=make_output(), command=command)
        r = self.run_hook(p)
        self.assertEqual((r.rc, r.stdout), (0, ""))
        self.assertEqual(len(self.stub.requests), 0)
        rec = self.read_log()[-1]
        self.assertEqual(rec["action"], "passthrough")
        self.assertEqual(rec["reason"], "recovery_read")
        self.assertEqual(rec["recovered_id"], self.ID)

    def test_cat_absolute_path(self):
        self.recovery("cat %s/out/%s.txt" % (self.home, self.ID))

    def test_cat_tilde_path(self):
        self.home = os.path.join(self.userhome, ".fovea")
        self.recovery("cat ~/.fovea/out/%s.txt" % self.ID)

    def test_real_roundtrip_id(self):
        p, r = self.replace_run()
        out_id = os.path.basename(self.out_path())[:-4]
        p2 = self.payload(stdout=make_output(), command="cat %s" % self.out_path(), tool_use_id="toolu_other")
        r2 = self.run_hook(p2)
        self.assertEqual(r2.stdout, "")
        rec = self.read_log()[-1]
        self.assertEqual(rec["reason"], "recovery_read")
        self.assertEqual(rec["recovered_id"], out_id)


class NoteTests(V11Base):
    def seed(self):
        self.write_log([{"ts": time.time(), "session_id": "sess-1", "tool_use_id": "toolu_x",
                         "cwd": "/work", "command": "git log --stat", "task": "find the bad commit",
                         "mode": "active", "action": "replaced", "reason": "", "n_blocks": 5,
                         "scores": {"A001": 0.9, "A002": 0.05}, "kept": ["A001"],
                         "out_id": "deadbeef", "out_path": os.path.join(self.home, "out", "deadbeef.txt")}])

    def test_known_id(self):
        self.seed()
        rc, out, err = self.run_py(CLI, ["note", "deadbeef", "missed the merge commit"])
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.strip())
        self.assertLessEqual(len(out.strip().splitlines()), 2)
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        n = notes[0]
        self.assertEqual(n["id"], "deadbeef")
        self.assertEqual(n["note"], "missed the merge commit")
        self.assertEqual(n["session_id"], "sess-1")
        self.assertEqual(n["command"], "git log --stat")
        self.assertEqual(n["task"], "find the bad commit")
        self.assertIn("ts", n)
        self.assertIn("cwd", n)
        self.assertIsNot(n.get("matched"), False)

    def test_notes_append(self):
        self.seed()
        self.run_py(CLI, ["note", "deadbeef", "one"])
        self.run_py(CLI, ["note", "deadbeef", "two"])
        self.assertEqual([n["note"] for n in self.notes()], ["one", "two"])

    def test_unknown_id_still_saved(self):
        self.seed()
        rc, out, err = self.run_py(CLI, ["note", "00000000", "something odd"])
        self.assertEqual(rc, 0, err)
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["id"], "00000000")
        self.assertEqual(notes[0]["note"], "something odd")
        self.assertIs(notes[0]["matched"], False)
        self.assertNotIn("command", {k for k, v in notes[0].items() if v})
        self.assertTrue(out.strip())

    def test_missing_text_exits_2(self):
        self.seed()
        rc, out, err = self.run_py(CLI, ["note", "deadbeef"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.notes(), [])
        self.assertFalse(os.path.exists(os.path.join(self.home, "notes.jsonl")) and self.notes())

    def test_no_args_exits_2(self):
        rc, out, err = self.run_py(CLI, ["note"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.notes(), [])
        rc, out, err = self.run_py(CLI, [])
        self.assertEqual(rc, 2)


class SessionStartTests(V11Base):
    STDIN = json.dumps({"hook_event_name": "SessionStart", "session_id": "s", "source": "startup"})

    def test_prints_additional_context(self):
        rc, out, err = self.run_py(SESSION_START, stdin=self.STDIN)
        self.assertEqual(rc, 0, err)
        o = json.loads(out)
        hso = o["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SessionStart")
        self.assertIn("fovea note", hso["additionalContext"])
        self.assertIn("[fovea", hso["additionalContext"])

    def test_mode_off_prints_nothing(self):
        rc, out, err = self.run_py(SESSION_START, stdin=self.STDIN, FOVEA_MODE="off")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_shadow_mode_still_prints(self):
        rc, out, err = self.run_py(SESSION_START, stdin=self.STDIN, FOVEA_MODE="shadow")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["hookEventName"], "SessionStart")


if __name__ == "__main__":
    unittest.main()

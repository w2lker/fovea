import json
import re
import unittest

from helpers import (HookCase, STATE_PREFIX, NEEDLE, API_KEY, parse_state, make_output, request_blocks)

Q_RE = re.compile(r"^The coding agent's current task: (.*)\nDoes block (A\d{3,4}) \(the text between "
                  r"\[(A\d{3,4})\] and \[/(A\d{3,4})\]\) contain information the agent needs for this task\?$",
                  re.S)


def user_rec(content, **extra):
    r = {"type": "user", "message": {"role": "user", "content": content}}
    r.update(extra)
    return r


def asst_rec(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class JudgeRequestTests(HookCase):
    def go(self, **kw):
        p = self.payload(**kw)
        r = self.run_hook(p)
        self.assertEqual(r.rc, 0)
        self.assertEqual(len(self.stub.requests), 1)
        return p, self.stub.requests[0]

    def tasks(self, req):
        out = []
        for q in req["body"]["questions"].values():
            m = Q_RE.match(q["instructions"])
            self.assertIsNotNone(m, q["instructions"][:300])
            out.append(m.group(1))
        return out

    def test_request_shape_and_headers(self):
        p, req = self.go()
        self.assertEqual(req["headers"]["authorization"], "Bearer " + API_KEY)
        body = req["body"]
        self.assertEqual(set(body.keys()), {"model", "state", "questions"})
        self.assertEqual(body["model"], "~typesafe/jev-latest")

    def test_model_is_configurable(self):
        self.run_hook(self.payload(), FOVEA_MODEL="other/model-x")
        self.assertEqual(self.stub.requests[0]["body"]["model"], "other/model-x")

    def test_state_format_and_labels(self):
        p, req = self.go()
        state = req["body"]["state"]
        self.assertTrue(state.startswith(STATE_PREFIX))
        blocks = parse_state(state)
        self.assertGreater(len(blocks), 10)
        rebuilt = STATE_PREFIX + "\n\n".join("[%s]\n%s\n[/%s]" % (l, t, l) for l, t in blocks)
        self.assertEqual(state, rebuilt)
        self.assertEqual([l for l, _ in blocks], ["A%03d" % (i + 1) for i in range(len(blocks))])

    def test_one_question_per_block_with_matching_labels(self):
        p, req = self.go()
        blocks = parse_state(req["body"]["state"])
        qs = req["body"]["questions"]
        self.assertIsInstance(qs, dict)
        self.assertEqual(set(qs), {label for label, _ in blocks})
        for label, _ in blocks:
            q = qs[label]
            self.assertEqual(q["type"], "noul")
            m = Q_RE.match(q["instructions"])
            self.assertIsNotNone(m)
            self.assertEqual({m.group(2), m.group(3), m.group(4)}, {label})

    def test_blocks_reassemble_the_output(self):
        p, req = self.go()
        joined = "\n".join(t for _, t in parse_state(req["body"]["state"]))
        orig = p["tool_response"]["stdout"]
        norm = lambda s: re.sub(r"\s+", " ", s).strip()
        self.assertEqual(norm(joined), norm(orig))

    def test_task_repeated_identically_in_every_question(self):
        p, req = self.go()
        tasks = self.tasks(req)
        self.assertEqual(len(set(tasks)), 1)
        self.assertTrue(tasks[0].strip())

    def test_description_command_assistant_user_in_order(self):
        recs = [
            user_rec("USERMSG-ALPHA please investigate"),
            asst_rec("ASSISTTEXT-BETA looking now"),
        ]
        p, req = self.go(command="python3 report.py --flag CMDMARK", description="DESCMARK what this does",
                         transcript_path=self.write_transcript(recs))
        task = self.tasks(req)[0]
        idx = [task.index(s) for s in ("DESCMARK", "CMDMARK", "ASSISTTEXT-BETA", "USERMSG-ALPHA")]
        self.assertEqual(idx, sorted(idx))

    def test_description_is_part_of_task_in_every_question(self):
        p, req = self.go(description="UNIQUE-DESCRIPTION-ZZ list the widgets")
        for t in self.tasks(req):
            self.assertIn("UNIQUE-DESCRIPTION-ZZ", t)

    def test_system_reminder_only_user_record_is_not_the_user_message(self):
        p, req = self.go()  # default transcript ends with a system-reminder-only record
        task = self.tasks(req)[0]
        self.assertNotIn("SYSREMINDER-ONLY-TEXT", task)
        self.assertNotIn("system-reminder", task)
        self.assertIn("Find out why the report script prints so much output", task)

    def test_latest_assistant_text_from_default_transcript(self):
        p, req = self.go()
        self.assertIn("I will run the report script", self.tasks(req)[0])

    def test_synthetic_user_records_are_skipped(self):
        recs = [
            user_rec("REAL-USER-REQUEST fix the parser"),
            asst_rec("working on it"),
            user_rec([{"type": "tool_result", "tool_use_id": "t1", "content": "TOOLRESULT-TEXT"}]),
            user_rec("hidden meta text META-TEXT", isMeta=True),
            user_rec("<task-notification>TASKNOTIF-TEXT</task-notification>"),
            user_rec("<local-command-stdout>LOCALCMD-TEXT</local-command-stdout>"),
            user_rec("<command-name>/foo</command-name> COMMANDNAME-TEXT"),
            user_rec([{"type": "text", "text": "<system-reminder>SR-BLOCK-TEXT</system-reminder>"}]),
        ]
        p, req = self.go(transcript_path=self.write_transcript(recs))
        task = self.tasks(req)[0]
        self.assertIn("REAL-USER-REQUEST", task)
        for bad in ("TOOLRESULT-TEXT", "META-TEXT", "TASKNOTIF-TEXT", "LOCALCMD-TEXT", "COMMANDNAME-TEXT",
                    "SR-BLOCK-TEXT"):
            self.assertNotIn(bad, task)

    def test_user_message_as_text_block_list(self):
        recs = [user_rec([{"type": "text", "text": "LISTFORM-USER-MESSAGE do the thing"}]), asst_rec("ok")]
        p, req = self.go(transcript_path=self.write_transcript(recs))
        self.assertIn("LISTFORM-USER-MESSAGE", self.tasks(req)[0])

    def test_latest_messages_win(self):
        recs = [user_rec("OLD-USER-ASK"), asst_rec("OLD-ASSISTANT"), user_rec("NEW-USER-ASK"),
                asst_rec("NEW-ASSISTANT")]
        p, req = self.go(transcript_path=self.write_transcript(recs))
        task = self.tasks(req)[0]
        self.assertIn("NEW-USER-ASK", task)
        self.assertIn("NEW-ASSISTANT", task)
        self.assertNotIn("OLD-USER-ASK", task)
        self.assertNotIn("OLD-ASSISTANT", task)

    def test_truncation_limits(self):
        recs = [user_rec("U" * 600 + "USERTAIL" + "u" * 400), asst_rec("A" * 600 + "ASSTTAIL" + "a" * 400)]
        cmd = "echo " + "c" * 300 + "CMDTAIL" + "d" * 100
        p, req = self.go(command=cmd, transcript_path=self.write_transcript(recs))
        task = self.tasks(req)[0]
        self.assertIn("U" * 500, task)
        self.assertNotIn("USERTAIL", task)
        self.assertIn("A" * 500, task)
        self.assertNotIn("ASSTTAIL", task)
        self.assertIn("c" * 200, task)
        self.assertNotIn("CMDTAIL", task)

    def test_missing_transcript_falls_back_to_description_and_command(self):
        p, req = self.go(command="python3 report.py --only-cmd", description="ONLYDESC fallback",
                         transcript_path=self.tmp + "/does-not-exist.jsonl")
        task = self.tasks(req)[0]
        self.assertIn("ONLYDESC", task)
        self.assertIn("--only-cmd", task)

    def test_no_description_still_uses_command(self):
        p, req = self.go(command="python3 report.py --nodesc", description=None)
        self.assertIn("--nodesc", self.tasks(req)[0])

    def test_truncated_transcript_last_line_is_tolerated(self):
        path = self.write_transcript([user_rec("PARTIAL-OK user ask"), asst_rec("fine")])
        with open(path, "a") as f:
            f.write('{"type": "assistant", "message": {"rol')
        p, req = self.go(transcript_path=path)
        self.assertIn("PARTIAL-OK", self.tasks(req)[0])

    def test_only_transcript_tail_is_needed(self):
        recs = [user_rec("FIRST-OLD-ASK")] + [asst_rec("filler %d " % i + "z" * 500) for i in range(3000)] + [
            user_rec("LATEST-ASK-AFTER-HUGE-HISTORY"), asst_rec("latest reply")]
        p, req = self.go(transcript_path=self.write_transcript(recs))
        task = self.tasks(req)[0]
        self.assertIn("LATEST-ASK-AFTER-HUGE-HISTORY", task)


if __name__ == "__main__":
    unittest.main()

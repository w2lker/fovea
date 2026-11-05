import os
import socket
import time
import unittest

from helpers import HookCase, make_output, load_fixture, NEEDLE, LOREM


class PassBase(HookCase):
    def assert_pass(self, payload, expect_request=False, expect_log=True, **env):
        r = self.run_hook(payload, **env)
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.stdout, "")
        if not expect_request:
            self.assertEqual(len(self.stub.requests), 0, "judge must not be called")
        if expect_log:
            lines = self.read_log()
            self.assertTrue(lines, "a passthrough must be logged")
            self.assertEqual(lines[-1]["action"], "passthrough")
            self.assertTrue(lines[-1]["reason"])
        return r


class SimplePassTests(PassBase):
    def test_real_fixture_small_output(self):
        p = load_fixture()
        r = self.run_hook(p)
        self.assertEqual((r.rc, r.stdout), (0, ""))
        self.assertEqual(len(self.stub.requests), 0)

    def test_small_output(self):
        self.assert_pass(self.payload(stdout="x\n" * 1000))  # 2000 chars

    def test_just_below_min_chars(self):
        self.assert_pass(self.payload(stdout=("a" * 99 + "\n") * 34 + "a" * 99))  # 3499 chars

    def test_exactly_min_chars_reaches_judge(self):
        out = make_output(n=12, needles=(5,))
        out = (out + "\n\n" + "tail " * 1000)[:3500]
        self.assertEqual(len(out), 3500)
        self.run_hook(self.payload(stdout=out))
        self.assertGreaterEqual(len(self.stub.requests), 1)

    def test_min_chars_is_configurable(self):
        self.run_hook(self.payload(stdout=make_output(n=12, needles=(5,))), FOVEA_MIN_CHARS="100000")
        self.assertEqual(len(self.stub.requests), 0)
        self.run_hook(self.payload(stdout="word " * 100), FOVEA_MIN_CHARS="100")
        self.assertGreaterEqual(len(self.stub.requests), 1)

    def test_mode_off(self):
        self.assert_pass(self.payload(), FOVEA_MODE="off")

    def test_no_api_key(self):
        self.assert_pass(self.payload(), OPENROUTER_API_KEY=None)

    def test_empty_api_key(self):
        self.assert_pass(self.payload(), OPENROUTER_API_KEY="")

    def test_custom_key_env_name_missing(self):
        self.assert_pass(self.payload(), FOVEA_API_KEY_ENV="MY_JEV_KEY")

    def test_custom_key_env_name_used(self):
        r = self.run_hook(self.payload(), FOVEA_API_KEY_ENV="MY_JEV_KEY", MY_JEV_KEY="custom-secret-123",
                          OPENROUTER_API_KEY=None)
        self.assertNotEqual(r.stdout, "")
        self.assertEqual(self.stub.requests[0]["headers"]["authorization"], "Bearer custom-secret-123")

    def test_not_bash(self):
        p = self.payload()
        p["tool_name"] = "Read"
        r = self.run_hook(p)
        self.assertEqual((r.rc, r.stdout), (0, ""))
        self.assertEqual(len(self.stub.requests), 0)

    def test_interrupted(self):
        self.assert_pass(self.payload(interrupted=True))

    def test_is_image(self):
        self.assert_pass(self.payload(isImage=True))

    def test_stderr_non_empty(self):
        self.assert_pass(self.payload(stderr="warning: something happened"))

    def test_stderr_with_cwd_note_plus_real_stderr(self):
        self.assert_pass(self.payload(stderr="warning: x\nShell cwd was reset to /tmp/a"))

    def test_bypass(self):
        self.assert_pass(self.payload(command="FOVEA_BYPASS=1 python3 report.py"))

    def test_bypass_inside_longer_command(self):
        self.assert_pass(self.payload(command="cd /tmp && FOVEA_BYPASS=1 cat big.txt"))

    def test_recovery_read_fovea_home_var(self):
        self.assert_pass(self.payload(command="cat $FOVEA_HOME/out/abcd1234.txt"))

    def test_recovery_read_tilde_fovea(self):
        self.assert_pass(self.payload(command="cat ~/.fovea/out/abcd1234.txt"))

    def test_recovery_read_sed_range_of_saved_file(self):
        self.assert_pass(self.payload(command="sed -n '10,40p' ~/.fovea/out/abcd1234.txt"))

    def test_malformed_stdin(self):
        r = self.run_hook(None, stdin="this is not json{")
        self.assertEqual((r.rc, r.stdout), (0, ""))

    def test_empty_stdin(self):
        r = self.run_hook(None, stdin="")
        self.assertEqual((r.rc, r.stdout), (0, ""))

    def test_json_without_expected_fields(self):
        r = self.run_hook(None, stdin='{"hook_event_name": "PostToolUse"}')
        self.assertEqual((r.rc, r.stdout), (0, ""))

    def test_missing_transcript_still_works_but_does_not_crash(self):
        p = self.payload(transcript_path=os.path.join(self.tmp, "nope.jsonl"))
        r = self.run_hook(p)
        self.assertEqual(r.rc, 0)
        self.assertNotEqual(r.stdout, "")

    def test_non_eligible_returns_quickly(self):
        best = min(self.run_hook(self.payload(stdout="tiny")).elapsed for _ in range(3))
        self.assertLess(best, 0.5)


def _make_error_test(line):
    def test(self):
        out = make_output(n=20, needles=(10,)) + "\n\n" + line + "\nmore text after"
        self.assert_pass(self.payload(stdout=out))
    return test


ERROR_LINES = {
    "traceback": "Traceback (most recent call last):",
    "error_colon_indented": "   error: cannot find module",
    "error_capital_colon": "Error: ENOENT no such file",
    "error_upper_bracket": "ERROR[build] step failed",
    "failed": "tests/test_x.py::test_a FAILED",
    "panicked": "thread 'main' panicked at src/main.rs:10:5",
    "fatal": "fatal: not a git repository",
    "exception": "java.lang.IllegalStateException: boom",
}
for _n, _l in ERROR_LINES.items():
    setattr(SimplePassTests, "test_error_pattern_" + _n, _make_error_test(_l))


class ErrorFalsePositiveTests(HookCase):
    """Error detection looks at the last 30 lines and at message shape only."""

    def _filtered(self, stdout):
        self.stub.score = lambda label, text: 0.9 if NEEDLE in text else 0.01
        r = self.run_hook(self.payload(stdout=stdout))
        self.assertTrue(r.stdout.strip(), "expected the output to be filtered, got pass-through")

    def test_bare_word_in_commit_body_does_not_block(self):
        body = make_output()
        lines = body.splitlines()
        lines.insert(len(lines) // 2, "      error or unload ends alert at once")
        lines.append("Error something went wrong")
        self._filtered("\n".join(lines) + "\n")

    def test_error_far_from_the_end_does_not_block(self):
        self._filtered("error: early warning-like line\n" + make_output())


def _make_secret_cmd_test(cmd):
    def test(self):
        self.assert_pass(self.payload(command=cmd))
    return test


SECRET_COMMANDS = {
    "op": "op read op://Dotfiles/item/field",
    "security": "security find-generic-password -s foo -w",
    "printenv": "printenv",
    "printenv_var": "printenv HOME",
    "env_bare": "env",
    "env_pipe": "env | sort",
    "gh_auth_token": "gh auth token",
    "dot_env_path": "cat config/.env",
    "ssh_dir": "cat ~/.ssh/config",
    "pem": "cat server.pem",
    "id_rsa": "cat ~/.ssh/id_rsa",
    "id_ed25519": "cat id_ed25519",
    "keychain": "ls ~/Library/Keychains/login.keychain-db",
    "after_cd": "cd /tmp/x && op item list",
    "after_sudo_or_env_assignment": "FOO=1 printenv",
}
for _n, _c in SECRET_COMMANDS.items():
    setattr(SimplePassTests, "test_secret_command_" + _n, _make_secret_cmd_test(_c))


def _make_secret_out_test(secret):
    def test(self):
        out = make_output(n=20, needles=(10,)) + "\n\nconfig value: " + secret + " end"
        self.assert_pass(self.payload(stdout=out))
    return test


SECRET_OUTPUTS = {
    "sk": "sk-" + "a1B2c3D4" * 3,
    "ghp": "ghp_" + "Ab12" * 6,
    "github_pat": "github_pat_11ABCDEFG0abcdefghij",
    "akia": "AKIA" + "ABCDEFGH12345678",
    "xoxb": "xoxb-123456789-abcdefghij",
    "xoxa": "xoxa-123456789-abcdefghij",
    "xoxp": "xoxp-123456789-abcdefghij",
    "rsa_private": "-----BEGIN RSA PRIVATE KEY-----",
    "plain_private": "-----BEGIN PRIVATE KEY-----",
    "openssh_private": "-----BEGIN OPENSSH PRIVATE KEY-----",
}
for _n, _s in SECRET_OUTPUTS.items():
    setattr(SimplePassTests, "test_secret_output_" + _n, _make_secret_out_test(_s))


def _make_range_test(cmd):
    def test(self):
        self.assert_pass(self.payload(command=cmd))
    return test


RANGE_COMMANDS = {
    "sed_n": "sed -n 10,50p big.log",
    "sed_n_quoted": "sed -n '1,200p' src/main.py",
    "pipe_head": "cat big.log | head -n 100",
    "pipe_head_bare": "grep foo big.log | head",
    "pipe_tail": "cat big.log | tail -n 100",
    "git_log_tail": "git log | tail -5",
}
for _n, _c in RANGE_COMMANDS.items():
    setattr(SimplePassTests, "test_agent_chose_range_" + _n, _make_range_test(_c))


class JudgePassTests(PassBase):
    def test_judge_timeout(self):
        self.stub.delay = 3
        r = self.assert_pass(self.payload(), expect_request=True, FOVEA_TIMEOUT="0.5")
        self.assertLess(r.elapsed, 3.0)
        self.assertEqual(len(self.stub.requests), 1)

    def test_judge_http_500(self):
        self.stub.status = 500
        self.assert_pass(self.payload(), expect_request=True)

    def test_judge_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assert_pass(self.payload(), FOVEA_BASE_URL="http://127.0.0.1:%d/x" % port)

    def test_malformed_answer_not_json(self):
        self.stub.raw_reply = "<html>not json</html>"
        self.assert_pass(self.payload(), expect_request=True)

    def test_malformed_answer_no_answers_key(self):
        self.stub.raw_reply = '{"result": "ok"}'
        self.assert_pass(self.payload(), expect_request=True)

    def test_malformed_answer_missing_labels(self):
        self.stub.raw_reply = '{"answers": {"A001": {"noul": 0.9}}}'
        self.assert_pass(self.payload(), expect_request=True)

    def test_malformed_answer_non_numeric(self):
        self.stub.raw_reply = '{"answers": {"A001": {"noul": "high"}}}'
        self.assert_pass(self.payload(), expect_request=True)

    def test_no_block_reaches_threshold(self):
        self.stub.score = lambda l, t: 0.05
        self.assert_pass(self.payload(), expect_request=True)

    def test_kept_text_at_least_80_percent(self):
        self.stub.score = lambda l, t: 0.9
        self.assert_pass(self.payload(), expect_request=True)

    def test_kept_text_just_over_80_percent(self):
        # keep 55 of 60 equally sized blocks (~92%)
        self.stub.score = lambda l, t: 0.9 if int(l[1:]) <= 55 else 0.01
        self.assert_pass(self.payload(), expect_request=True)

    def test_threshold_boundary_is_inclusive(self):
        self.stub.score = lambda l, t: 0.2 if NEEDLE in t else 0.0
        r = self.run_hook(self.payload())
        self.assertNotEqual(r.stdout, "")

    def test_threshold_is_configurable(self):
        self.stub.score = lambda l, t: 0.3 if NEEDLE in t else 0.0
        self.assert_pass(self.payload(), expect_request=True, FOVEA_THRESHOLD="0.5")

    def test_threshold_below_value_not_kept(self):
        self.stub.score = lambda l, t: 0.19 if NEEDLE in t else 0.0
        self.assert_pass(self.payload(), expect_request=True)


if __name__ == "__main__":
    unittest.main()


class HarnessLimitTests(HookCase):
    """Claude Code cuts Bash output at BASH_MAX_OUTPUT_LENGTH (default 30000) and saves it itself."""

    def test_output_at_the_harness_limit_passes_through(self):
        out = make_output(n=200)
        self.assertGreater(len(out), 30000)
        r = self.run_hook(self.payload(stdout=out[:30000]))
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.read_log()[-1]["reason"], "harness_persisted")

    def test_raised_limit_allows_filtering(self):
        out = make_output(n=200)[:30000]
        r = self.run_hook(self.payload(stdout=out), BASH_MAX_OUTPUT_LENGTH="150000")
        self.assertTrue(r.stdout.strip())

    def test_limit_is_counted_in_utf8_bytes(self):
        # Observed live: the cut lands at exactly 30000 UTF-8 bytes, fewer characters.
        line = "commit subject \u2014 with an em dash and enough text to be a real line\n"
        out = (line * 1000).encode("utf-8")[:30000].decode("utf-8", "ignore")
        self.assertLess(len(out), 30000)
        r = self.run_hook(self.payload(stdout=out))
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.read_log()[-1]["reason"], "harness_persisted")

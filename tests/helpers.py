"""Shared helpers for the Fovea v1 contract tests.

These tests are derived only from the "v1 specification" section of docs/DESIGN.md.
They run the hook as a black-box subprocess against a local stub of the judge endpoint.
"""
import copy
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
HOOK = os.path.join(ROOT, "fovea", "hook.py")
FIXTURES = os.path.join(TESTS_DIR, "fixtures")
FIXTURE_PAYLOAD = os.path.join(FIXTURES, "posttooluse-bash-small.json")
FIXTURE_TRANSCRIPT = os.path.join(FIXTURES, "transcript-sample.jsonl")

NEEDLE = "NEEDLE-TOKEN"
KEPT_MARKER = "KEPT-MARKER-QX7"
DROPPED_MARKER = "DROPPED-MARKER-QX7"
API_KEY = "test-key-0123456789"
STATE_PREFIX = "Tool output split into labeled blocks.\n\n"

LOREM = "lorem ipsum dolor sit amet " * 5  # ~135 chars


def load_fixture():
    with open(FIXTURE_PAYLOAD, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- outputs

def paragraph(i, needle=False):
    first = "short %03d" % i if i % 7 == 0 else "paragraph %03d starts here with filler words" % i
    second = "second line of paragraph %03d: %s" % (i, LOREM)
    if needle:
        second = "second line of paragraph %03d: %s %s" % (i, NEEDLE, LOREM)
        third = "third line %s %s" % (KEPT_MARKER, LOREM)
    else:
        third = "third line %s-%03d %s" % (DROPPED_MARKER, i, LOREM)
    return "\n".join([first, second, third])


def make_output(n=60, needles=(30,)):
    """Plain paragraph output (no trailing newline, last paragraph is never a needle)."""
    assert (n - 1) not in needles
    return "\n\n".join(paragraph(i, i in needles) for i in range(n))


def git_log_output(n=60):
    parts = []
    for i in range(n):
        sha = hashlib.sha1(b"c%d" % i).hexdigest()
        body = "Commit body for change %03d. %s" % (i, LOREM)
        if i == 30:
            body += " " + NEEDLE
        parts.append(
            "commit %s\nAuthor: Dev One <dev%d@example.com>\nDate:   Mon Nov 2 10:%02d:00 2026 +0000\n\n"
            "    change %03d: subject line\n\n    %s" % (sha, i, i % 60, i, body)
        )
    return "\n\n".join(parts)


def grep_output(files=40, per_file=3):
    lines = []
    for f in range(files):
        for m in range(per_file):
            extra = NEEDLE if f == 20 and m == 0 else "filler"
            lines.append("src/module%02d/file%02d.py:%d:    result = compute(%s)  # %s %s" % (
                f, f, 10 + m * 7, "arg%d" % m, extra, "x" * 40))
    return "\n".join(lines)


def grep_single_file_output(matches=45):
    return "\n".join(
        "src/big.py:%d:    value_%03d = lookup('key_%03d')  # %s" % (i + 1, i, i, "y" * 50)
        for i in range(matches)
    )


def json_array_output(items=50):
    arr = []
    for i in range(items):
        arr.append({
            "id": i,
            "token": "ITEM-%03d" % i,
            "name": "item number %d" % i,
            "description": ("%s %s" % (NEEDLE if i == 25 else "plain", LOREM)),
            "tags": ["alpha", "beta"],
        })
    return json.dumps(arr)


# ---------------------------------------------------------------- stub judge

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def do_POST(self):
        srv = self.server
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = None
        srv.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                             "body": body, "raw": raw})
        if srv.delay:
            time.sleep(srv.delay)
        try:
            if srv.status != 200:
                self.send_response(srv.status)
                self.end_headers()
                self.wfile.write(b'{"error": "boom"}')
                return
            if srv.raw_reply is not None:
                payload = srv.raw_reply.encode("utf-8")
            else:
                payload = json.dumps(srv.make_answers(body)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception:
            pass


def parse_state(state):
    """Return [(label, text)] from a judge request state."""
    return [(m.group(1), m.group(2))
            for m in re.finditer(r"\[(A\d{3,4})\]\n(.*?)\n\[/\1\]", state, re.S)]


class StubJudge(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests = []
        self.status = 200
        self.delay = 0
        self.raw_reply = None
        self.drop_answers = False
        self.score = lambda label, text: 0.9 if NEEDLE in text else 0.01
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d/api/alpha/decisions" % self.server_address[1]

    def make_answers(self, body):
        texts = dict(parse_state(body["state"]))
        answers = {}
        # The decisions API takes `questions` as an object keyed by label and
        # answers under the same keys.
        for label, q in body["questions"].items():
            assert re.search(r"block (A\d{3,4}) ", q["instructions"]).group(1) == label
            answers[label] = {"noul": self.score(label, texts.get(label, ""))}
        return {"answers": answers}

    def stop(self):
        self.shutdown()
        self.server_close()


# ---------------------------------------------------------------- harness

class Result:
    def __init__(self, stdout, stderr, rc, elapsed):
        self.stdout, self.stderr, self.rc, self.elapsed = stdout, stderr, rc, elapsed

    def json(self):
        return json.loads(self.stdout)


class HookCase(unittest.TestCase):
    TOOL_USE_ID = "toolu_01ErvVJZj85ApncuPcp9SZdk"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)
        self.userhome = os.path.join(self.tmp, "userhome")
        os.makedirs(self.userhome)
        self.home = os.path.join(self.tmp, "fovea-home")
        self.stub = StubJudge()
        self.addCleanup(self.stub.stop)

    # -- environment / invocation
    def env(self, **over):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.userhome,
            "PYTHONIOENCODING": "utf-8",
            "FOVEA_HOME": self.home,
            "FOVEA_BASE_URL": self.stub.url,
            "OPENROUTER_API_KEY": API_KEY,
            "FOVEA_SHADOW_SAMPLE": "0",
            "FOVEA_TIMEOUT": "4",
        }
        for k, v in over.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = str(v)
        return env

    def run_hook(self, payload, stdin=None, **env_over):
        data = stdin if stdin is not None else json.dumps(payload)
        t0 = time.time()
        p = subprocess.run([sys.executable, HOOK], input=data.encode("utf-8"),
                           capture_output=True, env=self.env(**env_over), cwd=self.tmp, timeout=60)
        return Result(p.stdout.decode("utf-8"), p.stderr.decode("utf-8", "replace"), p.returncode,
                      time.time() - t0)

    # -- payload building
    def transcript(self, path=None):
        dst = os.path.join(self.tmp, "transcript.jsonl")
        with open(FIXTURE_TRANSCRIPT, encoding="utf-8") as src, open(dst, "w", encoding="utf-8") as out:
            out.write(src.read())
        return dst

    def payload(self, stdout=None, command="python3 report.py", description="Run the report script",
                transcript_path="__default__", tool_use_id=None, **resp):
        p = load_fixture()
        p["tool_use_id"] = tool_use_id or self.TOOL_USE_ID
        p["tool_input"] = {"command": command}
        if description is not None:
            p["tool_input"]["description"] = description
        p["transcript_path"] = self.transcript() if transcript_path == "__default__" else transcript_path
        p["tool_response"]["stdout"] = make_output() if stdout is None else stdout
        for k, v in resp.items():
            p["tool_response"][k] = v
        return p

    def write_transcript(self, records):
        path = os.path.join(self.tmp, "custom-transcript.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        return path

    # -- artifacts
    def read_log(self):
        out = []
        for path in sorted(glob.glob(os.path.join(self.home, "log", "*.jsonl"))):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        out.append(json.loads(line))
        return out

    def log_text(self):
        text = ""
        for path in glob.glob(os.path.join(self.home, "log", "*")):
            with open(path, encoding="utf-8", errors="replace") as f:
                text += f.read()
        return text

    def out_path(self, tool_use_id=None):
        h = hashlib.sha1((tool_use_id or self.TOOL_USE_ID).encode()).hexdigest()[:8]
        return os.path.join(self.home, "out", h + ".txt")

    # -- scenario runner
    def replace_run(self, stdout=None, command="python3 report.py", **kw):
        env = kw.pop("env", {})
        p = self.payload(stdout=stdout, command=command, **kw)
        r = self.run_hook(p, **env)
        return p, r


# ---------------------------------------------------------------- view parsing

HEADER_RE = re.compile(r"^\[fovea [0-9a-f]{8}: (\d+)/(\d+) blocks, (\d+)% · full: cat (.+?) · kept (\d+)d\]$")
RANGE_RE = re.compile(r"^\[(\d+)(?:-(\d+))?\]$")
SKIP_RE = re.compile(r"^\[skip (\d+)(?:-(\d+))?: (.*)\]$")


def parse_view(text):
    """Parse the replaced stdout into (header, segments)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    m = HEADER_RE.match(lines[0])
    assert m, "bad header line: %r" % lines[0]
    header = {"kept": int(m.group(1)), "total": int(m.group(2)), "pct": int(m.group(3)),
              "path": m.group(4), "days": int(m.group(5))}
    segs = []
    i = 1
    while i < len(lines):
        line = lines[i]
        sm = SKIP_RE.match(line)
        rm = RANGE_RE.match(line)
        if sm:
            a = int(sm.group(1))
            b = int(sm.group(2) or a)
            segs.append({"type": "skip", "a": a, "b": b, "hint": sm.group(3)})
            i += 1
        elif rm:
            a = int(rm.group(1))
            b = int(rm.group(2) or a)
            n = b - a + 1
            segs.append({"type": "kept", "a": a, "b": b, "lines": lines[i + 1:i + 1 + n]})
            i += 1 + n
        else:
            raise AssertionError("unparseable line %d: %r" % (i, line[:100]))
    return header, segs


def request_blocks(req):
    return parse_state(req["body"]["state"])

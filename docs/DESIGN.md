# Fovea: design decisions

A living document: what has been decided and why. The numbers come from the validation run of
5 Nov 2026. Its results and scripts currently live in a private repository; for publication the test
set will be rebuilt on a different codebase.

## Mechanism

1. A Claude Code `PostToolUse` hook receives the tool output (`tool_response`), the call
   (`tool_input`) and the transcript path. Since Claude Code 2.1.121, a hook can replace the output of
   any tool via `hookSpecificOutput.updatedToolOutput`. The value must match the tool's output shape
   (for Bash: `stdout`, `stderr`, `interrupted`, `isImage`; for other tools, capture the shape from real
   calls). Otherwise the replacement is silently ignored.
2. The agent's task is taken from the transcript: the user's latest message, plus the agent's latest
   reasoning, plus the command or path itself. The transcript is written asynchronously and may lag one
   step behind.
3. The output is split into blocks (see below). The blocks are labeled `[A001]…[/A001]` and placed whole
   into `state`.
4. **One Jev request per output,** with one `noul` question per block. **Repeat the task in every
   question.** Stating it once at the top of `state` gives the relevant block p 0.35 instead of 0.96.
5. Keep blocks with p ≥ **0.2**. If no block passes the threshold, the whole output passes through.

## Splitting into blocks

One block is one thing the agent either needs or does not. The type is determined by the tool and the
command first, and by the content second.

| Source | Block |
|---|---|
| Read: markdown | section at `##`; large sections at `###` |
| Read: code | function / class / struct from the syntax tree; large classes by method |
| Read: JSON / YAML / TOML | array element or top-level key |
| grep / rg | matches from one file; many matches in groups of ~20 |
| git log | commit |
| git diff | file; large files by hunk |
| tests, builds | test or error |
| ls / find | directory |
| anything else | paragraph; with no paragraphs, windows of ~40 lines |

- Under ~200 characters: merge with a neighbor. Jev scores tiny blocks poorly: 0.3–0.6 instead of 0.96.
- Over ~2–3k characters: split at the next boundary down. Otherwise there is no saving; on code, 65% of
  the text stays.
- The whole request stays under ~30k tokens. Anything larger becomes several parallel requests.
- Every block carries an "address": path, line range, heading chain, class name.

## What the agent sees

- Kept blocks appear in their original order, with line numbers.
- Dropped blocks collapse into one outline line, for example `[12 blocks skipped, lines 120–460:
  def parse_config(), class Cache… full output: <path>]`. The full output is saved to a file.

## Never filtered

- Outputs under ~1000 tokens.
- Errors and stack traces.
- Reads of a file the agent is about to edit, since an edit needs the exact text.
- Output where the agent already chose the range itself (`sed -n 10,50p`, Read with `offset`).

## Modes

- **Shadow (default):** decide and log, but leave the output unchanged. Users see "would have saved X%,
  would have lost Y" on their own sessions before turning it on.
- **Active:** replace the output.
- With no key, Jev unavailable, or a timeout, the output passes through unchanged.

## Model and configuration

Default judge: TypeSafe Jev via OpenRouter (`~typesafe/jev-latest`, endpoint `api/alpha/decisions`; alpha,
may change). Jev returns one probability per question and generates no text. In validation it took
~0.45 s and ~$0.0006 per output. The scale is monotonic, but the middle runs high (p 0.7–0.9 → about
half the blocks are needed): fine as a filter, not literally a probability.

Everything is configurable through environment variables:

| Variable | Default |
|---|---|
| `FOVEA_BASE_URL` | `https://openrouter.ai/api/alpha/decisions` |
| `FOVEA_API_KEY_ENV` | `OPENROUTER_API_KEY` (name of the variable holding the key) |
| `FOVEA_MODEL` | `~typesafe/jev-latest` |
| `FOVEA_THRESHOLD` | `0.2` |
| `FOVEA_TIMEOUT` | a few seconds; on timeout the output passes through |

**Support policy:** the defaults are calibrated and validated; we stand behind Jev at 0.2. Any other
endpoint or model is the user's choice and responsibility, including the threshold. The endpoint must
speak the decisions format (`state` + `questions` in, one probability per question out); chat models
need a separate adapter, which v1 does not ship.

## Validation (5 Nov 2026, summary)

- **Test set:** 30 outputs in 6 formats, 1140 blocks, 90 tasks.
- **Labels:** three independent labelers (Sonnet, Opus, Minimax), Cohen's kappa 0.81–0.93.
- **At threshold 0.2, by majority vote:** recall 1.00 (90/90), with 29% of the text kept.
- **BM25 keeping the same share:** recall 0.73 overall, and 0.42–0.50 on paraphrased tasks.
- **Limits:** 800 questions per request and ≈33k tokens both go through.
- **Robustness:** block position and order have no effect, and on tasks with no answer, max p ≤ 0.06.
- **Tasks** came in three phrasings: with exact identifiers, paraphrased, and casual Russian.
- **Labeler agreement:** 97.8–99.3% pairwise (kappa above).
- **Calibration:** among 2,940 blocks scored below 0.2, none was needed.
- **Text kept by format at threshold 0.3** (no losses except one markdown block): JSON 6%, git log 7%,
  markdown 7%, grep 32%, code 65%.
- **Latency:** median 0.45 s per output.
- **Live example (5 Nov):** `git log --stat -n 40`, 29,658 chars → 1,261 chars (4%), 1 of 41 blocks kept.

Still open:
- live tasks from real sessions (a week in shadow mode);
- finer splitting of code;
- comparison with blind fixed windows;
- "hard" tasks with no answer.

## Project rules

- Public, MIT, released under Roman's name (GitHub `w2lker`). Public since 5 Nov 2026; the landing page is served by GitHub Pages from `docs/`.
- Other context filters are never named in the code, the docs, or the post.
- All repository docs are in English.

## v1 specification (agreed 5 Nov 2026)

Scope: **Bash only.** Read is deferred: Claude Code renders Read content with its own line numbers
computed from `startLine`, so replaced content would show wrong numbers; and after a filtered Read the
file counts as read, so Claude could Edit/Write it from a partial view. MCP and Grep are out of scope.
Bash carries ~78% of tool-output volume. Not installed into Claude Code until Roman says so.

### Runtime
- Entry point: `python3 <repo>/fovea/hook.py`, hook JSON on stdin. Python ≥ 3.9, standard library only.
- To replace: print `{"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": <obj>}}`
  where `<obj>` is the received `tool_response` with **only `stdout` changed** — all other keys
  (`stderr`, `interrupted`, `isImage`, `noOutputExpected`, any others) copied unchanged.
- Otherwise print nothing. Always exit 0. Any exception → print nothing, log the error.
- Non-eligible calls return in well under 100 ms.
- Install (later): PostToolUse matcher `Bash`, hook `timeout` 10 s.

### Configuration (environment)
| Variable | Default |
|---|---|
| `FOVEA_MODE` | `active` (`shadow`, `off`) |
| `FOVEA_BASE_URL` | `https://openrouter.ai/api/alpha/decisions` |
| `FOVEA_API_KEY_ENV` | `OPENROUTER_API_KEY` |
| `FOVEA_MODEL` | `~typesafe/jev-latest` |
| `FOVEA_THRESHOLD` | `0.2` |
| `FOVEA_TIMEOUT` | `4` seconds (HTTP) |
| `FOVEA_MIN_CHARS` | `3500` (≈1000 tokens) |
| `FOVEA_SHADOW_SAMPLE` | `0.15` |
| `FOVEA_HOME` | `~/.fovea` |
| `FOVEA_RETENTION_DAYS` | `30` |
| `FOVEA_MAX_STORE_MB` | `500` |

### Pass through unchanged (log the reason) when
- `tool_name` is not `Bash`; mode `off`; no API key in the environment.
- `stdout` shorter than `FOVEA_MIN_CHARS`; `interrupted` or `isImage` true.
- `stdout` at Claude Code's Bash limit (`BASH_MAX_OUTPUT_LENGTH`, default 30000, counted in **UTF-8 bytes**) → reason
  `harness_persisted`. Found live on 5 Nov: Claude Code cuts the output to the limit *before* the hook
  sees it, saves the full raw output to its own file, and shows the model only a 2 KB preview of the
  hook's replacement — so a selection would be both incomplete and cut off.
- `stderr` non-empty after removing lines starting with `Shell cwd was reset to ` (a harness note).
- Error output, checked in the **last 30 lines only** and by message shape, not the bare word:
  `Traceback (most recent call last)`, `^\s*(error|Error|ERROR)([…])?: ` (e.g. `error[E0425]: `), `^\s*ERROR\b`,
  `^FAILED`/`FAILED$`/`^FAIL`, `panicked at`, `^fatal: `, `^<Name>Exception: `/`^<Name>Error: `.
  (A first version matched the word anywhere and skipped a `git log` whose commit body said "error or…".)
- Recovery reads: the command references `FOVEA_HOME` (or `~/.fovea`).
- Bypass: the command contains `FOVEA_BYPASS=1`.
- Secrets by command: command words `op`, `security`, `printenv`, `env` (bare), `gh auth token`; paths
  containing `.env`, `.ssh/`, `.pem`, `id_rsa`, `id_ed25519`, `keychain`.
- Secrets by output: `sk-[A-Za-z0-9_-]{20,}`, `ghp_[A-Za-z0-9]{20,}`, `github_pat_`, `AKIA[0-9A-Z]{16}`,
  `xox[abp]-`, `-----BEGIN [A-Z ]*PRIVATE KEY-----`.
- The agent already chose a range: `sed -n`, or the output is piped to `head`/`tail`.
- Judge failure (timeout, HTTP error, malformed answer).
- No block reaches the threshold; or kept text ≥ 80% of the total (not worth it).

### Task text (one string, sent in every question)
In order, each part trimmed: Bash `description`; the command (≤ 300 chars); the latest assistant text
block in the transcript (≤ 600 chars); the latest real user message (≤ 600 chars). Read only the tail of
the transcript (it may lag a step or be missing — then use description + command). A user record is not
"real" if it holds a `tool_result`, has `isMeta: true`, or its text starts with `<system-reminder>`,
`<task-notification>`, `<local-command`, `<command-`.

### Splitting (Bash)
Content checks come first and win over the command: lines `^diff --git ` → diff splitter (covers `gh pr diff`, `bkt pr diff`, `git show`); ≥ 2 lines `^commit [0-9a-f]{7,}` → git-log splitter; stdout parses as JSON → JSON splitter. Otherwise detect by the first real command word (skip `cd …&&`, env assignments, `sudo`):
- `grep`/`rg`/`ag` or lines shaped `path:line:` → one block per file; >20 matches → groups of 20.
- `git log` → split at `^commit [0-9a-f]{7,}`; `git diff`/`git show` → at `^diff --git`, large files at `^@@`.
- `ls`/`find`/`tree` → by directory (`ls -R` sections; path prefix).
- stdout is JSON → array items / top-level keys, on a pretty-printed copy (indent 1); that copy is the
  saved full file so line ranges match.
- `cat`/`less` of `.md` → at `^#{1,3} `; of code files → at column-0 declarations.
- Otherwise paragraphs (blank lines), falling back to 40-line windows.
- Then merge blocks < 200 chars into the next one; split blocks > 2500 chars at line boundaries.
- A request's `state` stays ≤ 100k chars; larger outputs go as several parallel requests.

### Judge request
`POST FOVEA_BASE_URL`, `Authorization: Bearer <key>`, body `{"model", "state", "questions"}`.
`state` = `"Tool output split into labeled blocks.\n\n"` + blocks as `[A001]\n<text>\n[/A001]`, joined by
blank lines (labels `A%03d`, `A%04d` past 999). `questions` is an object keyed by label, one per block:
`{"type": "noul", "instructions": "The coding agent's current task: <task>\nDoes block A001 (the text
between [A001] and [/A001]) contain information the agent needs for this task?"}`. Answer:
`answers[label]["noul"]` (float). Keep blocks with p ≥ `FOVEA_THRESHOLD`.

### What the agent sees (exact format)
```
[fovea 7f3a2c1e: 3/60 blocks, 6% · full: cat ~/.fovea/out/7f3a2c1e.txt · kept 30d]
[skip 1-412: chore(deps) bump…; fix(grep) -t…; docs…; +20]
[413-470]
<lines 413–470 of the full file, verbatim>
[skip 471-1288: feat(init)…; ci…]
```
- Line numbers refer to the saved full file. Adjacent kept blocks merge into one range.
- Skip hints: first non-empty line of up to 3 blocks in the run, skipping git-log metadata lines
  (`commit <hash>`, `Author:`, `Date:`, `Merge:`), each ≤ 24 chars + `…`; `+N` for the rest.
- Header carries the output id (the saved file's name). Percentage = kept chars / total chars. Path written with `~`.
- Kept ranges and skip ranges together cover every line exactly once, in original order.

### Storage
- Full output: `FOVEA_HOME/out/<first 8 hex of sha1(tool_use_id)>.txt`, file mode 600, dir 700.
- Cleanup at most once a day (marker file): delete files older than `FOVEA_RETENTION_DAYS`, then the
  oldest until the folder is under `FOVEA_MAX_STORE_MB`.

### Modes and log
- `active`: replace, except a random `FOVEA_SHADOW_SAMPLE` share of eligible outputs, which pass through
  unchanged but are scored and logged (`sampled_shadow`) — that is how real losses get measured.
- `shadow`: score and log, never replace.
- Log: `FOVEA_HOME/log/YYYY-MM-DD.jsonl`, one line per call that reached eligibility checks: ts,
  session_id, tool_use_id, cwd, command[:200], task[:300], mode, action (`replaced` | `shadow` |
  `sampled_shadow` | `passthrough`), reason, splitter, n_blocks, block_lines {label: [a, b]},
  scores {label: p}, kept [labels], chars_total, chars_kept, latency_ms, cost, out_path, out_id,
  transcript_path, recovered_id, error. Never the output text.

## v1.1: feedback and report (agreed 5 Nov 2026)

### Recovery reads are logged
When a command reads a saved output (`…/out/<id>.txt`, any form of the Fovea home path), the hook still
passes it through, and the log line gets `reason: recovery_read` and `recovered_id: <id>`. A recovery
is an implicit signal that the selection was not enough.

### `fovea note` — the agent reports a bad filter
- CLI entry `fovea/cli.py`, shim `bin/fovea` (symlink it into `~/.local/bin`).
- `fovea note <id> "<what went wrong>"` appends to `FOVEA_HOME/notes.jsonl`: ts, id, note, cwd, plus
  session_id / command / task copied from the matching log line when the id is found. Unknown ids are
  still saved (with `matched: false`) and the command says so. Prints one short confirmation line.
  Bad or missing arguments print usage and exit 2.

### SessionStart hook — the agent learns about notes once per session
`fovea/session_start.py` prints
`{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": <text>}}` unless
`FOVEA_MODE=off`. The text (≈40 tokens): *Bash outputs marked `[fovea <id>: …]` are filtered to the
blocks relevant to your task; `[skip a-b]` lines are omitted ranges of the full file shown in the
header. If the filtering hid something you needed or misled you, run `fovea note <id> "<what went
wrong>"` — only when it really went wrong.*

### `fovea report [--days N]` (default 7)
Plain-text summary to stdout, from logs, notes and transcripts — nothing leaves the machine:
- Calls seen; eligible; replaced; passthrough by reason; judge errors; latency p50/p95; total cost.
- Savings on replaced outputs: sum chars_total, sum chars_kept, share saved, ≈ tokens saved (chars/3.5).
- **Losses (sampled_shadow and shadow records):** take the Bash output from the transcript
  (`tool_result` for that `tool_use_id`), cut it into the logged `block_lines`, and look at the next
  20 assistant records of that session (text and tool inputs). A block counts as *used* if one of its
  lines of ≥ 25 non-space characters appears there verbatim. Report used blocks that scored below the
  threshold (would have been dropped) — count, share of measured outputs, and a list (id, command,
  label, score). Records whose transcript or tool_result is missing are counted as unmeasured.
- Recoveries: count, share of replaced outputs, list of ids with their commands.
- Notes: every note with the matched command, task, kept/total blocks, and the dropped labels with
  scores.

### v1.1 implementation decisions (recorded from the implementer's report)
- Recovery check runs before the shape/size/stderr/error checks (after mode and key), so short recovery
  reads are logged too. `recovered_id` = first `<8 hex>.txt` in the command; a Fovea-home command with no
  id logs `recovered_id: null`.
- `out_id` is logged only when the file is saved; for shadow records the report derives it from
  `tool_use_id`.
- `fovea note`: extra words join into the note; empty note, missing argument or an id starting with `-`
  → usage on stderr, exit 2. The id is normalized (path, `.txt`, `[fovea` prefix, trailing `:` stripped,
  lowercased). Matching searches all logs and prefers the latest `replaced` line.
- SessionStart hint is suppressed only for `FOVEA_MODE=off`; it does not read stdin.
- `fovea report --days N`: last N×24 h by `ts`; N must be an integer ≥ 1 (else exit 2). Latency p50/p95
  for eligible calls and for all calls.
- Losses: output text from the transcript's `toolUseResult.stdout`, else the `tool_result` content (JSON
  re-printed as the splitter saw it); a range past the end → unmeasured. "Would have been dropped" = not
  in the logged `kept`. "Used" = a stripped block line of ≥ 25 non-space chars found in assistant text or
  any string inside tool inputs of the next 20 assistant records (thinking excluded). Count = lost blocks;
  share = outputs with ≥ 1 loss / measured outputs.
- Recoveries share = distinct recovered ids / replaced outputs in the window.

## Packaging (5 Nov 2026)

- The repository is a Claude Code plugin and its own marketplace: `.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json` (marketplace `w2lker`, entry `source: "./"`), hooks in
  `hooks/hooks.json` with paths from `${CLAUDE_PLUGIN_ROOT}`. Install:
  `/plugin install fovea --marketplace w2lker/fovea`.
- `bin/` is on the Bash tool's PATH while the plugin is enabled, so the agent can run `fovea note` and
  `fovea report` without any setup.
- The judge key can come from the install dialog (`userConfig.openrouter_api_key`, sensitive), exported
  to hooks as `CLAUDE_PLUGIN_OPTION_OPENROUTER_API_KEY`; the `FOVEA_API_KEY_ENV` variable wins if set.
- Data stays in `~/.fovea` rather than `${CLAUDE_PLUGIN_DATA}`: the CLI run from Bash does not receive
  the plugin variables, and hook and CLI must share one location.
- Anthropic directory submission (later): needs an icon (square PNG, 512–2048 px); hooks that run
  Python files are held for manual review; the README must disclose what is sent to the judge (done).

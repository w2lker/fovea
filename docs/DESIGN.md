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

Still open:
- live tasks from real sessions (a week in shadow mode);
- finer splitting of code;
- comparison with blind fixed windows;
- "hard" tasks with no answer.

## Project rules

- Public, MIT, released under Roman's name (GitHub `w2lker`). The repository goes public on launch day.
- Other context filters are never named in the code, the docs, or the post.
- All repository docs are in English.

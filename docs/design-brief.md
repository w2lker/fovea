# Design brief: Fovea README and GitHub page

## What Fovea is

Fovea is an open-source plugin for Claude Code (Anthropic's coding agent CLI). It sits between the agent's
tools and the model. When a shell command returns a large output (a `git log`, a grep across a repo, a long
file, a JSON dump), Fovea:

1. splits the output into natural blocks (one commit, one file's matches, one section, one function);
2. asks a fast decision model, "System 1" (a model that returns a probability, not text), whether each block
   matters for the agent's current task. All blocks go in one request, in ~0.5 s, for a fraction of a cent;
3. passes the relevant blocks through **verbatim** and collapses the rest into short outline lines, so the
   agent knows exactly what it is not seeing and can read it in full at any time.

**The metaphor:** the fovea is the small central pit of the retina, about 2° of the visual field. Only there
does the eye see sharply. The periphery is blurry, but you know it's there and can look at it. Fovea gives
the agent sharp focus on what matters, and a periphery it is aware of.

**The idea behind it:** Daniel Kahneman's System 1 / System 2. A fast, cheap, intuitive filter in front of
slow, expensive reasoning. Kahneman's failure mode, **WYSIATI**, means "What You See Is All There Is": a mind
reasons confidently from whatever it was shown. A filter that silently drops text makes an agent
confidently wrong. Fovea's core principle is **select, never delete**. It never paraphrases, and every
omission is visible.

## What the agent actually sees (real example)

`git log --stat -n 40` gave 29,658 chars. The task was "find when the crawl watchdog was added". After Fovea,
1,261 chars remain (4%):

```
[fovea 825aefc4: 1/41 blocks, 3% · full: cat ~/.fovea/out/825aefc4.txt · kept 30d]
[skip 1-66: finance: keep receipts i…; accounting: receipts fro…; homelab: notify-admin pu…]
[67-85]
commit 2315783…
    bni-lookup: watchdog alerts when the crawl tab stops
    …
[skip 86-627: bni-lookup: fix db lock …; bni-lookup: score new pr…; +34]
```

This before/after is the hero of the story. Use it, or a cleaner invented one in the same format.

## Evidence (real numbers from our validation, 5 Nov 2026)

- **Test set:** 30 real tool outputs in 6 formats (grep, source code, git log, JSON, prose, markdown),
  1,140 blocks and 90 agent tasks in three phrasings: with exact identifiers, paraphrased, and casual
  Russian.
- **Labels:** three independent labeler models (Sonnet, Opus, Minimax). Agreement 97.8–99.3%, Cohen's kappa
  0.81–0.93.
- **At threshold 0.2:** every needed block kept in 90/90 tasks (recall 1.00), with **29% of the text
  kept**.
- **Keyword search (BM25) keeping the same 29%:** recall 0.73 overall, and only **0.42–0.50 on paraphrased
  tasks**, where Fovea scores 1.00. That gap is the "System 1 understands meaning, not keywords" story.
- **Text kept by format** at threshold 0.3, with no losses except one block on markdown:

  | Format | Kept |
  |---|---|
  | JSON | 6% |
  | git log | 7% |
  | markdown | 7% |
  | grep | 32% |
  | code | 65% |

  Code is the honest weak spot: nothing is lost, but there is little saving.
- **Calibration:** among 2,940 blocks scored below 0.2, zero were needed.
- **Speed and cost:** median 0.45 s per output, about $0.0006 per output.

Charts worth considering:
- recall vs. share of text kept, Fovea vs. keyword search;
- the paraphrased-task gap;
- kept share by format.

## Audience and tone

- **Audience:** developers who use Claude Code or other coding agents daily and care about context quality
  and token cost. Technical, skeptical, allergic to hype.
- **Tone:** calm, precise, evidence-first. Strong claims are always backed by a number. No "10x", no
  "revolutionary". The differentiator is that we **measured what others only claim**, including the
  losses.
- **Personality:** perception and neuroscience (eye, retina, focus, periphery), with a light Kahneman
  reference. Clean and scientific, not cute.

## Deliverables

1. **README design for the GitHub repo** (`w2lker/fovea`).
   - Hero: a name and tagline lockup and a visual of the fovea metaphor (sharp center, soft periphery).
   - A before/after of a tool output.
   - A "how it works" diagram: tool output → blocks → System 1 judge → verbatim blocks + outline.
   - An evidence section with 1–2 charts.
   - Quick start and configuration (table of env vars).
   - Honest limitations.
2. **A landing page** suitable for GitHub Pages (single static page): the same story with more room to
   breathe.

Tagline (current): *Sharp focus on what matters in your coding agent's tool output; the rest stays in the
periphery.* Alternatives are welcome.

## Constraints

- **GitHub README rendering:**
  - no custom CSS or JS;
  - images as SVG or PNG committed to `docs/assets/`;
  - must look right in **both light and dark mode** (use `<picture>` with `prefers-color-scheme`, or
    designs that work on both);
  - keep text in Markdown, not baked into images, so it stays searchable and copyable.
- **Language:** English everywhere.
- **Naming:**
  - do **not** name or depict any other context-filtering tool, and make no comparisons by name;
  - do not call it "System One" as a product name: that is a vendor's API term. "System 1" may appear only
    as the Kahneman idea.
- **Judge model:** TypeSafe Jev, the default and the only one we calibrated. Mention it plainly in config
  and credits, not as branding.
- **Status:** pre-release. The repo is private until launch, so don't promise features that aren't there.
  - **v1 works:** filtering Bash outputs, the outline, `fovea note` (the agent reports when filtering went
    wrong), `fovea report` (savings and measured losses).
  - **Not yet:** file reads via the editor tool, MCP tool outputs.
- **Author:** Roman Bova (GitHub `w2lker`). MIT license.

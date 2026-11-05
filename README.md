# Fovea

Sharp focus on what matters in your coding agent's tool output; the rest stays in the periphery.

Your eye sees sharply only through the fovea, a spot about two degrees wide. Everything else is
periphery: blurry, but you know it is there and can look at it when you need to. Fovea does the same
for Claude Code. Before a large tool result (a file read, a grep, a log, a `git log`) reaches the
model, it is split into blocks, and a fast decision model (System 1) scores every block against the
task at hand. Relevant blocks pass through verbatim. The rest collapse into a one-line outline, so the
agent always knows it is looking at a selection and can read further.

> **Status:** pre-release. The core runs in shadow mode: it records what it would keep and what it
> would save without changing what the agent sees.

## Why

Irrelevant context costs tokens, and it also makes models reason worse. A filter that silently drops
text has its own failure, though: the agent cannot know what it did not see. Kahneman called this
WYSIATI, "what you see is all there is". Fovea is built around avoiding it: it keeps verbatim lines,
never paraphrases, and every omission is visible.

## License

MIT

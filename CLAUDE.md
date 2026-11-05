# CLAUDE.md

Fovea is a Claude Code plugin: a filter for tool outputs built on a fast decision model (Jev). It keeps
the relevant blocks verbatim and collapses the dropped ones into an outline line. Every decision and the
validation numbers are in [docs/DESIGN.md](docs/DESIGN.md). Read it first, and update it with every new
decision.

- Repository: `git@github.com:w2lker/fovea.git` (public). Branch `main`. GitHub Pages serves `docs/`.
- **All repository docs, comments and commit messages are in English.**
- The bare name `fovea` is taken on PyPI and npm, so the package needs a suffix (e.g. `fovea-cc`).
- Never name other context filters anywhere: code, README, docs, commits.
- The judge key lives in `OPENROUTER_API_KEY` in the environment. It never goes into the repository.

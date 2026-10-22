# CLAUDE.md

`AGENTS.md` is the authoritative project context; `docs/PLAN.md` is the single source of truth for phases, the current goal, approvals and team/model rules. Read those first.

## Non-negotiables (summary of AGENTS.md)

- Personal work-knowledge MCP server for 피터's company PC. Single user, local only.
- Originals, text, indexes and embeddings stay on the PC (outside the repo, not in sync folders). The server makes **no external AI/API calls**; embeddings run locally on CPU. Only snippets that an MCP client requests leave the PC, to that client.
- Company data never goes into git, fixtures, docs or logs. Use synthetic/public documents for development and tests.
- Never modify the user's source files. Every returned snippet/fact carries an exact source locator.
- No admin rights assumed. Model download only through the explicit download module or an offline bundle.
- Secrets only in git-ignored `.env`.

## Commands

To be filled in by the M0 environment work (see `docs/PLAN.md`).

## Style

Communicate in Korean, result first. Simplest readable implementation; prefer the standard library and code ported from `../Motive_vari` (tags `kakao-p1-complete-20261022`, `g0-freeze-20261017`) over new dependencies.

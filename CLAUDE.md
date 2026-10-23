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

- Install (uv, Python 3.12 user-level): `uv venv --python 3.12` then `uv sync --frozen`
- Tests: `uv run pytest -q`
- Environment check: `uv run workmcp doctor` (data home rules, Python, mcp, SQLite/FTS5, model files)
- Run the stdio MCP server: `uv run workmcp serve` (or `.venv\Scripts\python.exe -m workmcp serve`)
- Pip alternative (no uv): `python -m venv .venv`, `.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock.txt`, then `.venv\Scripts\python.exe -m pip install --no-deps -e .` (offline, e.g. the company PC: add `--no-build-isolation` after installing the pinned `setuptools` wheel)
- After changing dependencies: `uv lock`, `uv sync --frozen`, `uv export --format requirements-txt --hashes --no-emit-project -o requirements.lock.txt`
- `requirements.lock.txt` includes the dev dependency `pytest` on purpose, so the same tests run as a diagnosis on the dev PC and the company PC; the build backend is pinned (`setuptools==84.2.1` in `pyproject.toml`).
- Data home: `WORKMCP_HOME`, default `%LOCALAPPDATA%\Motive_work` (refused with exit 2 if inside the repo, a sync folder, UNC or relative).

## Style

Communicate in Korean, result first. Simplest readable implementation; prefer the standard library and code ported from `../Motive_vari` (tags `kakao-p1-complete-20261022`, `g0-freeze-20261017`) over new dependencies.

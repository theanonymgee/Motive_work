# CLAUDE.md

`AGENTS.md` is the authoritative project context; `docs/PLAN.md` is the single source of truth for phases, the current goal, approvals and team/model rules. Read those first.

## Non-negotiables (summary of AGENTS.md)

- Personal work-knowledge MCP server. Single user. Structure (2026-10-26): a collector on the company PC parses, chunks and embeds documents and pushes changes to the workmcp operating server on 피터's Hostinger VPS, which serves MCP over HTTPS with a token.
- Originals stay on the company PC. Chunk text, vectors, indexes and knowledge live only in the collector's data home and on 피터's VPS. **No external AI/API calls** anywhere; embeddings run on CPU (same model on both sides). The collector's only egress is the operating server (plus the explicit model download). Only snippets that an MCP client requests leave the server, to that client.
- Company data never goes into git, fixtures, docs or logs. Use synthetic/public documents for development and tests.
- Never modify the user's source files. Every returned snippet/fact carries an exact source locator.
- No admin rights assumed. Model download only through the explicit download module or an offline bundle.
- Secrets only outside the repository (server keeps token hashes; push token in the data home or env; MCP token in the client config or env).

## Commands

- Install (uv, Python 3.12 user-level): `uv venv --python 3.12` then `uv sync --frozen`
- Tests: `uv run pytest -q`
- Environment check: `uv run workmcp doctor` (data home rules, Python, mcp, SQLite/FTS5, model files)
- Collect: `uv run workmcp scan [--root PATH ...] [--retry-failed] [--reparse] [--rehash]` (roots from `<home>\config.toml` `[[sources]]`, or `--root` once; exit 0 done, 2 data home, 3 scan lock held, 4 sources/settings). In development only tmp folders of synthetic files, never a real document folder.
- Binding check: `uv run workmcp verify [--root PATH ...] [--doc ID ...]` (exit 0 ok, 1 failures; knowledge sources made stale by a later `scan --reparse` are a warning, `stale after reparse N`, not a failure); roots and their state: `uv run workmcp sources`
- Run the stdio MCP server: `uv run workmcp serve` (or `.venv\Scripts\python.exe -I -m workmcp serve`; `-I` blocks a stray `PYTHONPATH`). Exit 0 stdin closed, 2 data home refused or schema checksum changed, 3 store locked while a migration is needed, 4 settings. Client configs: `doc/examples/` (copy to `.mcp.json` / `.codex/config.toml`, never commit; set `WORKMCP_HOME` outside AppData)
- Pip alternative (no uv): `python -m venv .venv`, `.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock.txt`, then `.venv\Scripts\python.exe -m pip install --no-deps -e .` (offline, e.g. the company PC: add `--no-build-isolation` after installing the pinned `setuptools` wheel)
- After changing dependencies: `uv lock`, `uv sync --frozen`, `uv export --format requirements-txt --hashes --no-emit-project -o requirements.lock.txt`
- Build backend pin: `requirements-build.lock.txt` holds `setuptools` with its hash (used by `tools/make_bundle.py`); change it together with `[build-system] requires` in `pyproject.toml`. Offline bundle: `tools/make_bundle.py make --out <outside the repo>`, check with `verify`.
- `requirements.lock.txt` includes the dev dependency `pytest` on purpose, so the same tests run as a diagnosis on the dev PC and the company PC; the build backend is pinned (`setuptools==84.2.1` in `pyproject.toml`).
- Data home: `WORKMCP_HOME`, default `%USERPROFILE%\Motive_work` (refused with exit 2 if inside the repo, a sync folder, UNC, relative or under AppData). Set `WORKMCP_HOME` whenever the home is not that default, so `scan`, `push`, `doctor` and scheduled tasks find it.

## Style

Communicate in Korean, result first. Simplest readable implementation; prefer the standard library and code ported from `../Motive_vari` (tags `kakao-p1-complete-20261022`, `g0-freeze-20261017`) over new dependencies.

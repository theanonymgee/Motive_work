# Motive_work — Project Context

Personal work-knowledge MCP server. `Motive_work` is a working title (folder name), not a product name. This file is the durable project context for both Codex and Claude Code. Current plan, goal and approvals live in `docs/PLAN.md` (single source of truth); progress in `docs/PROGRESS.md`.

## Purpose

- Turn the user's own work material on the **company PC** into a structured, evidence-linked knowledge base, and expose it to the AI coding/chat tools the user already runs there (Claude Code, Codex, and other MCP clients) through a standard **MCP** server.
- Personal use only, single user. Not a product for sale, no multi-tenant features, no web service.
- Origin: the original intent of the sibling project `Motive_vari` (G0, "company context → evidence-linked decisions", spec v0.3 in `../Motive_vari/doc/specification/`), restarted as a lightweight personal tool. Fork points in `Motive_vari`: tags `g0-freeze-20261017` (G0 code, frozen incomplete) and `kakao-p1-complete-20261022` (local parsing/binding/embedding/search code proven on this PC). Port only what is needed; do not copy the G0 synthetic auth, evaluation harness or decision workbench.

## Hard constraints

- **Data stays on the PC.** Originals, extracted text, indexes and embeddings are stored locally (outside the repository, default `%LOCALAPPDATA%\Motive_work\` or a configured path; never in OneDrive/sync folders). No cloud storage, no telemetry.
- **No external AI API calls from the server.** Embeddings and any optional local model run on CPU on the PC (ONNX Runtime; BGE-M3 class). Reasoning and writing are done by the MCP client the user chooses. Only the snippets a client requests through MCP tools leave the server, to that client.
- **Model download** happens only in one explicit module/command (with SHA256 manifest) or from an offline bundle; the server itself has no other network egress.
- **Company data never enters git**, test fixtures, docs, logs or issue text. Development and tests use synthetic or public documents. Real company material exists only on the company PC.
- **No admin rights assumed** on the company PC; user-level Python venv or a portable bundle. Company security software, proxies and blocked downloads are expected.
- **Clients (confirmed 2026-10-23):** 피터 uses both Claude Code and Codex on the company PC with the same stdio MCP server; snippets a client requests go to that client's provider (Anthropic or OpenAI), accepted by 피터.
- **Company AI policy** compliance (installing the tool, sending snippets to Claude/Codex) is the user's responsibility; the tool must make what leaves the PC visible (tool responses list exact sources).
- **Read-only to sources.** The server never modifies, moves or deletes the user's original files.
- **Evidence binding.** Every returned fact or snippet carries an exact source locator (file, version hash, page/sheet/line or character range). Structured knowledge written through MCP must cite a source locator or be explicitly marked as the user's own note.
- Secrets in a git-ignored `.env` only; never print or commit them.

## Stack (initial, may change by plan)

- Python 3.12, standard library first; `onnxruntime` (CPU), `tokenizers`, `numpy` for local embeddings; official `mcp` Python SDK for the stdio server; SQLite (WAL) for metadata, chunks, vectors and history.
- Parsers: text/markdown, PDF, DOCX, PPTX, XLSX, EML/MSG, HWP/HWPX (port from `Motive_vari` research extractor and G0 parsing where licences and quality allow).
- Windows first (company PC). Keep paths and encodings Windows-safe (UTF-8, CP949 inputs).

## Documentation

- Substantive outcomes go under `doc/` (`decisions/`, `planning/`, `prompts/`, `validation/`, others only when needed). Filenames `yyyy-mm-dd_파일명_vN.md` (Asia/Seoul date). Index: `doc/2026-10-22_문서목록_v1.md`.
- Fixed names: `docs/PLAN.md`, `docs/PROGRESS.md`.
- Distinguish facts, estimates, decisions and proposals; record sources with access dates.

## Team and roles

Roles are 총괄 (session owner), 설계자, 실행자, 검토자, 조사 — all Claude models; never use other role names. Model assignment and escalation rules are in `docs/PLAN.md` §7.

## Working style

Korean with the user (address the user as "피터"). Lead with the result, then evidence, verification and limits. Simplest implementation that meets the plan; no speculative abstractions. Verify proportionate to impact. Ask only for decisions that change scope, cost, data handling or results.

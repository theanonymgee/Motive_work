# Motive_work — Project Context

Personal work-knowledge MCP server. `Motive_work` is a working title (folder name), not a product name. This file is the durable project context for both Codex and Claude Code. Current plan, goal and approvals live in `docs/PLAN.md` (single source of truth); progress in `docs/PROGRESS.md`.

## Purpose

- Turn the user's own work material on the **company PC** into a structured, evidence-linked knowledge base, and expose it to the AI coding/chat tools the user already runs (Claude Code, Codex, and other MCP clients) through a standard **MCP** server.
- **Operating structure (피터 decision 2026-10-26):** a **collector** on the company PC reads originals, parses, chunks and embeds them, and pushes only the changes to the **workmcp operating server** on 피터's own Hostinger VPS (KVM 2). The operating server stores chunk text, vectors, source locators and knowledge, embeds queries with the same model, and serves MCP over HTTPS with token authentication. Clients connect to that remote MCP server.
- Personal use only, single user. Not a product for sale, no multi-tenant features, no web service.
- Origin: the original intent of the sibling project `Motive_vari` (G0, "company context → evidence-linked decisions", spec v0.3 in `../Motive_vari/doc/specification/`), restarted as a lightweight personal tool. Fork points in `Motive_vari`: tags `g0-freeze-20261017` (G0 code, frozen incomplete) and `kakao-p1-complete-20261022` (local parsing/binding/embedding/search code proven on this PC). Port only what is needed; do not copy the G0 synthetic auth, evaluation harness or decision workbench.

## Hard constraints

- **Where data lives (피터 decision 2026-10-26).** Original files stay on the company PC only and are never uploaded. Extracted unit/chunk text, embeddings, indexes, source locators and knowledge are stored in exactly two places: the collector's local data home on the company PC (outside the repository, default `%USERPROFILE%\Motive_work`, never in OneDrive/sync folders) and 피터's own Hostinger VPS (workmcp operating server). No other cloud storage, no telemetry. Storing company text on the VPS is 피터's decision and responsibility.
- **No external AI API calls** from the collector or the operating server. Embeddings run on CPU with ONNX Runtime (BGE-M3 class): documents on the company PC, queries on the VPS, with the same model and manifest. Reasoning and writing are done by the MCP client the user chooses. Only the snippets a client requests through MCP tools leave the operating server, to that client.
- **Network boundary.** The collector's only network egress is HTTPS to the configured workmcp operating server (plus the explicit model download module). The operating server accepts MCP and upload requests only over TLS with a valid token (no unauthenticated endpoints) and has no egress except the model download module. Both are fixed by tests.
- **Model download** happens only in one explicit module/command (with SHA256 manifest, pinned revision) or from an offline bundle.
- **Company data never enters git**, test fixtures, docs, logs or issue text. Development and tests use synthetic or public documents. Real company material exists only on the company PC and, as extracted text/vectors, on the operating server.
- **No admin rights assumed** on the company PC; user-level Python venv or a portable bundle. Company security software, proxies and blocked downloads are expected.
- **Clients (confirmed 2026-10-23, remote since 2026-10-26):** 피터 uses both Claude Code and Codex; both connect to the same remote workmcp server; snippets a client requests go to that client's provider (Anthropic or OpenAI), accepted by 피터.
- **Company AI policy** compliance (installing the collector, storing extracted text on the VPS, sending snippets to Claude/Codex) is the user's responsibility; the tool must make what leaves the PC and the server visible (push reports, tool responses list exact sources).
- **Read-only to sources.** The server never modifies, moves or deletes the user's original files.
- **Evidence binding.** Every returned fact or snippet carries an exact source locator (file, version hash, page/sheet/line or character range). Structured knowledge written through MCP must cite a source locator or be explicitly marked as the user's own note.
- Secrets (server tokens, VPS credentials) live only outside the repository: the server stores token SHA-256 hashes only; the collector's push token in its data home token file or an environment variable; the MCP token in the client's own configuration (user scope) or an environment variable; SSH keys in the user's `.ssh` folder. Never print (except the one-time display at issue), log or commit them.
- The repository may be public (MIT, `LICENSE`). Never write the operating server's host name or IP address in tracked files; use the placeholders `<VPS_HOST>` and `<VPS_IP>` and keep the real values outside the repository.

## Stack (initial, may change by plan)

- Python 3.12, standard library first; `onnxruntime` (CPU), `tokenizers`, `numpy` for embeddings; official `mcp` Python SDK (stdio locally, Streamable HTTP on the operating server); SQLite (WAL) for metadata, chunks, vectors and history on both sides.
- Parsers: text/markdown, PDF, DOCX, PPTX, XLSX, EML/MSG, HWP/HWPX (port from `Motive_vari` research extractor and G0 parsing where licences and quality allow).
- Collector: Windows first (company PC); keep paths and encodings Windows-safe (UTF-8, CP949 inputs). Operating server: Ubuntu 24.04 on the VPS, coexisting with 피터's other services there.

## Documentation

- Substantive outcomes go under `doc/` (`decisions/`, `planning/`, `prompts/`, `validation/`, others only when needed). Filenames `yyyy-mm-dd_파일명_vN.md` (Asia/Seoul date). Index: `doc/2026-10-22_문서목록_v1.md`.
- Fixed names: `docs/PLAN.md`, `docs/PROGRESS.md`.
- Distinguish facts, estimates, decisions and proposals; record sources with access dates.

## Team and roles

Roles are 총괄 (session owner), 설계자, 실행자, 검토자, 조사 — all Claude models; never use other role names. Model assignment and escalation rules are in `docs/PLAN.md` §7.

## Working style

Korean with the user (address the user as "피터"). Lead with the result, then evidence, verification and limits. Simplest implementation that meets the plan; no speculative abstractions. Verify proportionate to impact. Ask only for decisions that change scope, cost, data handling or results.

"""MCP stdio transport adapter; business behavior lives in ``tools.py`` (design M0 §1.1, M3 §1, §6).

Five read tools, each an ``async def`` without ``await`` (the SDK runs a sync tool on a worker
thread and every request in its own task; an async tool runs to the end on the event loop thread,
so tool calls never overlap and ``search.Engine`` needs no lock). Every tool returns one string,
the compact JSON envelope (``structured_output=False``: the SDK sends it as one text content as is).
``handle`` is the common path: idle release, the tool function, the size budget, the local call
log (``tool_calls`` + ``deliveries``, no query or body text), a last guard that turns any
exception into ``internal_error`` with the exception class name only (traceback to stderr) and a
final length check of the serialized answer (``response_too_large``, review M3-3).

The call log never holds a call up (review M3-1): its write waits at most ``LOG_BUSY_MS`` for the
store (a running ``scan`` keeps a write transaction open) and a call it could not write stays in a
queue in this process's memory, written by the next call or the ticker. Entries still queued when
the server stops are lost; their number goes to stderr.

Integer and boolean arguments are strict (``StrictInt``/``StrictBool``): the SDK refuses ``true`` or
``2.0`` for an integer with its argument validation error (review L1) instead of converting them.

stdout carries MCP JSON-RPC only: nothing here prints to stdout, logs go to stderr. The server
runs the stdio transport only and makes no outbound calls; ``anyio`` is used for the idle-release
ticker (``sleep``, ``create_task_group``) and nothing else.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import StrictBool, StrictInt

from . import __version__, config, locator, paths, search, snippet, store, tools

LOG = logging.getLogger('workmcp.server')
LOG_FORMAT = '%(asctime)s %(name)s %(levelname)s %(message)s'
INSTRUCTIONS = ('응답의 delivered_sources가 이번 응답으로 PC를 떠난 원문 조각의 출처 전부다. '
                '검색 결과 없음 ≠ 회사에 그 정보가 없음.')
CLIENT_MAX_CHARS = 64
UNKNOWN_CLIENT = 'unknown'
LOG_DELAYED_NOTICE = ('로컬 전달 기록(deliveries)을 대기열에 두었습니다({}건) — 저장소 잠금이 풀리면 씁니다; '
                      '서버가 그 전에 끝나면 잃을 수 있음')
LOG_BUSY_MS = 200           # the call log's busy_timeout: a scan's write transaction never holds a call up
LOG_QUEUE_MAX = 10_000      # calls kept in memory while the store is busy; older ones are dropped (counted)
MODEL_LOADED_NOTICE = '의미 검색 모델을 이 호출에서 처음 로드했습니다(+{} ms); 다음 질의부터 빠릅니다'
_CONTROL = re.compile('[\x00-\x1f\x7f-\x9f]')


@dataclass
class ServerState:
    """What one server process keeps: the search engine, the write connection for the call log
    (None: calls are not logged, tests only), the response budget and the idle-release tick."""
    home: Path
    engine: search.Engine
    write_db: sqlite3.Connection | None = None
    budget: int = tools.RESPONSE_MAX_CHARS - tools.NOTICE_RESERVE
    tick: float = 30.0
    started_at: str = field(default_factory=store.now)
    unloads: int = 0
    pending: list = field(default_factory=list)   # (tool_calls row, deliveries rows) not written yet
    log_dropped: int = 0


def make_state(home: Path, *, engine: search.Engine | None = None, write_db: sqlite3.Connection | None = None,
               settings: config.Config | None = None) -> ServerState:
    """State from ``<home>/config.toml`` (raises ``config.ConfigError``); ``engine`` is injected by
    tests (``HashEmbedder`` factory), else the manifest model, loaded on first semantic use."""
    settings = settings or config.load(home)
    if write_db is not None:
        write_db.execute(f'PRAGMA busy_timeout={LOG_BUSY_MS}')
    return ServerState(home=Path(home), engine=engine or search.Engine(home, settings.search), write_db=write_db,
                       budget=settings.server.response_max_chars - tools.NOTICE_RESERVE,
                       tick=float(settings.server.unload_tick_seconds))


def _clean(text, limit: int = CLIENT_MAX_CHARS) -> str:
    return _CONTROL.sub('', text if isinstance(text, str) else '')[:limit]


def client_of(ctx) -> tuple[str, str]:
    """``(name, version)`` from the client's ``clientInfo`` (``initialize``, or the request envelope on
    newer protocols), ``('unknown', '')`` without one; at most 64 characters, no control characters."""
    try:
        params = ctx.session.client_params if ctx is not None else None
    except (ValueError, AttributeError):  # a Context outside a request
        params = None
    info = getattr(params, 'client_info', None)
    return _clean(getattr(info, 'name', None)) or UNKNOWN_CLIENT, _clean(getattr(info, 'version', None))


# Each takes the state and the tool arguments and returns an envelope.
TOOLS = {
    'search': lambda state, **args: tools.search(state.home, state.engine, **args),
    'get_snippet': lambda state, **args: tools.get_snippet(state.home, **args),
    'list_documents': lambda state, **args: tools.list_documents(state.home, **args),
    'recent_changes': lambda state, **args: tools.recent_changes(state.home, **args),
    'status': lambda state, **args: tools.status(state.home, state.engine, started_at=state.started_at,
                                                 unloads=state.unloads, pending=len(state.pending)),
}


def unload_if_idle(state: ServerState) -> bool:
    """``engine.maybe_unload()``; counts and reports a release (ticker and the start of every call)."""
    released = state.engine.maybe_unload()
    if released:
        state.unloads += 1
        print('workmcp: unloaded model after idle', file=sys.stderr)
    return released


def _annotate_engine(envelope: dict, engine: search.Engine, loads_before: int) -> None:
    """``meta.engine`` on a search result, and a notice when this call loaded the model (design M3 §6.3)."""
    meta = (envelope.get('result') or {}).get('meta') if envelope.get('ok') else None
    if not isinstance(meta, dict):
        return
    loaded_now = engine.loads > loads_before and engine.status()['loaded']
    load_ms = getattr(engine, 'last_load_ms', None) if loaded_now else 0
    meta['engine'] = {'model_loaded_now': loaded_now, 'load_ms': load_ms}
    if loaded_now:
        envelope['notice'].append(MODEL_LOADED_NOTICE.format(load_ms))


def flush_log(state: ServerState) -> bool:
    """Write every queued call (``tool_calls`` + its ``deliveries`` rows) in one transaction; True when
    the queue is empty afterwards. A busy store (``LOG_BUSY_MS``) or any SQLite error keeps the queue."""
    if not state.pending:
        return True
    db = state.write_db
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            for call, rows in state.pending:
                store.record_call(db, call)
                store.record_deliveries(db, call['call_id'], rows)
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    except sqlite3.Error:
        return False
    state.pending.clear()
    return True


def _record(state: ServerState, call_id: str, client: tuple[str, str], tool: str, args: dict, envelope: dict,
            began: float) -> bool:
    """Queue one ``tool_calls`` row and one ``deliveries`` row per delivered piece and write the queue;
    True when written now (also when there is no write connection: nothing is logged, tests only)."""
    if state.write_db is None:
        return True
    pieces = tools.pieces_of(envelope)
    at = store.now()
    call = {'at': at, 'client': client[0], 'client_version': client[1], 'tool': tool, 'call_id': call_id,
            'args_json': tools.summarize_args(tool, args), 'pieces': len(pieces),
            'chars': sum(len(text) for _, text in pieces), 'ok': envelope['ok'],
            'error_code': None if envelope['ok'] else envelope['error']['code'],
            'elapsed_ms': round((time.perf_counter() - began) * 1000, 1), 'pid': os.getpid()}
    rows = [{'at': at, 'client': client[0], 'tool': tool, 'locator_json': obj, 'chars': len(text)}
            for obj, text in pieces]
    state.pending.append((call, rows))
    if len(state.pending) > LOG_QUEUE_MAX:
        state.pending.pop(0)
        state.log_dropped += 1
    return flush_log(state)


def handle(state: ServerState, tool: str, args: dict, client: tuple[str, str]) -> str:
    """Run one tool call and return the serialized envelope (never raises for an ordinary error)."""
    began = time.perf_counter()
    call_id = os.urandom(8).hex()
    try:
        unload_if_idle(state)
        loads_before = state.engine.loads
        envelope = TOOLS[tool](state, **args)
        envelope = tools.fit(envelope, tools.SHRINK[tool], state.budget)
        _annotate_engine(envelope, state.engine, loads_before)
    except search.SearchError as exc:
        envelope = tools.error(exc.code, exc.message)
    except locator.LocatorError as exc:  # a fixed code (shape, hash, range, text mismatch), no text
        envelope = tools.error('locator_invalid', exc.code)
    except snippet.NotFound as exc:
        envelope = tools.error('not_found', str(exc))
    except FileNotFoundError as exc:
        envelope = tools.error('store_missing', str(exc))
    except sqlite3.OperationalError as exc:
        if 'locked' in str(exc) or 'busy' in str(exc):
            envelope = tools.error('store_busy', 'the store is busy; try again', retryable=True)
        else:
            LOG.exception('tool %s failed', tool)
            envelope = tools.error('internal_error', type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - last guard: class name only to the client, traceback to stderr
        LOG.exception('tool %s failed', tool)
        envelope = tools.error('internal_error', type(exc).__name__)
    limit = state.budget + tools.NOTICE_RESERVE
    if len(tools.dumps(envelope)) > limit:  # any path, errors too (review M3-3)
        envelope = tools.error('response_too_large', f'the response does not fit in {limit} characters')
    if not _record(state, call_id, client, tool, args, envelope, began):
        print(f'workmcp: call log delayed for {tool} ({len(state.pending)} queued)', file=sys.stderr)
        if envelope.get('ok'):
            envelope['notice'].append(LOG_DELAYED_NOTICE.format(len(state.pending)))
    return tools.dumps(envelope)


def close_log(state: ServerState) -> int:
    """Last write of the queue when the server stops; returns (and reports on stderr) what is lost."""
    lost = state.log_dropped + (0 if state.write_db is None or flush_log(state) else len(state.pending))
    if lost:
        print(f'workmcp: {lost} call log entries were not written (store busy)', file=sys.stderr)
    return lost


async def _ticker(state: ServerState) -> None:
    while True:
        await anyio.sleep(state.tick)
        try:
            unload_if_idle(state)
            if state.pending:
                flush_log(state)
        except Exception:  # noqa: BLE001 - the ticker must keep running
            LOG.exception('idle release failed')


def _lifespan(state: ServerState):
    """Per connection (stdio: once): the idle-release ticker runs beside the session (design M3 §6.2)."""
    @contextlib.asynccontextmanager
    async def lifespan(_server):
        async with anyio.create_task_group() as group:
            group.start_soon(_ticker, state)
            try:
                yield {}
            finally:
                group.cancel_scope.cancel()
    return lifespan


def build_server(home: Path, *, engine: search.Engine | None = None, state: ServerState | None = None) -> MCPServer:
    """The MCP server with the five read tools; ``server.workmcp_state`` is its ``ServerState``."""
    state = state or make_state(home, engine=engine)
    server = MCPServer('workmcp', version=__version__, instructions=INSTRUCTIONS, lifespan=_lifespan(state))

    @server.tool(name='search', structured_output=False)
    async def search_tool(query: str, mode: str = 'hybrid', limit: StrictInt = 10, path_prefix: str | None = None,
                          ext: list[str] | None = None, since: str | None = None, until: str | None = None,
                          collapse_by_document: StrictBool = False, ctx: Context | None = None) -> str:
        """Hybrid (lexical + semantic) search over the collected documents. Returns up to `limit` hits, each
        with a snippet of document text (at most 600 characters) and its exact source locator;
        delivered_sources lists the source of every snippet in this response. units[] names the stored units
        the hit's chunk spans ({unit, chars, text_sha256}; v, doc, path and sha256 are the hit locator's) and
        carries no text. mode: hybrid|lexical|semantic; since/until filter on when a file version was
        observed, not dates inside documents."""
        return handle(state, 'search', {'query': query, 'mode': mode, 'limit': limit, 'path_prefix': path_prefix,
                                        'ext': ext, 'since': since, 'until': until,
                                        'collapse_by_document': collapse_by_document}, client_of(ctx))

    @server.tool(name='get_snippet', structured_output=False)
    async def get_snippet_tool(chunk_id: StrictInt | None = None, locator: dict[str, Any] | None = None,
                               context_units: StrictInt = 0, max_chars: StrictInt = tools.PIECE_MAX_CHARS,
                               ctx: Context | None = None) -> str:
        """Document text of one search hit (chunk_id) or one locator, as pieces per stored unit with exact
        locators, plus up to 5 whole units of context before and after (context_units). Returns document
        text; delivered_sources lists every piece. A piece over max_chars (200-4000) is cut at a word
        boundary and its locator names the cut range."""
        return handle(state, 'get_snippet', {'chunk_id': chunk_id, 'locator': locator, 'context_units': context_units,
                                             'max_chars': max_chars}, client_of(ctx))

    @server.tool(name='list_documents', structured_output=False)
    async def list_documents_tool(path_prefix: str | None = None, ext: list[str] | None = None, status: str = 'active',
                                  changed_since: str | None = None, limit: StrictInt = tools.LIST_LIMIT_DEFAULT,
                                  offset: StrictInt = 0, ctx: Context | None = None) -> str:
        """Collected documents with their latest version (path, hash, size, parse status, indexed or not).
        Returns no document text. status: active|deleted|skipped|all; limit 1-500, offset for paging."""
        return handle(state, 'list_documents', {'path_prefix': path_prefix, 'ext': ext, 'status': status,
                                                'changed_since': changed_since, 'limit': limit, 'offset': offset},
                      client_of(ctx))

    @server.tool(name='recent_changes', structured_output=False)
    async def recent_changes_tool(since: str | None = None, until: str | None = None, kinds: list[str] | None = None,
                                  path_prefix: str | None = None, ext: list[str] | None = None,
                                  limit: StrictInt = tools.LIST_LIMIT_DEFAULT, offset: StrictInt = 0,
                                  ctx: Context | None = None) -> str:
        """Documents added, changed, restored, deleted or reparsed by scans, newest first (default: the last
        30 days). Returns no document text. kinds: added|changed|restored|deleted|reparsed."""
        return handle(state, 'recent_changes', {'since': since, 'until': until, 'kinds': kinds,
                                                'path_prefix': path_prefix, 'ext': ext, 'limit': limit,
                                                'offset': offset}, client_of(ctx))

    @server.tool(name='status', structured_output=False)
    async def status_tool(ctx: Context | None = None) -> str:
        """Data home, store counts, last scan and index run, model and search engine state, app
        virtualization of the data home, tool calls per client. Returns no document text."""
        return handle(state, 'status', {}, client_of(ctx))

    server.workmcp_state = state
    return server


def main(argv=None) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format=LOG_FORMAT)
    try:
        home = paths.resolve_home()
    except paths.DataPathError as exc:
        print(exc, file=sys.stderr)
        return 2
    paths.ensure_home(home)
    print(f'workmcp: {paths.virtualization_line(home, paths.virtualization_probe(home, os.environ))}',
          file=sys.stderr)
    try:
        settings = config.load(home)
    except config.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 4
    try:
        write_db = store.open_home(home)  # migrates once (no write lock when up to date); kept for the call log
    except sqlite3.OperationalError as exc:  # a migration was needed and another process held the store
        print(f'workmcp: cannot open the store ({exc}): another process (scan or index) holds it while it needs '
              f'a schema update; start the server again when that process ends', file=sys.stderr)
        return 3
    except RuntimeError as exc:
        if not store.schema_changed(exc):
            raise
        print(store.DEV_SCHEMA_HINT.format(db=paths.db_path(home)), file=sys.stderr)
        return 2
    state = make_state(home, write_db=write_db, settings=settings)
    try:
        build_server(home, state=state).run(transport='stdio')
    finally:
        close_log(state)
        write_db.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())

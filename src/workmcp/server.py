"""MCP stdio transport adapter; business behavior lives in ``tools.py`` (design M0 §1.1, M3 §1, §6).

Fifteen tools (the five read tools of M3, six knowledge writers and four knowledge readers of M4), each
an ``async def`` without ``await`` (the SDK runs a sync tool on a worker
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

Knowledge writes use a second write connection (``know_db``, busy timeout ``KNOWLEDGE_BUSY_MS``): a
write must not be lost, so it waits up to 5 s for the store and otherwise answers ``store_busy`` with
nothing written (design M4 §8.2). ``handle`` gives the writers ``recorded_by = "<client>/<tool>"`` and
the call id through ``ServerState.client`` and ``ServerState.call_id`` (set before each call), rolls
back a transaction a writer left open (``internal_error``: a call commits everything or nothing), and
answers a committed write that does not fit the budget with ``response_too_large`` naming the stored
ids, so the client does not repeat it. Two serve processes on one store serialize their writes on
SQLite's write lock (``BEGIN IMMEDIATE`` holds it from the first read to the commit).

Integer and boolean arguments are strict (``StrictInt``/``StrictBool``): the SDK refuses ``true`` or
``2.0`` for an integer with its argument validation error (review L1) instead of converting them.

stdout carries MCP JSON-RPC only: nothing here prints to stdout, logs go to stderr. This module
runs the stdio transport only and makes no outbound calls; ``anyio`` is used for the idle-release
ticker (``sleep``, ``create_task_group``) and nothing else. The operating server (``remote.py``) builds
the same server with token authentication and runs it over Streamable HTTP on a loopback address.
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

from . import __version__, config, knowledge, locator, paths, search, snippet, store, tools

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
KNOWLEDGE_BUSY_MS = 5000    # the knowledge write connection's busy_timeout (design M4 §8.2)
STORED_TOO_LARGE = ('stored (id {}) but the answer does not fit in {} characters; do not repeat the call, '
                    'read it back with get_entity or get_history')
MODEL_LOADED_NOTICE ='의미 검색 모델을 이 호출에서 처음 로드했습니다(+{} ms); 다음 질의부터 빠릅니다'
_CONTROL = re.compile('[\x00-\x1f\x7f-\x9f]')


@dataclass
class ServerState:
    """What one server process keeps: the search engine, the write connection for the call log
    (None: calls are not logged, tests only), the knowledge write connection (None: tests without
    writes), the response budget, the idle-release tick, and the client name and call id of the call
    being handled (``handle`` sets them; the knowledge writers read them)."""
    home: Path
    engine: search.Engine
    write_db: sqlite3.Connection | None = None
    know_db: sqlite3.Connection | None = None
    budget: int = tools.RESPONSE_MAX_CHARS - tools.NOTICE_RESERVE
    tick: float = 30.0
    started_at: str = field(default_factory=store.now)
    unloads: int = 0
    pending: list = field(default_factory=list)   # (tool_calls row, deliveries rows) not written yet
    log_dropped: int = 0
    client: str = UNKNOWN_CLIENT
    call_id: str | None = None
    role: str = 'local'      # 'server' on the operating server (remote.py, design M5 remote §4.7)
    push_db: sqlite3.Connection | None = None   # the operating server's push receive connection (remote.py)
    remote: Any = None       # the operating server's route context (``remote.RemoteContext``), None for stdio


def make_state(home: Path, *, engine: search.Engine | None = None, write_db: sqlite3.Connection | None = None,
               know_db: sqlite3.Connection | None = None, settings: config.Config | None = None) -> ServerState:
    """State from ``<home>/config.toml`` (raises ``config.ConfigError``); ``engine`` is injected by
    tests (``HashEmbedder`` factory), else the manifest model, loaded on first semantic use."""
    settings = settings or config.load(home)
    if write_db is not None:
        write_db.execute(f'PRAGMA busy_timeout={LOG_BUSY_MS}')
    if know_db is not None:
        know_db.execute(f'PRAGMA busy_timeout={KNOWLEDGE_BUSY_MS}')
    return ServerState(home=Path(home), engine=engine or search.Engine(home, settings.search), write_db=write_db,
                       know_db=know_db,
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


def _writer(tool: str, function):
    """A knowledge writer on the knowledge write connection with ``recorded_by = "<client>/<tool>"`` and the
    call id ``handle`` set (design M4 §7.2). Without that connection (a state made for read-only tests)
    the writer answers ``store_missing`` and writes nothing."""
    def call(st: ServerState, **args) -> dict:
        if st.know_db is None:
            return tools.error('store_missing', 'this server has no knowledge write connection')
        return function(st.know_db, recorded_by=f'{st.client}/{tool}', call_id=st.call_id, **args)
    return call


# Each takes the state (positionally) and the tool arguments and returns an envelope. The first parameter
# is not named ``state``: record_task, supersede and list_knowledge have an argument of that name.
TOOLS = {
    'search': lambda st, **args: tools.search(st.home, st.engine, **args),
    'get_snippet': lambda st, **args: tools.get_snippet(st.home, **args),
    'list_documents': lambda st, **args: tools.list_documents(st.home, **args),
    'recent_changes': lambda st, **args: tools.recent_changes(st.home, **args),
    'status': lambda st, **args: tools.status(st.home, st.engine, started_at=st.started_at,
                                              unloads=st.unloads, pending=len(st.pending), role=st.role),
    'record_entity': _writer('record_entity', tools.record_entity),
    'record_fact': _writer('record_fact', tools.record_fact),
    'record_decision': _writer('record_decision', tools.record_decision),
    'record_task': _writer('record_task', tools.record_task),
    'supersede': _writer('supersede', tools.supersede),
    'retract': _writer('retract', tools.retract),
    'find_entity': lambda st, **args: tools.find_entity(st.home, **args),
    'get_entity': lambda st, **args: tools.get_entity(st.home, **args),
    'list_knowledge': lambda st, **args: tools.list_knowledge(st.home, **args),
    'get_history': lambda st, **args: tools.get_history(st.home, **args),
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


def _written_ids(envelope: dict) -> list[int]:
    """The ids a successful knowledge write answer names (entity, assertion, cut and new rows), for the
    message of a write whose answer is too large: the write is stored, the client must not repeat it."""
    result = envelope.get('result') if envelope.get('ok') else None
    if not isinstance(result, dict):
        return []
    return [row['id'] for key in ('entity', 'assertion', 'cut', 'new')
            if isinstance(row := result.get(key), dict) and type(row.get('id')) is int]


def _settle_knowledge(state: ServerState, tool: str) -> bool:
    """True when a knowledge writer left its transaction open (a bug): roll it back, so a call either
    committed everything or wrote nothing (design M4 §8.2); the class of failure goes to stderr."""
    db = state.know_db
    if db is None or not db.in_transaction:
        return False
    db.execute('ROLLBACK')
    LOG.error('tool %s left a knowledge transaction open; rolled back', tool)
    return True


def handle(state: ServerState, tool: str, args: dict, client: tuple[str, str]) -> str:
    """Run one tool call and return the serialized envelope (never raises for an ordinary error).

    A knowledge write is committed or not at all: a transaction a writer left open is rolled back and the
    answer becomes ``internal_error``. A committed write whose answer does not fit the budget answers
    ``response_too_large`` naming the stored ids (not retryable: repeating it would record it twice)."""
    began = time.perf_counter()
    call_id = os.urandom(8).hex()
    state.client, state.call_id = client[0], call_id  # the knowledge writers' recorded_by and call id
    stored: list[int] = []
    try:
        unload_if_idle(state)
        loads_before = state.engine.loads
        envelope = TOOLS[tool](state, **args)
        if tool in tools.WRITE_TOOL_NAMES:
            stored = _written_ids(envelope)
        envelope = tools.fit(envelope, tools.SHRINK[tool], state.budget)
        _annotate_engine(envelope, state.engine, loads_before)
    except knowledge.KnowledgeError as exc:  # argument names, numbers and labels only, never text
        if exc.code == 'internal_error':     # stored, the answer failed (review M4 v1 L8): traceback to stderr
            LOG.exception('tool %s stored its rows but its answer failed', tool)
        envelope = tools.error(exc.code, exc.message, retryable=exc.retryable)
    except store.Busy:  # a writer that did not turn the lock wait into its KnowledgeError: nothing written
        envelope = tools.error('store_busy', 'the store is busy; try again', retryable=True)
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
    if _settle_knowledge(state, tool) and envelope.get('ok'):  # an answer for a write that was not committed
        envelope, stored = tools.error('internal_error', 'TransactionLeftOpen'), []
    limit = state.budget + tools.NOTICE_RESERVE
    if len(tools.dumps(envelope)) > limit:  # any path, errors too (review M3-3)
        envelope = tools.error('response_too_large', f'the response does not fit in {limit} characters')
    if stored and not envelope.get('ok') and envelope['error']['code'] == 'response_too_large':
        envelope = tools.error('response_too_large', STORED_TOO_LARGE.format(', '.join(map(str, stored)), limit))
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
    """Once per process (stdio: the one connection; Streamable HTTP: the session manager's run, design M5
    remote §0.1): the idle-release ticker runs beside the sessions (design M3 §6.2)."""
    @contextlib.asynccontextmanager
    async def lifespan(_server):
        async with anyio.create_task_group() as group:
            group.start_soon(_ticker, state)
            try:
                yield {}
            finally:
                group.cancel_scope.cancel()
    return lifespan


def build_server(home: Path, *, engine: search.Engine | None = None, state: ServerState | None = None,
                 instructions: str = INSTRUCTIONS, auth=None, token_verifier=None, log_level: str = 'INFO') -> MCPServer:
    """The MCP server with the fifteen tools; ``server.workmcp_state`` is its ``ServerState``.

    The operating server (``remote.py``, design M5 remote §4.1, §4.2, §4.7) passes its own ``instructions``,
    the SDK's ``auth`` (``AuthSettings``) with a ``token_verifier`` and ``log_level='WARNING'`` (the SDK
    hands it to uvicorn: no access log); the stdio server passes none of them."""
    state = state or make_state(home, engine=engine)
    server = MCPServer('workmcp', version=__version__, instructions=instructions, lifespan=_lifespan(state),
                       auth=auth, token_verifier=token_verifier, log_level=log_level)

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

    _knowledge_tools(server, state)
    server.workmcp_state = state
    return server


def _knowledge_tools(server: MCPServer, state: ServerState) -> None:
    """The ten knowledge tools (design M4 §6). Several have an argument named ``state`` (a task state), so
    the bodies call ``answer``, which holds the ``ServerState``."""

    def answer(tool: str, args: dict, ctx) -> str:
        return handle(state, tool, args, client_of(ctx))

    @server.tool(name='record_entity', structured_output=False)
    async def record_entity_tool(kind: str, name: str, aliases: list[str] | None = None, note: str | None = None,
                                 ctx: Context | None = None) -> str:
        """Record a person, project, customer, org, term or other entity (kind) by name, or add aliases to the
        existing one with the same kind and name (never merged automatically). Returns no document text."""
        return answer('record_entity', {'kind': kind, 'name': name, 'aliases': aliases, 'note': note}, ctx)

    @server.tool(name='record_fact', structured_output=False)
    async def record_fact_tool(entity_id: StrictInt, predicate: str, value: str | None = None, unit: str | None = None,
                               object_entity_id: StrictInt | None = None, statement: str | None = None,
                               valid_from: str | None = None, valid_to: str | None = None,
                               sources: list[dict[str, Any]] | None = None, my_note: StrictBool = False,
                               ctx: Context | None = None) -> str:
        """Record a fact: subject entity_id, predicate (check predicates_in_use of get_entity first), and a
        value (+unit) or an object entity. valid_from/valid_to (YYYY-MM-DD, to exclusive) say when it is true.
        The server finds each quote in the stored text and stores its exact locator; the response returns the
        stored quotes (delivered_sources). An open fact with the same subject and predicate, an overlapping
        period and another value is marked as a conflict (both kept). Give exactly one of sources (1-5
        items {chunk_id or locator, quote}; quote copied exactly from a search snippet or get_snippet text,
        inside one unit) or my_note: true (stored as "내 메모")."""
        return answer('record_fact', {'entity_id': entity_id, 'predicate': predicate, 'value': value, 'unit': unit,
                                   'object_entity_id': object_entity_id, 'statement': statement,
                                   'valid_from': valid_from, 'valid_to': valid_to, 'sources': sources,
                                   'my_note': my_note}, ctx)

    @server.tool(name='record_decision', structured_output=False)
    async def record_decision_tool(statement: str, entity_id: StrictInt | None = None, options: list[str] | None = None,
                                   chosen: str | None = None, rationale: str | None = None,
                                   decided_on: str | None = None, valid_to: str | None = None,
                                   sources: list[dict[str, Any]] | None = None, my_note: StrictBool = False,
                                   ctx: Context | None = None) -> str:
        """Record a decision (statement, options, chosen, rationale, decided_on YYYY-MM-DD), optionally about
        an entity. The response returns the stored quotes (delivered_sources). Give exactly one of sources
        (1-5 items {chunk_id or locator, quote}, quote copied exactly) or my_note: true."""
        return answer('record_decision', {'statement': statement, 'entity_id': entity_id, 'options': options,
                                       'chosen': chosen, 'rationale': rationale, 'decided_on': decided_on,
                                       'valid_to': valid_to, 'sources': sources, 'my_note': my_note}, ctx)

    @server.tool(name='record_task', structured_output=False)
    async def record_task_tool(statement: str, entity_id: StrictInt | None = None, due: str | None = None,
                               owner_entity_id: StrictInt | None = None, state: str = 'open',
                               done_at: str | None = None, sources: list[dict[str, Any]] | None = None,
                               my_note: StrictBool = False, ctx: Context | None = None) -> str:
        """Record a task (statement, due YYYY-MM-DD, owner entity, state open|done|cancelled). Change its state
        later with supersede. The response returns the stored quotes (delivered_sources). Give exactly one of
        sources (1-5 items {chunk_id or locator, quote}, quote copied exactly) or my_note: true."""
        return answer('record_task', {'statement': statement, 'entity_id': entity_id, 'due': due,
                                   'owner_entity_id': owner_entity_id, 'state': state, 'done_at': done_at,
                                   'sources': sources, 'my_note': my_note}, ctx)

    @server.tool(name='supersede', structured_output=False)
    async def supersede_tool(assertion_id: StrictInt, reason: str, sources: list[dict[str, Any]] | None = None,
                             my_note: StrictBool = False, old_valid_to: str | None = None,
                             entity_id: StrictInt | None = None, predicate: str | None = None, value: str | None = None,
                             unit: str | None = None, object_entity_id: StrictInt | None = None,
                             statement: str | None = None, valid_from: str | None = None, valid_to: str | None = None,
                             options: list[str] | None = None, chosen: str | None = None,
                             rationale: str | None = None, decided_on: str | None = None, due: str | None = None,
                             owner_entity_id: StrictInt | None = None, state: str | None = None,
                             done_at: str | None = None, ctx: Context | None = None) -> str:
        """Correct a fact, decision or task: a new row with the given fields changed (the others copied) and
        the old row closed as superseded (kept in history). Without old_valid_to this corrects the record:
        the old row leaves current knowledge for every date. With old_valid_to (YYYY-MM-DD) the old content
        stays true until that date and the new one holds from it. Fields must belong to the row's kind.
        Sources are never inherited: give exactly one of sources (1-5 items {chunk_id or locator, quote},
        quote copied exactly) or my_note: true."""
        return answer('supersede', {'assertion_id': assertion_id, 'reason': reason, 'sources': sources,
                                 'my_note': my_note, 'old_valid_to': old_valid_to, 'entity_id': entity_id,
                                 'predicate': predicate, 'value': value, 'unit': unit,
                                 'object_entity_id': object_entity_id, 'statement': statement,
                                 'valid_from': valid_from, 'valid_to': valid_to, 'options': options,
                                 'chosen': chosen, 'rationale': rationale, 'decided_on': decided_on, 'due': due,
                                 'owner_entity_id': owner_entity_id, 'state': state, 'done_at': done_at}, ctx)

    @server.tool(name='retract', structured_output=False)
    async def retract_tool(assertion_id: StrictInt, reason: str, ctx: Context | None = None) -> str:
        """Withdraw a fact, decision or task with a reason; the row stays in history (never deleted) and its
        conflicts are resolved. Returns no document text."""
        return answer('retract', {'assertion_id': assertion_id, 'reason': reason}, ctx)

    @server.tool(name='find_entity', structured_output=False)
    async def find_entity_tool(query: str, kind: str | None = None, limit: StrictInt = tools.FIND_LIMIT_DEFAULT,
                               ctx: Context | None = None) -> str:
        """Entities whose name or alias contains query (case-insensitive), exact matches first; limit 1-50.
        Returns no document text."""
        return answer('find_entity', {'query': query, 'kind': kind, 'limit': limit}, ctx)

    @server.tool(name='get_entity', structured_output=False)
    async def get_entity_tool(entity_id: StrictInt | None = None, kind: str | None = None, name: str | None = None,
                              as_of_valid: str | None = None, as_of_recorded: str | None = None,
                              include_closed: StrictBool = False, source_kind: str = 'all',
                              limit: StrictInt = tools.ENTITY_LIMIT_DEFAULT, ctx: Context | None = None) -> str:
        """One entity (entity_id, or exact kind and name) with its facts, facts naming it, decisions, tasks and
        predicates_in_use. as_of_valid (YYYY-MM-DD): only what was true that day (default: every period);
        as_of_recorded (ISO time): what the record knew then (default now). Returns the evidence quotes of
        each item (document text; delivered_sources lists them). source_kind: evidence|my_note|all."""
        return answer('get_entity', {'entity_id': entity_id, 'kind': kind, 'name': name, 'as_of_valid': as_of_valid,
                                  'as_of_recorded': as_of_recorded, 'include_closed': include_closed,
                                  'source_kind': source_kind, 'limit': limit}, ctx)

    @server.tool(name='list_knowledge', structured_output=False)
    async def list_knowledge_tool(kind: str = 'all', entity_id: StrictInt | None = None, predicate: str | None = None,
                                  status: str = 'open', state: str | None = None, due_before: str | None = None,
                                  source_kind: str = 'all', as_of_valid: str | None = None,
                                  as_of_recorded: str | None = None, recorded_since: str | None = None,
                                  limit: StrictInt = tools.KNOWLEDGE_LIMIT_DEFAULT, offset: StrictInt = 0,
                                  ctx: Context | None = None) -> str:
        """Facts, decisions and tasks (kind fact|decision|todo|all) filtered by entity, predicate, status
        (open|conflict|closed|all), task state, due date, source kind and the two time axes (as in
        get_entity), newest first; limit 1-200, offset for paging. Returns no document text."""
        return answer('list_knowledge', {'kind': kind, 'entity_id': entity_id, 'predicate': predicate,
                                      'status': status, 'state': state, 'due_before': due_before,
                                      'source_kind': source_kind, 'as_of_valid': as_of_valid,
                                      'as_of_recorded': as_of_recorded, 'recorded_since': recorded_since,
                                      'limit': limit, 'offset': offset}, ctx)

    @server.tool(name='get_history', structured_output=False)
    async def get_history_tool(assertion_id: StrictInt, ctx: Context | None = None) -> str:
        """The supersede chain of one fact, decision or task, oldest first, with its events (recorded,
        retracted, superseded, conflicts), links and evidence quotes (document text; delivered_sources lists
        them), and whether a quote from a changed file is still found in its latest version."""
        return answer('get_history', {'assertion_id': assertion_id}, ctx)


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
    know_db = store.connect(paths.db_path(home))  # already migrated by write_db: read-only check, no lock
    state = make_state(home, write_db=write_db, know_db=know_db, settings=settings)
    try:
        build_server(home, state=state).run(transport='stdio')
    finally:
        close_log(state)
        know_db.close()
        write_db.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())

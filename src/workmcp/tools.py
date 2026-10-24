"""MCP tool implementations as plain functions (dict in, envelope dict out; design M0 §4, M3 §2).

Every response is an envelope ``{"ok": true, "result", "delivered_sources", "notice"}`` or
``{"ok": false, "error": {"code", "message", "retryable"}}``. ``delivered_sources`` lists, in
order, one ``locator.display`` line per piece of document text the response carries (search
snippets, ``get_snippet`` pieces and context) and nothing else (design M3 §3.1); ``pieces_of``
finds those pieces from the response structure. ``list_documents``, ``recent_changes`` and
``status`` carry none. Nothing here knows the MCP SDK; ``server.handle`` serializes with ``dumps``.

Stage 0 (design M3 §14.1): ``status`` is complete; ``search``, ``get_snippet``, ``list_documents``
and ``recent_changes`` have their final signatures and answer ``not_implemented`` until stage 1.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from . import locator, models, paths, snippet, store
from . import search as searching  # the module; ``search`` below is the tool

TOOL_NAMES = ('search', 'get_snippet', 'list_documents', 'recent_changes', 'status')

# Size budget (design M3 §4): characters of the serialized envelope (``dumps``), not bytes.
RESPONSE_MAX_CHARS = 40_000     # config [server] response_max_chars may lower it (4,000-40,000)
NOTICE_RESERVE = 200            # room for the notice added when the local delivery log fails (§3.3)
PIECE_MAX_CHARS = snippet.PIECE_MAX_CHARS  # one get_snippet piece (default and maximum of max_chars)
PIECE_MIN_CHARS = 200
CONTEXT_UNITS_MAX = 5
LIST_LIMIT_DEFAULT = 100
LIST_LIMIT_MAX = 500
RECENT_DEFAULT_DAYS = 30
DOCUMENT_STATUSES = (*store.DOCUMENT_STATUSES, 'all')
EVENT_KINDS = store.EVENT_KINDS

# Error codes (design M3 §8.1); only store_busy is retryable.
ERROR_CODES = (*searching.ERROR_CODES, 'argument_invalid', 'locator_invalid', 'not_found', 'store_missing', 'store_busy',
               'response_too_large', 'internal_error')
NOT_IMPLEMENTED = 'not_implemented'  # stage 0 only; not one of ERROR_CODES

# Result shapes (design M3 §2). The search tool's ``result`` is ``searching.RESULT_KEYS`` with
# ``searching.HIT_KEYS`` hits; its ``meta`` is ``searching.META_KEYS`` plus the keys the tool layer adds.
SEARCH_META_EXTRA_KEYS = frozenset({'collapsed', 'truncated_hits', 'signals', 'engine'})  # beyond M2 §5.7
SEARCH_ENGINE_KEYS = frozenset({'model_loaded_now', 'load_ms'})
SNIPPET_RESULT_KEYS = snippet.RESULT_KEYS
PIECE_KEYS = snippet.PIECE_KEYS
DOCUMENT_KEYS = frozenset({'id', 'path', 'ext', 'status', 'skip_reason', 'root_id', 'first_seen_at', 'last_seen_at',
                           'deleted_at', 'latest', 'indexed'})
DOCUMENT_LATEST_KEYS = frozenset({'version_id', 'seq', 'sha256', 'size', 'observed_at', 'parse_status', 'parse_error',
                                  'unit_count', 'text_chars'})
LIST_RESULT_KEYS = frozenset({'documents', 'total', 'limit', 'offset', 'has_more'})
CHANGE_KEYS = frozenset({'id', 'at', 'kind', 'doc', 'version_id', 'seq', 'sha256', 'parse_status', 'parse_error',
                         'scan_id'})
CHANGE_DOC_KEYS = frozenset({'id', 'path', 'ext', 'status'})
CHANGES_RESULT_KEYS = frozenset({'changes', 'total', 'limit', 'offset', 'has_more', 'since', 'until'})
STATUS_KEYS = frozenset({'home', 'schema_version', 'db', 'model', 'fts5', 'last_scan', 'index', 'delivery_log_count',
                         'engine', 'home_virtualization', 'calls', 'process'})
ENGINE_STATUS_EXTRA_KEYS = frozenset({'idle_seconds', 'model_idle_seconds', 'loads', 'unloads'})  # + Engine.status()


def ok(result, *, delivered_sources=(), notice=()) -> dict:
    return {'ok': True, 'result': result, 'delivered_sources': list(delivered_sources), 'notice': list(notice)}


def error(code: str, message: str, *, retryable: bool = False) -> dict:
    return {'ok': False, 'error': {'code': code, 'message': message, 'retryable': retryable}}


def dumps(envelope: Mapping) -> str:
    """The wire form of an envelope: compact JSON, non-ASCII kept (its ``len`` is what the budget counts)."""
    return json.dumps(envelope, ensure_ascii=False, separators=(',', ':'))


# ---- delivered pieces and the size budget (design M3 §3.1, §4) ----------------------------------

def pieces_of(envelope: Mapping) -> list[tuple[dict, str]]:
    """``(locator, text)`` of every piece of document text in a response, in response order: search
    ``hits[*].snippet``; ``get_snippet`` ``context_before``, ``pieces``, ``context_after``. ``units[]``,
    paths, ``notice`` and ``meta`` are not text; an error envelope has none."""
    if not envelope.get('ok'):
        return []
    result = envelope.get('result') or {}
    if 'hits' in result:
        return [(hit['locator'], hit['snippet']) for hit in result['hits']]
    if 'pieces' in result:
        return [(piece['locator'], piece['text']) for key in ('context_before', 'pieces', 'context_after')
                for piece in result.get(key) or ()]
    return []


def delivered(envelope: Mapping) -> list[str]:
    """``delivered_sources`` of a response: ``locator.display`` of each piece, in order."""
    return [locator.display(obj) for obj, _ in pieces_of(envelope)]


def shrink_none(envelope: dict) -> bool:
    """A shrink step that can drop nothing (``status``; stage 0 for every tool)."""
    return False


# Per tool: drop one unit from the end (a hit, a context unit, a row) or cut a piece; False when
# nothing is left to drop. Stage 1 replaces search, get_snippet, list_documents and recent_changes.
SHRINK: dict[str, Callable[[dict], bool]] = {name: shrink_none for name in TOOL_NAMES}


def fit(envelope: dict, shrink: Callable[[dict], bool], budget: int = RESPONSE_MAX_CHARS - NOTICE_RESERVE) -> dict:
    """``envelope`` with ``delivered_sources`` recomputed, shrunk by ``shrink`` (which edits it in place
    and records its own notice) until ``dumps`` is at most ``budget`` characters; ``response_too_large``
    when it cannot get there. A dropped piece is in neither the response nor ``delivered_sources``."""
    if not envelope.get('ok'):
        return envelope
    envelope['delivered_sources'] = delivered(envelope)
    while len(dumps(envelope)) > budget:
        if not shrink(envelope):
            return error('response_too_large', f'the response does not fit in {budget} characters')
        envelope['delivered_sources'] = delivered(envelope)
    return envelope


# ---- local log summary (design M3 §3.3) ---------------------------------------------------------

FILTER_ARGS = {'search': ('path_prefix', 'ext', 'since', 'until'),
               'list_documents': ('path_prefix', 'ext', 'changed_since'),
               'recent_changes': ('since', 'until', 'kinds', 'path_prefix', 'ext')}


def _count(value):
    return value if type(value) is int else None


def _choice(value, allowed):
    return value if value is None or value in allowed else 'invalid'


def summarize_args(tool: str, args: Mapping) -> dict:
    """``tool_calls.args_json``: which arguments were used, never the query, a path or a locator body.
    The query is kept as its length and the first 12 hex digits of its SHA-256 only."""
    used = sorted(key for key in FILTER_ARGS.get(tool, ()) if args.get(key) is not None)
    if tool == 'search':
        query = args.get('query') if isinstance(args.get('query'), str) else ''
        return {'mode': _choice(args.get('mode'), searching.MODES), 'limit': _count(args.get('limit')), 'filters': used,
                'query_chars': len(query),
                'query_sha12': hashlib.sha256(query.encode('utf-8', 'surrogatepass')).hexdigest()[:12],
                'collapse': bool(args.get('collapse_by_document'))}
    if tool == 'get_snippet':
        chunk_id, obj = args.get('chunk_id'), args.get('locator')
        by = 'both' if chunk_id is not None and obj is not None else 'chunk_id' if chunk_id is not None else \
            'locator' if obj is not None else None
        return {'by': by, 'chunk_id': _count(chunk_id), 'doc': _count(obj.get('doc')) if isinstance(obj, Mapping) else None,
                'context_units': _count(args.get('context_units')), 'max_chars': _count(args.get('max_chars'))}
    if tool == 'list_documents':
        return {'filters': used, 'status': _choice(args.get('status'), DOCUMENT_STATUSES),
                'limit': _count(args.get('limit')), 'offset': _count(args.get('offset'))}
    if tool == 'recent_changes':
        kinds = args.get('kinds')
        return {'filters': used, 'kinds': sorted({k for k in kinds if k in EVENT_KINDS}) if isinstance(kinds, list) else None,
                'limit': _count(args.get('limit')), 'offset': _count(args.get('offset'))}
    return {}


# ---- the five tools -----------------------------------------------------------------------------

def _not_implemented(tool: str) -> dict:
    return error(NOT_IMPLEMENTED, f'{tool} is not implemented yet (M3 stage 1)')


def search(home: Path, engine, query: str, *, mode: str = 'hybrid', limit: int = searching.LIMIT_DEFAULT,
                path_prefix: str | None = None, ext: list[str] | None = None, since: str | None = None,
                until: str | None = None, collapse_by_document: bool = False) -> dict:
    """``search`` (design M3 §2.2): ``engine.search(...)`` over ``home``; ``result`` = M2 §5.7 plus
    ``meta.collapsed/truncated_hits/signals/engine``; ``delivered_sources`` one line per hit snippet.
    Raises ``search.SearchError`` for bad input (``server.handle`` turns it into an error envelope)."""
    return _not_implemented('search')


def get_snippet(home: Path, *, chunk_id: int | None = None, locator: dict | None = None, context_units: int = 0,
                max_chars: int = PIECE_MAX_CHARS) -> dict:
    """``get_snippet`` (design M3 §2.3): exactly one of ``chunk_id``/``locator``; ``result`` =
    ``SNIPPET_RESULT_KEYS`` (``snippet.by_chunk`` / ``snippet.by_locator``); ``delivered_sources`` =
    ``context_before``, ``pieces``, ``context_after``. Errors ``argument_invalid``, ``locator_invalid``,
    ``not_found``, ``store_missing``."""
    return _not_implemented('get_snippet')


def list_documents(home: Path, *, path_prefix: str | None = None, ext: list[str] | None = None, status: str = 'active',
                   changed_since: str | None = None, limit: int = LIST_LIMIT_DEFAULT, offset: int = 0) -> dict:
    """``list_documents`` (design M3 §2.4): ``LIST_RESULT_KEYS`` with ``DOCUMENT_KEYS`` rows
    (``store.documents_query``), no text, ``delivered_sources`` empty."""
    return _not_implemented('list_documents')


def recent_changes(home: Path, *, since: str | None = None, until: str | None = None, kinds: list[str] | None = None,
                   path_prefix: str | None = None, ext: list[str] | None = None, limit: int = LIST_LIMIT_DEFAULT,
                   offset: int = 0) -> dict:
    """``recent_changes`` (design M3 §2.5): ``CHANGES_RESULT_KEYS`` with ``CHANGE_KEYS`` rows
    (``store.document_events_query``), newest first, ``since`` default now - 30 days, no text."""
    return _not_implemented('recent_changes')


def _index_status(db) -> dict:
    """``{last_run, chunk_version, model{name, model_key, vectors}, fts{...}}`` (design M2 §1.4): counts only."""
    last = store.last_index(db)
    key = last['model_key'] if last else None
    vectors = db.execute('SELECT COUNT(*) FROM chunk_vectors WHERE model_key=?', (key,)).fetchone()[0] if key else 0
    return {'last_run': ({k: last[k] for k in ('at', 'chunks', 'embedded', 'reused')} if last else None),
            'chunk_version': store.meta_get(db, 'chunk_version'),
            'model': {'name': last['model'] if last else None, 'model_key': key, 'vectors': vectors},
            'fts': store.fts_status(db)}


def engine_status(engine, unloads: int = 0) -> dict | None:
    """``Engine.status()`` plus ``ENGINE_STATUS_EXTRA_KEYS`` (design M3 §2.6); None without an engine."""
    if engine is None:
        return None
    last = engine.last_used
    return {**engine.status(),
            'idle_seconds': None if last is None else round(engine.clock() - last, 1),
            'model_idle_seconds': engine.settings.model_idle_seconds, 'loads': engine.loads, 'unloads': unloads}


def status(home: Path, engine=None, *, started_at: str | None = None, unloads: int = 0) -> dict:
    """Data home, store counts, last finished scan and index run, model availability, FTS5 support,
    the FTS index state, the search engine of this process, app virtualization of the home, tool
    calls per client and this process (design M3 §2.6); no source text."""
    try:
        db = store.open_read(home)
    except FileNotFoundError as exc:
        return error('store_missing', str(exc))
    try:
        count = {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                 for table in ('documents', 'versions', 'chunks', 'chunk_vectors', 'deliveries')}
        schema = db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
        last_scan = store.last_scan(db)
        index = _index_status(db)
        calls = store.call_counts(db)
    finally:
        db.close()
    result = {
        'home': str(home),
        'schema_version': schema,
        'db': {'documents': count['documents'], 'versions': count['versions'], 'chunks': count['chunks'],
               'vectors': count['chunk_vectors']},
        'model': models.quick_status(models.DEFAULT_MODEL, home),
        'fts5': store.fts5_available(),
        'last_scan': last_scan,
        'index': index,
        'delivery_log_count': count['deliveries'],
        'engine': engine_status(engine, unloads),
        'home_virtualization': paths.virtualization_probe(home, os.environ),
        'calls': calls,
        'process': {'pid': os.getpid(), 'started_at': started_at, 'python': list(sys.version_info[:3])},
    }
    notice = ['아직 수집(scan)한 적이 없습니다.'] if result['last_scan'] is None else []
    fts = index['fts']
    if fts['available'] and not fts['consistent']:
        notice.append(f"전문 색인(chunks_fts)이 청크와 맞지 않습니다(빠짐 {fts['missing']}, 남은 항목 {fts['extra']}"
                      f"{', FTS5 없이 바뀐 청크 있음' if fts['dirty'] else ''}) - "
                      f'workmcp index --rebuild-fts를 실행하세요.')
    return ok(result, notice=notice)

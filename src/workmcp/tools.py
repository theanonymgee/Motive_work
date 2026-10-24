"""MCP tool implementations as plain functions (dict in, envelope dict out; design M0 §4, M3 §2).

Every response is an envelope ``{"ok": true, "result", "delivered_sources", "notice"}`` or
``{"ok": false, "error": {"code", "message", "retryable"}}``. ``delivered_sources`` lists, in
order, one ``locator.display`` line per piece of document text the response carries (search
snippets, ``get_snippet`` pieces and context) and nothing else (design M3 §3.1); ``pieces_of``
finds those pieces from the response structure. ``list_documents``, ``recent_changes`` and
``status`` carry none. Nothing here knows the MCP SDK; ``server.handle`` serializes with ``dumps``.

Argument errors come back as error envelopes (``argument_invalid`` here); ``search.SearchError``,
``locator.LocatorError``, ``snippet.NotFound`` and a missing store (``FileNotFoundError``) are raised
and ``server.handle`` turns them into envelopes. ``SHRINK`` holds, per tool, the step that drops one
whole unit (a hit, a context unit, a row) or halves a ``get_snippet`` piece when a response is over
the budget (design M3 §4).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path

from . import chunks, locator, models, paths, snippet, store
from . import search as searching  # the module; ``search`` below is the tool

locators = locator  # ``get_snippet`` has an argument named ``locator``

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
NOT_IMPLEMENTED = 'not_implemented'  # stage 0 only, no tool answers it now; not one of ERROR_CODES

# Result shapes (design M3 §2). The search tool's ``result`` is ``searching.RESULT_KEYS`` with
# ``searching.HIT_KEYS`` hits; its ``meta`` is ``searching.META_KEYS`` plus the keys the tool layer adds.
SEARCH_META_EXTRA_KEYS = frozenset({'collapsed', 'truncated_hits', 'signals', 'engine'})  # beyond M2 §5.7
SEARCH_ENGINE_KEYS = frozenset({'model_loaded_now', 'load_ms'})
# A hit's ``units[]`` entry in the tool response: the locator fields that differ per unit; ``v``, ``doc``,
# ``path`` and ``sha256`` are the hit locator's (``expand_units`` rebuilds the full locators). Coordinator
# decision 2026-10-24: a full locator per unit made ten hits overflow the 40,000-character budget.
UNIT_REF_KEYS = frozenset({'unit', 'chars', 'text_sha256'})
SHARED_LOCATOR_KEYS = ('v', 'doc', 'path', 'sha256')
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

def compact_units(hit: dict) -> None:
    """``hit['units']`` in place as ``UNIT_REF_KEYS`` entries (each unit locator shares the hit's version)."""
    shared = {key: hit['locator'][key] for key in SHARED_LOCATOR_KEYS}
    out = []
    for obj in hit['units']:
        if any(obj[key] != value for key, value in shared.items()):
            raise ValueError('a unit locator of another version than its hit')  # never: _hits builds them so
        out.append({'unit': obj['unit'], 'chars': obj['chars'], 'text_sha256': obj['text_sha256']})
    hit['units'] = out


def expand_units(hit: Mapping) -> list[dict]:
    """The full locators of a tool hit's compact ``units[]`` (``locator.LOCATOR_KEYS``, the order
    ``locator.make`` gives), for clients and tests that check them against the store."""
    return [{'v': hit['locator']['v'], 'doc': hit['locator']['doc'], 'path': hit['locator']['path'],
             'sha256': hit['locator']['sha256'], 'unit': ref['unit'], 'chars': ref['chars'],
             'text_sha256': ref['text_sha256']} for ref in hit['units']]


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
    """A shrink step that can drop nothing (``status``)."""
    return False


SHRINK_HITS_NOTICE = '응답 크기 상한으로 hit {}개를 뺐습니다 — limit을 줄이거나 필터를 쓰세요'
SHRINK_CONTEXT_NOTICE = '응답 크기 상한으로 문맥 {}단위를 뺐습니다'
SHRINK_PIECE_NOTICE = '응답 크기 상한으로 조각을 {}자로 줄였습니다'
SHRINK_ROWS_NOTICE = '응답 크기 상한으로 limit {}으로 줄였습니다; offset으로 이어서 받으세요'
READ_ON = 'locator의 chars를 바꿔'
TRUNCATED_NOTICE = ('잘린 조각이 있습니다(truncated: true, locator의 chars가 잘린 범위) — 이어 읽으려면 '
                    f'{READ_ON} get_snippet을 다시 부르세요')


def _set_notice(envelope: dict, template: str, value) -> None:
    """Replace the notice made from ``template`` (with any earlier value) or append it."""
    head = template.split('{}', 1)[0]
    line = template.format(value)
    notice = envelope['notice']
    for at, text in enumerate(notice):
        if text.startswith(head):
            notice[at] = line
            return
    notice.append(line)


def _notice_count(envelope: dict, template: str) -> int:
    """The number in the notice made from ``template``, 0 when there is none."""
    head = template.split('{}', 1)[0]
    for text in envelope['notice']:
        if text.startswith(head) and (match := re.match(r'\d+', text[len(head):])):
            return int(match.group())
    return 0


def shrink_search(envelope: dict) -> bool:
    """Drop the last hit (its snippet leaves ``delivered_sources`` with it); ``meta.truncated_hits``."""
    result = envelope['result']
    if not result['hits']:
        return False
    result['hits'].pop()
    meta = result['meta']
    meta['truncated_hits'] = meta.get('truncated_hits', 0) + 1
    _set_notice(envelope, SHRINK_HITS_NOTICE, meta['truncated_hits'])
    return True


def shrink_snippet(envelope: dict) -> bool:
    """Drop the last unit of ``context_after``, then the first of ``context_before``; then cut the longest
    piece to half its length (``snippet.trim``: word boundary, ``truncated``, a locator for the cut range)."""
    result = envelope['result']
    for key, at in (('context_after', -1), ('context_before', 0)):
        if result[key]:
            result[key].pop(at)
            _set_notice(envelope, SHRINK_CONTEXT_NOTICE, _notice_count(envelope, SHRINK_CONTEXT_NOTICE) + 1)
            return True
    pieces = result['pieces']
    if not pieces:
        return False
    longest = max(range(len(pieces)), key=lambda i: (len(pieces[i]['text']), i))
    size = len(pieces[longest]['text'])
    if size < 2:
        return False
    cut = snippet.trim(pieces[longest], size // 2)
    if len(cut['text']) >= size:
        return False
    pieces[longest] = cut
    _set_notice(envelope, SHRINK_PIECE_NOTICE, max(len(piece['text']) for piece in pieces))
    if not any(READ_ON in line for line in envelope['notice']):  # snippet.notices may have said it already
        envelope['notice'].append(TRUNCATED_NOTICE)
    return True


def _shrink_rows(key: str) -> Callable[[dict], bool]:
    def shrink(envelope: dict) -> bool:
        """Drop the last row; ``limit`` becomes the number of rows returned and ``has_more`` true."""
        result = envelope['result']
        if not result[key]:
            return False
        result[key].pop()
        result['limit'] = len(result[key])
        result['has_more'] = True
        _set_notice(envelope, SHRINK_ROWS_NOTICE, result['limit'])
        return True
    return shrink


# Per tool: drop one unit from the end (a hit, a context unit, a row) or cut a piece; False when
# nothing is left to drop.
SHRINK: dict[str, Callable[[dict], bool]] = {
    'search': shrink_search, 'get_snippet': shrink_snippet, 'list_documents': _shrink_rows('documents'),
    'recent_changes': _shrink_rows('changes'), 'status': shrink_none}


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

def _int_in(value, low: int, high: int | None = None) -> bool:
    return type(value) is int and value >= low and (high is None or value <= high)


def _page_error(limit, offset) -> dict | None:
    if not _int_in(limit, 1, LIST_LIMIT_MAX):
        return error('argument_invalid', f'limit must be an integer 1-{LIST_LIMIT_MAX}')
    if not _int_in(offset, 0):
        return error('argument_invalid', 'offset must be an integer >= 0')
    return None


SEMANTIC_NOTE_NOTICE = '의미 검색 사용 불가({}) — 어휘 결과만'
_NOTE_CODE = re.compile(r'[a-z_]+')
NO_EVENTS_NOTICE = '이 저장소의 과거 scan은 이벤트를 남기지 않았습니다(M3 이전); 다음 scan부터 기록됩니다'


def search(home: Path, engine, query: str, *, mode: str = 'hybrid', limit: int = searching.LIMIT_DEFAULT,
                path_prefix: str | None = None, ext: list[str] | None = None, since: str | None = None,
                until: str | None = None, collapse_by_document: bool = False) -> dict:
    """``search`` (design M3 §2.2): ``engine.search(...)`` over ``home``; ``result`` = M2 §5.7 with compact
    ``units[]`` (``UNIT_REF_KEYS``, see ``expand_units``) plus ``meta.collapsed/truncated_hits/signals/engine``;
    ``delivered_sources`` one line per hit snippet.
    Raises ``search.SearchError`` for bad input (``server.handle`` turns it into an error envelope)."""
    result = engine.search(query, mode=mode, limit=limit, path_prefix=path_prefix, ext=ext, since=since, until=until,
                           collapse_by_document=collapse_by_document)
    for hit in result['hits']:
        compact_units(hit)
    meta = result['meta']
    meta.setdefault('collapsed', 0)
    meta.setdefault('signals', dict(searching.DEFAULT_SIGNALS))
    meta['truncated_hits'] = 0
    meta['engine'] = {'model_loaded_now': False, 'load_ms': 0}  # server.handle fills it in (design M3 §6.3)
    notice = list(result.get('notice') or ())
    note = meta.get('semantic_note')
    if note and mode != 'lexical':  # the reason code only, never the message
        code = _NOTE_CODE.match(note)
        line = SEMANTIC_NOTE_NOTICE.format(code.group() if code else 'unknown')
        if line not in notice:
            notice.append(line)
    envelope = ok(result, notice=notice)
    envelope['delivered_sources'] = delivered(envelope)
    return envelope


def get_snippet(home: Path, *, chunk_id: int | None = None, locator: dict | None = None, context_units: int = 0,
                max_chars: int = PIECE_MAX_CHARS) -> dict:
    """``get_snippet`` (design M3 §2.3): exactly one of ``chunk_id``/``locator``; ``result`` =
    ``SNIPPET_RESULT_KEYS`` (``snippet.by_chunk`` / ``snippet.by_locator``); ``delivered_sources`` =
    ``context_before``, ``pieces``, ``context_after``. Errors ``argument_invalid``, ``locator_invalid``,
    ``not_found``, ``store_missing``."""
    if (chunk_id is None) == (locator is None):
        return error('argument_invalid', 'give exactly one of chunk_id or locator')
    if chunk_id is not None and not _int_in(chunk_id, 1):
        return error('argument_invalid', 'chunk_id must be a positive integer')
    if not _int_in(context_units, 0, CONTEXT_UNITS_MAX):
        return error('argument_invalid', f'context_units must be an integer 0-{CONTEXT_UNITS_MAX}')
    if not _int_in(max_chars, PIECE_MIN_CHARS, PIECE_MAX_CHARS):
        return error('argument_invalid', f'max_chars must be an integer {PIECE_MIN_CHARS}-{PIECE_MAX_CHARS}')
    if locator is not None:
        locators.check_shape(locator)  # LocatorError -> locator_invalid before the store is opened
    db = store.open_read(home)
    try:
        if chunk_id is not None:
            result = snippet.by_chunk(db, chunk_id, context_units=context_units, max_chars=max_chars)
        else:
            result = snippet.by_locator(db, locator, context_units=context_units, max_chars=max_chars)
    finally:
        db.close()
    envelope = ok(result, notice=snippet.notices(result, max_chars))  # old version, deleted, cut pieces
    envelope['delivered_sources'] = delivered(envelope)
    return envelope


def list_documents(home: Path, *, path_prefix: str | None = None, ext: list[str] | None = None, status: str = 'active',
                   changed_since: str | None = None, limit: int = LIST_LIMIT_DEFAULT, offset: int = 0) -> dict:
    """``list_documents`` (design M3 §2.4): ``LIST_RESULT_KEYS`` with ``DOCUMENT_KEYS`` rows
    (``store.documents_query``), no text, ``delivered_sources`` empty."""
    if status not in DOCUMENT_STATUSES:
        return error('argument_invalid', f'status must be one of {", ".join(DOCUMENT_STATUSES)}')
    if failure := _page_error(limit, offset):
        return failure
    checked = searching.check_filters(path_prefix, ext, since=changed_since)
    filters = {'path_prefix': checked['path_prefix'], 'ext': checked['ext'], 'status': status,
               'changed_since': checked['since']}
    db = store.open_read(home)
    try:
        rows, total = store.documents_query(db, filters, limit, offset, chunk_version=chunks.CHUNK_VERSION)
    finally:
        db.close()
    return ok({'documents': rows, 'total': total, 'limit': limit, 'offset': offset,
               'has_more': offset + len(rows) < total})


def recent_changes(home: Path, *, since: str | None = None, until: str | None = None, kinds: list[str] | None = None,
                   path_prefix: str | None = None, ext: list[str] | None = None, limit: int = LIST_LIMIT_DEFAULT,
                   offset: int = 0) -> dict:
    """``recent_changes`` (design M3 §2.5): ``CHANGES_RESULT_KEYS`` with ``CHANGE_KEYS`` rows
    (``store.document_events_query``), newest first, ``since`` default now - 30 days, no text."""
    if kinds is not None and (not isinstance(kinds, list) or not kinds
                              or any(not isinstance(kind, str) or kind not in EVENT_KINDS for kind in kinds)):
        return error('argument_invalid', f'kinds must be a list of {", ".join(EVENT_KINDS)}')
    if failure := _page_error(limit, offset):
        return failure
    if since is None:
        since = (datetime.now().astimezone() - timedelta(days=RECENT_DEFAULT_DAYS)).isoformat(timespec='seconds')
    checked = searching.check_filters(path_prefix, ext, since, until)
    filters = {**checked, 'kinds': sorted(set(kinds)) if kinds else None}
    db = store.open_read(home)
    try:
        rows, total = store.document_events_query(db, filters, limit, offset)
        unrecorded = db.execute('SELECT NOT EXISTS(SELECT 1 FROM document_events) '
                                'AND EXISTS(SELECT 1 FROM scans)').fetchone()[0]
    finally:
        db.close()
    return ok({'changes': rows, 'total': total, 'limit': limit, 'offset': offset,
               'has_more': offset + len(rows) < total, 'since': checked['since'], 'until': checked['until']},
              notice=[NO_EVENTS_NOTICE] if unrecorded else [])


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

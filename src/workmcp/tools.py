"""MCP tool implementations as plain functions (dict in, envelope dict out; design M0 §4, M3 §2).

Every response is an envelope ``{"ok": true, "result", "delivered_sources", "notice"}`` or
``{"ok": false, "error": {"code", "message", "retryable"}}``. ``delivered_sources`` lists, in
order, one ``locator.display`` line per piece of document text the response carries (search
snippets, ``get_snippet`` pieces and context, stored evidence quotes of the knowledge tools) and nothing
else (design M3 §3.1, M4 §7.1); ``pieces_of`` finds those pieces from the response structure.
``list_documents``, ``recent_changes``, ``status``, ``find_entity`` and ``list_knowledge`` carry none.
Nothing here knows the MCP SDK; ``server.handle`` serializes with ``dumps``.

Argument errors come back as error envelopes (``argument_invalid`` here); ``search.SearchError``,
``locator.LocatorError``, ``snippet.NotFound``, ``knowledge.KnowledgeError`` and a missing store
(``FileNotFoundError``) are raised and ``server.handle`` turns them into envelopes. The six knowledge
writers take the knowledge write connection plus ``recorded_by`` (``"<client>/<tool>"``) and ``call_id``
from ``server``; the four knowledge readers take the home (design M4 §1, §7.2). ``SHRINK`` holds, per tool, the step that drops one
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

from . import chunks, knowledge, knowledge_query, locator, models, paths, snippet, store
from . import search as searching  # the module; ``search`` below is the tool

locators = locator  # ``get_snippet`` has an argument named ``locator``

READ_TOOL_NAMES = ('search', 'get_snippet', 'list_documents', 'recent_changes', 'status')
WRITE_TOOL_NAMES = ('record_entity', 'record_fact', 'record_decision', 'record_task', 'supersede', 'retract')
KNOWLEDGE_READ_TOOL_NAMES = ('find_entity', 'get_entity', 'list_knowledge', 'get_history')
TOOL_NAMES = (*READ_TOOL_NAMES, *WRITE_TOOL_NAMES, *KNOWLEDGE_READ_TOOL_NAMES)

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
               'response_too_large', 'internal_error',
               # M4 (design M4 §6.1): the knowledge tools
               'evidence_required', 'evidence_quote_not_found', 'evidence_quote_ambiguous', 'chunk_not_found',
               'entity_not_found', 'assertion_not_found', 'already_closed', 'kind_mismatch')
NOT_IMPLEMENTED = 'not_implemented'  # stage 0 only (M4: the ten knowledge tools); not one of ERROR_CODES

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
                         'engine', 'home_virtualization', 'calls', 'process', 'knowledge'})
KNOWLEDGE_STATUS_KEYS = frozenset({'entities', 'assertions', 'tasks_open', 'evidence', 'events'})  # store.knowledge_counts
ENGINE_STATUS_EXTRA_KEYS = frozenset({'idle_seconds', 'model_idle_seconds', 'loads', 'unloads'})  # + Engine.status()

# Knowledge tools (design M4 §6): one assertion and one entity representation (``knowledge_query``).
ASSERTION_KEYS = knowledge_query.ASSERTION_KEYS
EVIDENCE_KEYS = knowledge_query.EVIDENCE_KEYS
CONFLICT_KEYS = knowledge_query.CONFLICT_KEYS
ENTITY_KEYS = knowledge_query.ENTITY_KEYS
ENTITY_COUNT_KEYS = knowledge_query.ENTITY_COUNT_KEYS
RECORD_ENTITY_RESULT_KEYS = frozenset({'entity', 'created', 'added_aliases', 'alias_of'})
RECORD_RESULT_KEYS = frozenset({'assertion', 'conflicts', 'duplicates'})      # record_fact/decision/task
SUPERSEDE_RESULT_KEYS = frozenset({'old', 'cut', 'new', 'conflicts', 'duplicates', 'cleared_conflicts'})
RETRACT_RESULT_KEYS = frozenset({'assertion', 'cleared_conflicts'})
FIND_RESULT_KEYS = knowledge_query.FIND_RESULT_KEYS
ENTITY_RESULT_KEYS = knowledge_query.ENTITY_RESULT_KEYS
KNOWLEDGE_LIST_RESULT_KEYS = knowledge_query.LIST_RESULT_KEYS
HISTORY_RESULT_KEYS = knowledge_query.HISTORY_RESULT_KEYS
# Read tool limits (design M4 §6.3); the writers' text limits are ``knowledge.*_MAX``.
FIND_QUERY_MAX = 200
FIND_LIMIT_DEFAULT, FIND_LIMIT_MAX = 20, 50
ENTITY_LIMIT_DEFAULT, ENTITY_LIMIT_MAX = 50, 100
KNOWLEDGE_LIMIT_DEFAULT, KNOWLEDGE_LIMIT_MAX = 50, 200


def ok(result, *, delivered_sources=(), notice=()) -> dict:
    return {'ok': True, 'result': result, 'delivered_sources': list(delivered_sources), 'notice': list(notice)}


ERROR_MESSAGE_MAX = 200  # an error message may name what the client sent; never more than this (review M3-3)


def error(code: str, message: str, *, retryable: bool = False) -> dict:
    if len(message) > ERROR_MESSAGE_MAX:
        message = message[:ERROR_MESSAGE_MAX - 1] + '…'
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


PIECE_TEXT_KEYS = ('snippet', 'text', 'quote')


def pieces_of(envelope: Mapping) -> list[tuple[dict, str]]:
    """``(locator, text)`` of every piece of document text in a response, in JSON order (dict keys in
    order, then list items; design M4 §7.1): any dict in ``result`` with a ``locator`` dict and exactly
    one of ``snippet``/``text``/``quote`` as a string: a search hit, a ``get_snippet`` context unit or
    piece (``context_before``, ``pieces``, ``context_after``, in that key order), a stored evidence quote.
    ``units[]`` (no locator), ``requote`` (a locator without text), a dropped quote (None), paths,
    ``notice`` and ``meta`` are not pieces; an error envelope has none."""
    if not envelope.get('ok'):
        return []
    found: list[tuple[dict, str]] = []

    def walk(node) -> None:
        if isinstance(node, Mapping):
            texts = [key for key in PIECE_TEXT_KEYS if isinstance(node.get(key), str)]
            if isinstance(node.get('locator'), Mapping) and len(texts) == 1:
                found.append((node['locator'], node[texts[0]]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(envelope.get('result'))
    return found


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
SHRINK_QUOTES_NOTICE = '응답 크기 상한으로 인용 {}건을 뺐습니다(quote: null, quote_chars) — get_history로 하나씩 받으세요'
SHRINK_ENTITY_NOTICE = '응답 크기 상한으로 목록 행 {}개를 뺐습니다(has_more 참고) — limit을 줄이거나 list_knowledge로 받으세요'
SHRINK_HISTORY_NOTICE = '응답 크기 상한으로 이력의 오래된 행 {}개를 뺐습니다(truncated: true)'
ENTITY_SHRINK_ORDER = ('tasks', 'decisions', 'related', 'facts')   # design M4 §6.5: tasks go first


def quote_items(node) -> list[dict]:
    """The evidence items that carry a quote (a dict with a ``locator`` and a string ``quote``), in JSON
    order (``pieces_of``'s order)."""
    found = []
    if isinstance(node, Mapping):
        if isinstance(node.get('locator'), Mapping) and isinstance(node.get('quote'), str):
            found.append(node)
        for value in node.values():
            found += quote_items(value)
    elif isinstance(node, list):
        for item in node:
            found += quote_items(item)
    return found


def _drop_quote(envelope: dict, item: dict) -> None:
    """``quote`` to None (``quote_chars`` keeps its length): it leaves the response and delivered_sources."""
    item['quote_chars'] = len(item['quote'])
    item['quote'] = None
    _set_notice(envelope, SHRINK_QUOTES_NOTICE, _notice_count(envelope, SHRINK_QUOTES_NOTICE) + 1)


def shrink_quotes(envelope: dict) -> bool:
    """Write responses (design M4 §6.5): drop the last quote still in the response."""
    items = quote_items(envelope['result'])
    if not items:
        return False
    _drop_quote(envelope, items[-1])
    return True


def shrink_entity(envelope: dict) -> bool:
    """``get_entity``: drop the last quote still in the response (its locator stays; get_history gives it
    again), then the last row of tasks, decisions, related, facts (``has_more`` true). Quotes go first
    because they live in the rows: dropping the rows first would leave no quote to drop (design M4 §6.5
    lists rows first)."""
    if shrink_quotes(envelope):
        return True
    result = envelope['result']
    for key in ENTITY_SHRINK_ORDER:
        if result.get(key):
            result[key].pop()
            result['has_more'][key] = True
            _set_notice(envelope, SHRINK_ENTITY_NOTICE, _notice_count(envelope, SHRINK_ENTITY_NOTICE) + 1)
            return True
    return False


def shrink_history(envelope: dict) -> bool:
    """``get_history``: drop the quotes from the root on, then the oldest rows (``truncated`` true)."""
    result = envelope['result']
    if items := quote_items(result['chain']):
        _drop_quote(envelope, items[0])
        return True
    if not result['chain']:
        return False
    result['chain'].pop(0)
    result['truncated'] = True
    _set_notice(envelope, SHRINK_HISTORY_NOTICE, _notice_count(envelope, SHRINK_HISTORY_NOTICE) + 1)
    return True


SHRINK: dict[str, Callable[[dict], bool]] = {
    'search': shrink_search, 'get_snippet': shrink_snippet, 'list_documents': _shrink_rows('documents'),
    'recent_changes': _shrink_rows('changes'), 'status': shrink_none,
    **{name: shrink_quotes for name in WRITE_TOOL_NAMES}, 'find_entity': _shrink_rows('entities'),
    'get_entity': shrink_entity, 'list_knowledge': _shrink_rows('items'), 'get_history': shrink_history}


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
               'recent_changes': ('since', 'until', 'kinds', 'path_prefix', 'ext'),
               'list_knowledge': ('entity_id', 'predicate', 'state', 'due_before', 'as_of_valid', 'as_of_recorded',
                                  'recorded_since')}
RECORD_KINDS = {'record_fact': 'fact', 'record_decision': 'decision', 'record_task': 'todo'}
SUPERSEDE_FIELDS = tuple(dict.fromkeys(name for names in knowledge.FIELDS.values() for name in names))


def _count(value):
    return value if type(value) is int else None


def _choice(value, allowed):
    return value if value is None or value in allowed else 'invalid'


def _chars(value) -> int:
    return len(value) if isinstance(value, str) else 0


def _sources_summary(args: Mapping) -> dict:
    """``{sources, by, my_note, quote_chars}`` of a writer's evidence arguments: counts and lengths only."""
    sources = args.get('sources')
    items = [item for item in sources if isinstance(item, Mapping)] if isinstance(sources, list) else []
    return {'sources': len(sources) if isinstance(sources, list) else 0,
            'by': sorted({key for item in items for key in ('chunk_id', 'locator') if item.get(key) is not None}),
            'my_note': args.get('my_note') is True,
            'quote_chars': sum(_chars(item.get('quote')) for item in items)}


def _knowledge_summary(tool: str, args: Mapping) -> dict | None:
    """``args_json`` of the knowledge tools (design M4 §7.3): ids, counts, lengths, booleans and fixed
    choices; never a statement, value, quote, name, alias, note, reason, predicate or date. None for
    another tool."""
    if tool == 'record_entity':
        aliases = args.get('aliases')
        return {'kind': _choice(args.get('kind'), knowledge.ENTITY_KINDS), 'name_chars': _chars(args.get('name')),
                'aliases': len(aliases) if isinstance(aliases, list) else 0, 'note': args.get('note') is not None}
    if tool in RECORD_KINDS:
        kind = RECORD_KINDS[tool]
        valid_from = args.get('decided_on' if kind == 'decision' else 'valid_from')
        summary = {'kind': kind, 'entity': _count(args.get('entity_id')),
                   'object': _count(args.get('object_entity_id')), 'predicate_chars': _chars(args.get('predicate')),
                   'statement_chars': _chars(args.get('statement')), 'value_chars': _chars(args.get('value')),
                   **_sources_summary(args), 'valid': [valid_from is not None, args.get('valid_to') is not None]}
        if kind == 'todo':
            summary['state'] = _choice(args.get('state'), knowledge.TASK_STATES)
        return summary
    if tool == 'supersede':
        return {'assertion': _count(args.get('assertion_id')),
                'fields': sorted(name for name in SUPERSEDE_FIELDS if args.get(name) is not None),
                'old_valid_to': args.get('old_valid_to') is not None, **_sources_summary(args),
                'reason_chars': _chars(args.get('reason'))}
    if tool == 'retract':
        return {'assertion': _count(args.get('assertion_id')), 'reason_chars': _chars(args.get('reason'))}
    if tool == 'find_entity':
        return {'query_chars': _chars(args.get('query')), 'kind': _choice(args.get('kind'), knowledge.ENTITY_KINDS),
                'limit': _count(args.get('limit'))}
    if tool == 'get_entity':
        by = 'id' if args.get('entity_id') is not None else 'name' if args.get('name') is not None else None
        return {'by': by, 'entity': _count(args.get('entity_id')),
                'as_of': [args.get('as_of_valid') is not None, args.get('as_of_recorded') is not None],
                'include_closed': args.get('include_closed') is True, 'limit': _count(args.get('limit'))}
    if tool == 'list_knowledge':
        return {'filters': sorted(key for key in FILTER_ARGS[tool] if args.get(key) is not None),
                'kind': _choice(args.get('kind'), knowledge_query.LIST_KINDS),
                'status': _choice(args.get('status'), knowledge_query.LIST_STATUSES),
                'state': _choice(args.get('state'), knowledge.TASK_STATES),
                'source_kind': _choice(args.get('source_kind'), knowledge_query.SOURCE_FILTERS),
                'entity': _count(args.get('entity_id')), 'limit': _count(args.get('limit')),
                'offset': _count(args.get('offset'))}
    if tool == 'get_history':
        return {'assertion': _count(args.get('assertion_id'))}
    return None


def summarize_args(tool: str, args: Mapping) -> dict:
    """``tool_calls.args_json``: which arguments were used, never the query, a path or a locator body.
    The query is kept as its length and the first 12 hex digits of its SHA-256 only. The knowledge tools
    keep ids, counts, lengths, booleans and fixed choices only (``_knowledge_summary``)."""
    if (summary := _knowledge_summary(tool, args)) is not None:
        return summary
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
CHUNK_GONE_HINT = ('scan/index가 청크를 바꿨을 수 있습니다 — 다시 검색하거나 hit의 locator로 get_snippet을 '
                   '부르세요(옛 버전도 locator로는 나옵니다)')
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
            try:
                result = snippet.by_chunk(db, chunk_id, context_units=context_units, max_chars=max_chars)
            except snippet.NotFound as exc:  # review L5: chunk ids of an older search may be gone
                raise snippet.NotFound(f'{exc}; {CHUNK_GONE_HINT}') from None
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


def status(home: Path, engine=None, *, started_at: str | None = None, unloads: int = 0, pending: int = 0) -> dict:
    """Data home, store counts, last finished scan and index run, model availability, FTS5 support,
    the FTS index state, the search engine of this process, app virtualization of the home, tool
    calls per client and this process (design M3 §2.6), knowledge counts (design M4 §6.4); no source text."""
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
        knowledge_counts = store.knowledge_counts(db)
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
        'calls': {**calls, 'pending': pending},  # pending: calls this server could not log yet
        'process': {'pid': os.getpid(), 'started_at': started_at, 'python': list(sys.version_info[:3])},
        'knowledge': knowledge_counts,
    }
    notice = ['아직 수집(scan)한 적이 없습니다.'] if result['last_scan'] is None else []
    fts = index['fts']
    if fts['available'] and not fts['consistent']:
        notice.append(f"전문 색인(chunks_fts)이 청크와 맞지 않습니다(빠짐 {fts['missing']}, 남은 항목 {fts['extra']}"
                      f"{', FTS5 없이 바뀐 청크 있음' if fts['dirty'] else ''}) - "
                      f'workmcp index --rebuild-fts를 실행하세요.')
    return ok(result, notice=notice)


# ---- the ten knowledge tools (design M4 §6) ---------------------------------------------------------
# Order of checks as in M3: types and ranges here (argument_invalid, evidence_required), then the store,
# then the work in ``knowledge`` / ``knowledge_query`` (NFC, lengths, ids, binding: KnowledgeError).

_RULES: dict[str, tuple[Callable[[object], bool], str]] = {
    'text': (lambda v: isinstance(v, str), 'a string'),
    'texts': (lambda v: isinstance(v, list) and all(isinstance(i, str) for i in v), 'a list of strings'),
    'id': (lambda v: _int_in(v, 1), 'a positive integer'),
    'bool': (lambda v: type(v) is bool, 'true or false'),
}


def _instant(value, name: str) -> str:
    """An ISO 8601 date or time as an aware ISO string (``search``'s rule); ``KnowledgeError
    ('argument_invalid')`` naming ``name``."""
    try:
        return searching._instant(value, name)  # noqa: SLF001 - the one ISO rule of the package
    except searching.SearchError as exc:
        raise knowledge.KnowledgeError('argument_invalid', exc.message) from None


def _check(rules: Mapping[str, object], args: Mapping) -> dict | None:
    """The first ``argument_invalid`` envelope for ``args`` under ``rules`` (name -> ``'text'``, ``'texts'``,
    ``'id'``, ``'bool'``, ``'date'``, ``'instant'``, or a tuple of allowed values; a trailing ``?`` on a
    rule name allows None), else None. Dates and instants are checked by their parsers."""
    for name, rule in rules.items():
        value = args.get(name)
        if isinstance(rule, tuple):
            if value is not None and value not in rule or value is None and None not in rule:
                choices = '|'.join(str(choice) for choice in rule if choice is not None)
                return error('argument_invalid', f'{name} must be one of {choices}')
            continue
        if rule.endswith('?'):
            if value is None:
                continue
            rule = rule[:-1]
        try:
            if rule == 'date':
                knowledge.check_date(value, name)
            elif rule == 'instant':
                _instant(value, name)
            elif not _RULES[rule][0](value):
                return error('argument_invalid', f'{name} must be {_RULES[rule][1]}')
        except knowledge.KnowledgeError as exc:
            return error(exc.code, exc.message)
    return None


SOURCE_ITEM_KEYS = frozenset({'chunk_id', 'locator', 'quote'})


def _evidence_error(sources, my_note) -> dict | None:
    """``evidence_required`` unless exactly one of ``sources`` (a list of 1-``knowledge.SOURCES_MAX``
    ``{chunk_id | locator, quote}``) or ``my_note: true`` is given; ``argument_invalid`` for a bad shape."""
    if type(my_note) is not bool:
        return error('argument_invalid', 'my_note must be true or false')
    if sources is not None and not isinstance(sources, list):
        return error('argument_invalid', 'sources must be a list')
    if (sources is not None) == my_note or sources == []:
        return error('evidence_required', 'give exactly one of sources (a non-empty list) or my_note: true')
    for n, item in enumerate(sources or (), 1):
        if not isinstance(item, Mapping) or set(item) - SOURCE_ITEM_KEYS:
            return error('argument_invalid', f'sources[{n}] must be an object with chunk_id or locator, and quote')
        if (item.get('chunk_id') is None) == (item.get('locator') is None):
            return error('argument_invalid', f'sources[{n}] needs exactly one of chunk_id or locator')
        if item.get('chunk_id') is not None and not _int_in(item['chunk_id'], 1):
            return error('argument_invalid', f'sources[{n}].chunk_id must be a positive integer')
        if item.get('locator') is not None and not isinstance(item['locator'], Mapping):
            return error('argument_invalid', f'sources[{n}].locator must be an object')
        if not isinstance(item.get('quote'), str):
            return error('argument_invalid', f'sources[{n}].quote must be a string')
    if sources is not None and len(sources) > knowledge.SOURCES_MAX:
        return error('argument_invalid', f'sources must have 1-{knowledge.SOURCES_MAX} items')
    return None


def knowledge_envelope(out: Mapping) -> dict:
    """The envelope of a ``knowledge``/``knowledge_query`` result: ``out`` without ``notice`` as ``result``,
    its ``notice`` lines, ``delivered_sources`` from the evidence quotes it carries."""
    result = {key: value for key, value in out.items() if key != 'notice'}
    envelope = ok(result, notice=list(out.get('notice') or ()))
    envelope['delivered_sources'] = delivered(envelope)
    return envelope


def _written(db, write: Callable[[], Mapping]) -> dict:
    """The envelope of one knowledge write; ``store_missing`` when the server has no write connection."""
    if db is None:
        return error('store_missing', 'this server has no knowledge write connection')
    return knowledge_envelope(write())


def _read(home: Path, read: Callable[..., Mapping]) -> dict:
    """The envelope of one knowledge read on a read-only connection (``FileNotFoundError``: store_missing)."""
    db = store.open_read(home)
    try:
        return knowledge_envelope(read(db))
    finally:
        db.close()


def record_entity(db, *, recorded_by: str, call_id: str | None, kind: str, name: str,
                  aliases: list[str] | None = None, note: str | None = None) -> dict:
    """``record_entity`` (design M4 §6.2): ``RECORD_ENTITY_RESULT_KEYS`` (``knowledge.record_entity``)."""
    if failure := _check({'kind': knowledge.ENTITY_KINDS, 'name': 'text', 'aliases': 'texts?', 'note': 'text?'},
                         {'kind': kind, 'name': name, 'aliases': aliases, 'note': note}):
        return failure
    return _written(db, lambda: knowledge.record_entity(db, kind=kind, name=name, aliases=aliases, note=note,
                                                       recorded_by=recorded_by))


FACT_RULES = {'entity_id': 'id', 'predicate': 'text', 'value': 'text?', 'unit': 'text?', 'object_entity_id': 'id?',
              'statement': 'text?', 'valid_from': 'date?', 'valid_to': 'date?'}
DECISION_RULES = {'statement': 'text', 'entity_id': 'id?', 'options': 'texts?', 'chosen': 'text?',
                  'rationale': 'text?', 'decided_on': 'date?', 'valid_to': 'date?'}
TASK_RULES = {'statement': 'text', 'entity_id': 'id?', 'due': 'date?', 'owner_entity_id': 'id?',
              'state': knowledge.TASK_STATES, 'done_at': 'date?'}
# supersede: every content argument is optional (copied from the old row when absent).
SUPERSEDE_RULES = {'assertion_id': 'id', 'reason': 'text', 'old_valid_to': 'date?',
                   **{name: rule if isinstance(rule, tuple) else rule.rstrip('?') + '?'
                      for rules in (FACT_RULES, DECISION_RULES, TASK_RULES) for name, rule in rules.items()},
                   'state': (*knowledge.TASK_STATES, None)}


def record_fact(db, *, recorded_by: str, call_id: str | None, entity_id: int, predicate: str,
                value: str | None = None, unit: str | None = None, object_entity_id: int | None = None,
                statement: str | None = None, valid_from: str | None = None, valid_to: str | None = None,
                sources: list[dict] | None = None, my_note: bool = False) -> dict:
    """``record_fact`` (design M4 §6.2): ``RECORD_RESULT_KEYS`` (``knowledge.record(db, 'fact', ...)``)."""
    args = {'entity_id': entity_id, 'predicate': predicate, 'value': value, 'unit': unit,
            'object_entity_id': object_entity_id, 'statement': statement, 'valid_from': valid_from,
            'valid_to': valid_to}
    if failure := _check(FACT_RULES, args) or _evidence_error(sources, my_note):
        return failure
    return _written(db, lambda: knowledge.record(db, 'fact', recorded_by=recorded_by, call_id=call_id, sources=sources,
                                                my_note=my_note, **args))


def record_decision(db, *, recorded_by: str, call_id: str | None, statement: str, entity_id: int | None = None,
                    options: list[str] | None = None, chosen: str | None = None, rationale: str | None = None,
                    decided_on: str | None = None, valid_to: str | None = None, sources: list[dict] | None = None,
                    my_note: bool = False) -> dict:
    """``record_decision`` (design M4 §6.2): ``RECORD_RESULT_KEYS`` (``knowledge.record(db, 'decision', ...)``)."""
    args = {'statement': statement, 'entity_id': entity_id, 'options': options, 'chosen': chosen,
            'rationale': rationale, 'decided_on': decided_on, 'valid_to': valid_to}
    if failure := _check(DECISION_RULES, args) or _evidence_error(sources, my_note):
        return failure
    return _written(db, lambda: knowledge.record(db, 'decision', recorded_by=recorded_by, call_id=call_id,
                                                sources=sources, my_note=my_note, **args))


def record_task(db, *, recorded_by: str, call_id: str | None, statement: str, entity_id: int | None = None,
                due: str | None = None, owner_entity_id: int | None = None, state: str = 'open',
                done_at: str | None = None, sources: list[dict] | None = None, my_note: bool = False) -> dict:
    """``record_task`` (design M4 §6.2): ``RECORD_RESULT_KEYS`` (``knowledge.record(db, 'todo', ...)``)."""
    args = {'statement': statement, 'entity_id': entity_id, 'due': due, 'owner_entity_id': owner_entity_id,
            'state': state, 'done_at': done_at}
    if failure := _check(TASK_RULES, args) or _evidence_error(sources, my_note):
        return failure
    return _written(db, lambda: knowledge.record(db, 'todo', recorded_by=recorded_by, call_id=call_id, sources=sources,
                                                my_note=my_note, **args))


def supersede(db, *, recorded_by: str, call_id: str | None, assertion_id: int, reason: str,
              sources: list[dict] | None = None, my_note: bool = False, old_valid_to: str | None = None,
              entity_id: int | None = None, predicate: str | None = None, value: str | None = None,
              unit: str | None = None, object_entity_id: int | None = None, statement: str | None = None,
              valid_from: str | None = None, valid_to: str | None = None, options: list[str] | None = None,
              chosen: str | None = None, rationale: str | None = None, decided_on: str | None = None,
              due: str | None = None, owner_entity_id: int | None = None, state: str | None = None,
              done_at: str | None = None) -> dict:
    """``supersede`` (design M4 §4.3, §6.2): ``SUPERSEDE_RESULT_KEYS`` (``knowledge.supersede``); content
    arguments not given are copied from the old row, one of another kind is ``kind_mismatch``."""
    args = {'assertion_id': assertion_id, 'reason': reason, 'old_valid_to': old_valid_to, 'entity_id': entity_id,
            'predicate': predicate, 'value': value, 'unit': unit, 'object_entity_id': object_entity_id,
            'statement': statement, 'valid_from': valid_from, 'valid_to': valid_to, 'options': options,
            'chosen': chosen, 'rationale': rationale, 'decided_on': decided_on, 'due': due,
            'owner_entity_id': owner_entity_id, 'state': state, 'done_at': done_at}
    if failure := _check(SUPERSEDE_RULES, args) or _evidence_error(sources, my_note):
        return failure
    fields = {name: value for name, value in args.items() if name not in ('assertion_id', 'reason', 'old_valid_to')}
    return _written(db, lambda: knowledge.supersede(db, assertion_id, reason=reason, recorded_by=recorded_by,
                                                   call_id=call_id, sources=sources, my_note=my_note,
                                                   old_valid_to=old_valid_to, **fields))


def retract(db, *, recorded_by: str, call_id: str | None, assertion_id: int, reason: str) -> dict:
    """``retract`` (design M4 §4.2, §6.2): ``RETRACT_RESULT_KEYS`` (``knowledge.retract``)."""
    if failure := _check({'assertion_id': 'id', 'reason': 'text'}, {'assertion_id': assertion_id, 'reason': reason}):
        return failure
    return _written(db, lambda: knowledge.retract(db, assertion_id, reason=reason, recorded_by=recorded_by,
                                                 call_id=call_id))


def find_entity(home: Path, *, query: str, kind: str | None = None, limit: int = FIND_LIMIT_DEFAULT) -> dict:
    """``find_entity`` (design M4 §6.3): ``FIND_RESULT_KEYS`` (``knowledge_query.find_entities``), no text,
    ``delivered_sources`` empty."""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= FIND_QUERY_MAX:
        return error('argument_invalid', f'query must be 1-{FIND_QUERY_MAX} characters')
    if failure := _check({'kind': (*knowledge.ENTITY_KINDS, None)}, {'kind': kind}):
        return failure
    if not _int_in(limit, 1, FIND_LIMIT_MAX):
        return error('argument_invalid', f'limit must be an integer 1-{FIND_LIMIT_MAX}')
    query = knowledge.norm_text(query, 'query', FIND_QUERY_MAX)
    return _read(home, lambda db: knowledge_query.find_entities(db, query, kind, limit))


AS_OF_RULES = {'as_of_valid': 'date?', 'as_of_recorded': 'instant?', 'source_kind': knowledge_query.SOURCE_FILTERS}


def get_entity(home: Path, *, entity_id: int | None = None, kind: str | None = None, name: str | None = None,
               as_of_valid: str | None = None, as_of_recorded: str | None = None, include_closed: bool = False,
               source_kind: str = 'all', limit: int = ENTITY_LIMIT_DEFAULT) -> dict:
    """``get_entity`` (design M4 §6.3): ``ENTITY_RESULT_KEYS`` (``knowledge_query.entity_detail``) by
    ``entity_id`` or by exact ``kind`` + ``name``; assertions carry evidence quotes (``delivered_sources``)."""
    by_name = kind is not None or name is not None
    if (entity_id is not None) == by_name or (by_name and (kind is None or name is None)):
        return error('argument_invalid', 'give entity_id, or kind and name')
    rules = {'entity_id': 'id?', 'kind': (*knowledge.ENTITY_KINDS, None), 'name': 'text?',
             'include_closed': 'bool', **AS_OF_RULES}
    if failure := _check(rules, {'entity_id': entity_id, 'kind': kind, 'name': name, 'include_closed': include_closed,
                                 'as_of_valid': as_of_valid, 'as_of_recorded': as_of_recorded,
                                 'source_kind': source_kind}):
        return failure
    if not _int_in(limit, 1, ENTITY_LIMIT_MAX):
        return error('argument_invalid', f'limit must be an integer 1-{ENTITY_LIMIT_MAX}')
    recorded = None if as_of_recorded is None else _instant(as_of_recorded, 'as_of_recorded')
    name = None if name is None else knowledge.norm_text(name, 'name', knowledge.NAME_MAX)

    def read(db):
        found = entity_id if entity_id is not None else knowledge_query.entity_by_name(db, kind, name)
        return knowledge_query.entity_detail(db, found, as_of_valid=as_of_valid, as_of_recorded=recorded,
                                             include_closed=include_closed, source_kind=source_kind, limit=limit)
    return _read(home, read)


def list_knowledge(home: Path, *, kind: str = 'all', entity_id: int | None = None, predicate: str | None = None,
                   status: str = 'open', state: str | None = None, due_before: str | None = None,
                   source_kind: str = 'all', as_of_valid: str | None = None, as_of_recorded: str | None = None,
                   recorded_since: str | None = None, limit: int = KNOWLEDGE_LIMIT_DEFAULT, offset: int = 0) -> dict:
    """``list_knowledge`` (design M4 §6.3): ``KNOWLEDGE_LIST_RESULT_KEYS`` (``knowledge_query.list_assertions``),
    no quotes, ``delivered_sources`` empty."""
    rules = {'kind': knowledge_query.LIST_KINDS, 'entity_id': 'id?', 'predicate': 'text?',
             'status': knowledge_query.LIST_STATUSES, 'state': (*knowledge.TASK_STATES, None), 'due_before': 'date?',
             'recorded_since': 'instant?', **AS_OF_RULES}
    if failure := _check(rules, {'kind': kind, 'entity_id': entity_id, 'predicate': predicate, 'status': status,
                                 'state': state, 'due_before': due_before, 'source_kind': source_kind,
                                 'as_of_valid': as_of_valid, 'as_of_recorded': as_of_recorded,
                                 'recorded_since': recorded_since}):
        return failure
    if not _int_in(limit, 1, KNOWLEDGE_LIMIT_MAX):
        return error('argument_invalid', f'limit must be an integer 1-{KNOWLEDGE_LIMIT_MAX}')
    if not _int_in(offset, 0):
        return error('argument_invalid', 'offset must be an integer >= 0')
    filters = {'kind': kind, 'entity_id': entity_id,
               'predicate': None if predicate is None else knowledge.norm_text(predicate, 'predicate',
                                                                               knowledge.PREDICATE_MAX),
               'status': status, 'state': state, 'due_before': due_before, 'source_kind': source_kind,
               'as_of_valid': as_of_valid,
               'as_of_recorded': None if as_of_recorded is None else _instant(as_of_recorded, 'as_of_recorded'),
               'recorded_since': None if recorded_since is None else _instant(recorded_since, 'recorded_since')}
    return _read(home, lambda db: knowledge_query.list_assertions(db, filters, limit, offset))


def get_history(home: Path, *, assertion_id: int) -> dict:
    """``get_history`` (design M4 §5.2): ``HISTORY_RESULT_KEYS`` (``knowledge_query.history``); evidence quotes
    in ``delivered_sources``."""
    if failure := _check({'assertion_id': 'id'}, {'assertion_id': assertion_id}):
        return failure
    return _read(home, lambda db: knowledge_query.history(db, assertion_id))

"""Knowledge read path (design M4 §3.4, §5, §6.3): the two time axes, entity search and detail, assertion
lists, the history of one assertion, and the state of stored sources. Read connections only
(``store.open_read``); the writers in ``knowledge`` also call ``assertion_row`` and ``entity_row`` on their
write connection after the commit, so writes and reads share one representation.

Time axes (design M4 §5.1): ``as_of_recorded`` (aware ISO time, default now) selects what the record knew
then; ``as_of_valid`` (``YYYY-MM-DD``, default none = every validity period) selects what was true then.
Both may be given (AND).

Every function that feeds a tool returns the tool's ``result`` dict plus ``'notice'`` (a list of lines)
which ``tools`` moves into the envelope. Errors are ``knowledge.KnowledgeError`` (``entity_not_found``,
``assertion_not_found``, ``argument_invalid``). Import in module form only (``from . import knowledge``):
``knowledge`` imports this module too.

Source state (design M4 §3.4): an ``evidence`` row never changes and names one version; when it is read,
its document's status and whether that version is still the latest give ``source_state`` (the document
status first: ``deleted``, ``skipped``; then ``changed`` for a superseded version, or when the stored unit
no longer gives the quote at its locator — ``scan --reparse`` refills the units of the same version, review
M4 v1 M1; else ``current``).
For a ``changed`` source, get_entity and get_history add ``requote``: whether the same unit of the latest
version still holds the quote exactly once, with that locator as a suggestion (never stored).

This module reads stored rows only: no file is opened, no row is written, and no statement, value,
quote or name goes into an error message.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime

from . import knowledge, locator, parsers, store

# One assertion as every knowledge tool shows it (design M4 §6.1). ``evidence`` is a list where the tool
# carries quotes (write responses, get_entity, get_history) and None in list_knowledge; ``evidence_count``
# is always there. ``source_state`` is the worst state of the row's sources (None for a note).
# ``superseded_by_id`` is the row that closed this one (its ``superseded`` event; None while open);
# ``continued_by_ids`` are the other rows whose ``supersedes_id`` is this one (the cut row C of an old row,
# the later-period row N of an open C; review M4 v1 M2). ``conflicts`` holds at most ``CONFLICTS_MAX``
# entries, ``conflicts_more`` counts the rest (review L3).
ASSERTION_KEYS = frozenset({'id', 'kind', 'entity_id', 'entity', 'predicate', 'object_entity_id', 'object',
                            'statement', 'data', 'valid_from', 'valid_to', 'recorded_from', 'recorded_to', 'status',
                            'status_as_of', 'source_kind', 'source_label', 'source_state', 'supersedes_id',
                            'superseded_by_id', 'continued_by_ids', 'recorded_by', 'conflicts', 'conflicts_more',
                            'evidence', 'evidence_count'})
CONFLICTS_MAX = 20
ENTITY_REF_KEYS = frozenset({'kind', 'name'})              # ``entity`` / ``object`` of an assertion
CONFLICT_KEYS = frozenset({'id', 'value', 'valid_from', 'valid_to', 'recorded_from'})
# ``quote`` is document text (a piece for delivered_sources); None when the size budget dropped it, then
# ``quote_chars`` still tells its length. ``requote`` (get_entity, get_history; None elsewhere and for a
# source that did not change): ``{'found': bool, 'locator'?}``, a suggestion, never stored.
EVIDENCE_KEYS = frozenset({'id', 'locator', 'quote', 'quote_chars', 'source_state', 'requote'})
SOURCE_STATES = ('current', 'changed', 'deleted', 'skipped')
SOURCE_STATE_ORDER = ('deleted', 'changed', 'skipped', 'current')   # worst first (design M4 §3.4)
ENTITY_KEYS = frozenset({'id', 'kind', 'name', 'aliases', 'note', 'created_at', 'created_by', 'merged_into',
                         'counts'})
ENTITY_COUNT_KEYS = frozenset({'facts_open', 'decisions_open', 'tasks_open', 'conflicts'})
EVENT_KEYS = frozenset({'id', 'at', 'kind', 'reason', 'related_id', 'recorded_by'})
LINK_KEYS = frozenset({'id', 'from_id', 'to_id', 'relation', 'recorded_at', 'recorded_by', 'resolved_at',
                       'resolved_by_id'})
HISTORY_ROW_EXTRA_KEYS = frozenset({'events', 'links'})    # a get_history chain row: ASSERTION_KEYS + these
BRANCH_KEYS = frozenset({'id', 'children'})                # a get_history branch: a row superseded by two or more
STATUS_AS_OF = ('open', 'withdrawn', 'superseded')

# Tool results (``notice`` comes on top and goes to the envelope).
FIND_RESULT_KEYS = frozenset({'entities', 'total', 'limit', 'has_more'})
ENTITY_RESULT_KEYS = frozenset({'entity', 'facts', 'related', 'decisions', 'tasks', 'predicates_in_use',
                                'has_more', 'as_of'})
ENTITY_LISTS = ('facts', 'related', 'decisions', 'tasks')   # the keys of ``has_more``
AS_OF_KEYS = frozenset({'valid', 'recorded'})
LIST_RESULT_KEYS = frozenset({'items', 'total', 'limit', 'offset', 'has_more', 'as_of'})
HISTORY_RESULT_KEYS = frozenset({'chain', 'truncated', 'branches'})
HISTORY_MAX = 50
HISTORY_GRAPH_MAX = 1_000      # rows walked along supersedes_id before the history stops looking further

# ``list_assertions`` filters, checked and normalised by ``tools.list_knowledge``: kind
# fact|decision|todo|all, entity_id int|None, predicate str|None, status open|conflict|closed|all,
# state open|done|cancelled|None, due_before date|None, source_kind evidence|my_note|all, as_of_valid
# date|None, as_of_recorded aware ISO|None (now), recorded_since aware ISO|None.
FILTER_KEYS = ('kind', 'entity_id', 'predicate', 'status', 'state', 'due_before', 'source_kind', 'as_of_valid',
               'as_of_recorded', 'recorded_since')
LIST_KINDS = (*store.ASSERTION_KINDS, 'all')
LIST_STATUSES = ('open', 'conflict', 'closed', 'all')
SOURCE_FILTERS = ('evidence', 'my_note', 'all')


def visible_sql(valid_at: str | None, recorded_at: str | None, include_closed: bool) -> tuple[str, list]:
    """``(sql, params)``: a boolean expression on alias ``a`` (assertions), no leading ``AND``, for the two
    time axes (design M4 §5.1). ``recorded_at`` None means now; ``valid_at`` None filters no validity
    period; ``include_closed`` keeps rows closed before ``recorded_at`` (recorded by then).

    Recorded axis at R: ``julianday(recorded_from) <= julianday(R) AND (recorded_to IS NULL OR julianday(R)
    < julianday(recorded_to))`` (a row closed at R is no longer seen); now is the open rows exactly
    (``recorded_to IS NULL``). Valid axis at T: ``(valid_from IS NULL OR valid_from <= T) AND (valid_to IS
    NULL OR T < valid_to)`` (``[valid_from, valid_to)``, open ends unbounded). ``argument_invalid`` for a
    bad date or time."""
    parts, params = [], []
    recorded_at = recorded_instant(recorded_at)
    if recorded_at is None:
        if not include_closed:
            parts.append('a.recorded_to IS NULL')
    else:
        parts.append('julianday(a.recorded_from) <= julianday(?)')
        params.append(recorded_at)
        if not include_closed:
            parts.append('(a.recorded_to IS NULL OR julianday(?) < julianday(a.recorded_to))')
            params.append(recorded_at)
    if valid_at is not None:
        knowledge.check_date(valid_at, 'as_of_valid')
        parts += ['(a.valid_from IS NULL OR a.valid_from <= ?)', '(a.valid_to IS NULL OR ? < a.valid_to)']
        params += [valid_at, valid_at]
    return ' AND '.join(parts) or '1', params


def recorded_instant(value, name: str = 'as_of_recorded') -> str | None:
    """``value`` (an ISO 8601 date or time) as an aware ISO time to the second, the local time zone when it
    has none (``search._instant``'s rule); None stays None (now). ``KnowledgeError('argument_invalid')``."""
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        moment = datetime.fromisoformat(value.strip())
        if moment.tzinfo is None:
            moment = moment.astimezone()
        offset = moment.utcoffset()
        if offset.seconds % 60 or offset.microseconds:
            raise ValueError       # SQLite's julianday() cannot read an offset with seconds (review L4)
    except (ValueError, OverflowError, OSError):
        raise knowledge.KnowledgeError('argument_invalid', f'{name} must be an ISO 8601 date or time '
                                                           '(offset in whole minutes)') from None
    return moment.isoformat(timespec='seconds')


_ROW_SQL = ('SELECT id, kind, entity_id, predicate, object_entity_id, statement, data_json, valid_from, valid_to, '
            'recorded_from, recorded_to, status, source_kind, supersedes_id, recorded_by FROM assertions WHERE id = ?')
_EVIDENCE_SQL = ('SELECT e.id, e.locator_json, e.quote, v.superseded_at, d.status, d.id, d.path, e.version_id '
                 'FROM evidence e JOIN versions v ON v.id = e.version_id JOIN documents d ON d.id = v.document_id '
                 'WHERE e.assertion_id = ? ORDER BY e.id')


def _entity_ref(db, entity_id: int | None) -> dict | None:
    if entity_id is None:
        return None
    row = db.execute('SELECT kind, name FROM entities WHERE id = ?', (entity_id,)).fetchone()
    return None if row is None else {'kind': row[0], 'name': row[1]}


def source_state(document_status: str, superseded_at: str | None, matches: bool = True) -> str:
    """``SOURCE_STATES`` of one stored source (design M4 §3.4): the document's status first; ``changed`` for
    a superseded version, or when the stored unit no longer holds the quote at its locator (``matches``
    False: ``scan --reparse`` refilled the units of that same version, review M4 v1 M1)."""
    if document_status in ('deleted', 'skipped'):
        return document_status
    return 'changed' if superseded_at is not None or not matches else 'current'


def still_matches(db, version_id: int, obj: Mapping, quote: str) -> bool:
    """The stored unit of ``version_id`` named by ``obj`` still gives ``quote`` at ``obj['chars']``."""
    try:
        unit = db.execute('SELECT text FROM units WHERE version_id = ? AND locator_json = ?',
                          (version_id, parsers.unit_json(obj['unit']))).fetchone()
        return unit is not None and locator.check_text(dict(obj), unit[0]) == quote
    except (locator.LocatorError, KeyError, TypeError):
        return False


def worst_state(states) -> str | None:
    """The worst of ``states`` (``SOURCE_STATE_ORDER``), None when there is none (a note)."""
    present = set(states)
    return next((state for state in SOURCE_STATE_ORDER if state in present), None)


def requote(db, document_id: int, path: str, obj: dict, quote: str) -> dict:
    """``{'found': True, 'locator'}`` when the same unit of the document's latest version holds ``quote``
    exactly once (a locator for it there), else ``{'found': False}`` (design M4 §3.4)."""
    latest = db.execute('SELECT id, sha256 FROM versions WHERE document_id = ? AND superseded_at IS NULL '
                        'ORDER BY seq DESC LIMIT 1', (document_id,)).fetchone()
    if latest is None or not quote:
        return {'found': False}
    unit = db.execute('SELECT text FROM units WHERE version_id = ? AND locator_json = ?',
                      (latest[0], parsers.unit_json(obj['unit']))).fetchone()
    if unit is None:
        return {'found': False}
    text = unit[0]
    start = text.find(quote)
    if start < 0 or text.find(quote, start + 1) >= 0:
        return {'found': False}
    return {'found': True,
            'locator': locator.make(document_id, path, latest[1], obj['unit'], text, [start, start + len(quote)])}


def _evidence(db, assertion_id: int, *, with_requote: bool) -> list[dict]:
    items = []
    for evidence_id, locator_json, quote, superseded_at, doc_status, doc_id, path, version_id in db.execute(
            _EVIDENCE_SQL, (assertion_id,)).fetchall():
        obj = json.loads(locator_json)
        state = source_state(doc_status, superseded_at, still_matches(db, version_id, obj, quote))
        items.append({'id': evidence_id, 'locator': obj, 'quote': quote, 'quote_chars': len(quote),
                      'source_state': state,
                      'requote': requote(db, doc_id, path, obj, quote) if with_requote and state == 'changed' else None})
    return items


def _closed_at(db, recorded_to: str | None, recorded_at: str | None) -> bool:
    """Whether a row with ``recorded_to`` was closed at ``recorded_at`` (None: now)."""
    if recorded_to is None:
        return False
    if recorded_at is None:
        return True
    return bool(db.execute('SELECT julianday(?) >= julianday(?)', (recorded_at, recorded_to)).fetchone()[0])


def _conflict_ids(db, assertion_id: int, recorded_at: str | None) -> list[int]:
    """The other rows of the ``conflicts`` links of ``assertion_id`` open at ``recorded_at`` (None: now;
    design M4 §4.5): recorded by then and not resolved by then."""
    sql = ("SELECT from_id, to_id FROM links WHERE relation = 'conflicts' AND (from_id = ? OR to_id = ?) AND ")
    params: list = [assertion_id, assertion_id]
    if recorded_at is None:
        sql += 'resolved_at IS NULL'
    else:
        sql += ('julianday(recorded_at) <= julianday(?) AND '
                '(resolved_at IS NULL OR julianday(?) < julianday(resolved_at))')
        params += [recorded_at, recorded_at]
    others = []
    for from_id, to_id in db.execute(sql + ' ORDER BY id', params).fetchall():
        other = to_id if from_id == assertion_id else from_id
        if other != assertion_id and other not in others:
            others.append(other)
    return others


def _conflict(db, other_id: int) -> dict:
    row = db.execute('SELECT data_json, valid_from, valid_to, recorded_from FROM assertions WHERE id = ?',
                     (other_id,)).fetchone()
    return {'id': other_id, 'value': json.loads(row[0]).get('value'), 'valid_from': row[1], 'valid_to': row[2],
            'recorded_from': row[3]}


def _row(db, assertion_id: int, *, with_evidence: bool, with_requote: bool, recorded_at: str | None) -> dict:
    row = db.execute(_ROW_SQL, (assertion_id,)).fetchone()
    if row is None:
        raise knowledge.KnowledgeError('assertion_not_found', f'no assertion {assertion_id}')
    (row_id, kind, entity_id, predicate, object_id, statement, data_json, valid_from, valid_to, recorded_from,
     recorded_to, status, source_kind, supersedes_id, recorded_by) = row
    evidence = _evidence(db, row_id, with_requote=with_requote)
    closed = _closed_at(db, recorded_to, recorded_at)
    superseded_by = None
    if closed and status == 'superseded':      # the row its closing event names (review M4 v1 M2)
        found = db.execute("SELECT related_id FROM assertion_events WHERE assertion_id = ? AND kind = 'superseded' "
                           'ORDER BY id DESC LIMIT 1', (row_id,)).fetchone()
        superseded_by = found[0] if found else None
    later_sql = 'SELECT id FROM assertions WHERE supersedes_id = ?'
    later_params: list = [row_id]
    if recorded_at is not None:
        later_sql += ' AND julianday(recorded_from) <= julianday(?)'
        later_params.append(recorded_at)
    continued = [later for (later,) in db.execute(later_sql + ' ORDER BY id', later_params).fetchall()
                 if later != superseded_by]
    others = [] if closed else _conflict_ids(db, row_id, recorded_at)
    count = len(evidence)
    return {'id': row_id, 'kind': kind, 'entity_id': entity_id, 'entity': _entity_ref(db, entity_id),
            'predicate': predicate, 'object_entity_id': object_id, 'object': _entity_ref(db, object_id),
            'statement': statement, 'data': json.loads(data_json), 'valid_from': valid_from, 'valid_to': valid_to,
            'recorded_from': recorded_from, 'recorded_to': recorded_to, 'status': status,
            'status_as_of': status if closed else 'open', 'source_kind': source_kind,
            'source_label': knowledge.MY_NOTE_LABEL if source_kind == 'my_note' else knowledge.EVIDENCE_LABEL.format(count),
            'source_state': worst_state(item['source_state'] for item in evidence),
            'supersedes_id': supersedes_id,
            'superseded_by_id': superseded_by, 'continued_by_ids': continued,
            'recorded_by': recorded_by,
            'conflicts': [_conflict(db, other) for other in others[:CONFLICTS_MAX]],
            'conflicts_more': max(0, len(others) - CONFLICTS_MAX),
            'evidence': evidence if with_evidence else None, 'evidence_count': count}


def assertion_row(db, assertion_id: int, *, with_evidence: bool,
                  as_of_recorded: str | None = None) -> dict:
    """One assertion as ``ASSERTION_KEYS`` (``evidence`` a list of ``EVIDENCE_KEYS`` when ``with_evidence``,
    else None); ``status_as_of`` and ``conflicts`` at ``as_of_recorded`` (None: now). Raises
    ``KnowledgeError('assertion_not_found')``.

    ``status_as_of`` is ``open`` unless the row was closed by then (then its stored status); a closed row
    has no ``conflicts``. ``superseded_by_id`` is the row that closed it by superseding (None while open);
    ``continued_by_ids`` the other rows recorded by then that supersede it (a cut row, a later-period row).
    ``requote`` is None here (write responses); get_entity and get_history fill it."""
    return _row(db, assertion_id, with_evidence=with_evidence, with_requote=False,
                recorded_at=recorded_instant(as_of_recorded))


def entity_row(db, entity_id: int) -> dict:
    """One entity as ``ENTITY_KEYS`` (``aliases`` a list, ``counts`` ``ENTITY_COUNT_KEYS`` of open rows now).
    Raises ``KnowledgeError('entity_not_found')``.

    ``tasks_open``: open task rows whose state is ``open`` (``store.knowledge_counts``' rule);
    ``conflicts``: open rows about the entity marked ``conflict``."""
    row = db.execute('SELECT id, kind, name, aliases_json, note, created_at, created_by, merged_into FROM entities '
                     'WHERE id = ?', (entity_id,)).fetchone()
    if row is None:
        raise knowledge.KnowledgeError('entity_not_found', f'no entity {entity_id}')
    counts = db.execute(
        "SELECT COALESCE(SUM(kind = 'fact'), 0), COALESCE(SUM(kind = 'decision'), 0), "
        "COALESCE(SUM(kind = 'todo' AND json_extract(data_json, '$.state') = 'open'), 0), "
        "COALESCE(SUM(status = 'conflict'), 0) FROM assertions WHERE entity_id = ? AND recorded_to IS NULL",
        (entity_id,)).fetchone()
    return {'id': row[0], 'kind': row[1], 'name': row[2], 'aliases': json.loads(row[3]), 'note': row[4],
            'created_at': row[5], 'created_by': row[6], 'merged_into': row[7],
            'counts': dict(zip(('facts_open', 'decisions_open', 'tasks_open', 'conflicts'), counts))}


# ---- notices (lines the tools move into the envelope) ---------------------------------------------------
SOURCE_NOTICE = ('출처 {}건의 원문이 인용 뒤 바뀌었거나(changed) 삭제(deleted)·건너뜀(skipped) 상태입니다(source_state) '
                 '— changed는 requote 제안의 locator로 supersede하면 새 근거가 됩니다')
SOURCE_LIST_NOTICE = '출처가 바뀌었거나 사라진 항목 {}건(source_state) — get_history로 근거를 확인하세요'
CONFLICT_NOTICE = '충돌로 표시된 사실 {}건(같은 주어·속성, 겹치는 기간, 다른 값) — retract 또는 supersede로 해소하세요'
FIND_EMPTY_NOTICE = '이름·별칭이 맞는 엔티티가 없습니다 — 없는 것이 아니라 기록되지 않은 것일 수 있습니다(record_entity)'
LIST_EMPTY_NOTICE = '조건에 맞는 기록이 없습니다 — 기록되지 않았다는 뜻이지 그런 사실이 없다는 뜻은 아닙니다'
HISTORY_TRUNCATED_NOTICE = f'이력이 {HISTORY_MAX}행을 넘어 최신 {HISTORY_MAX}행만 보입니다(truncated)'
FIND_QUERY_MAX = 200


def _notices(rows, *, quotes: bool) -> list[str]:
    """Source and conflict lines for the assertions a tool returns (``quotes``: counted per evidence item,
    else per row)."""
    notice = []
    if quotes:
        stale = sum(item['source_state'] != 'current' for row in rows for item in row['evidence'] or ())
        if stale:
            notice.append(SOURCE_NOTICE.format(stale))
    else:
        stale = sum(row['source_state'] not in (None, 'current') for row in rows)
        if stale:
            notice.append(SOURCE_LIST_NOTICE.format(stale))
    if conflicted := sum(bool(row['conflicts']) for row in rows):
        notice.append(CONFLICT_NOTICE.format(conflicted))
    return notice


def _as_of(valid_at: str | None, recorded_at: str | None) -> dict:
    """``AS_OF_KEYS``: the validity date (None: every period) and the recorded time used (now when none)."""
    return {'valid': valid_at, 'recorded': recorded_at or store.now()}


def _source_filter(source_kind: str) -> tuple[str, list]:
    if source_kind not in SOURCE_FILTERS:
        raise knowledge.KnowledgeError('argument_invalid', f"source_kind must be one of {'|'.join(SOURCE_FILTERS)}")
    return ('1', []) if source_kind == 'all' else ('a.source_kind = ?', [source_kind])


def _check_kind(kind, *, allow_none: bool) -> None:
    if not (kind is None and allow_none) and kind not in store.ENTITY_KINDS:
        raise knowledge.KnowledgeError('argument_invalid', f"kind must be one of {'|'.join(store.ENTITY_KINDS)}")


def entity_by_name(db, kind: str, name: str) -> int:
    """The id of entity ``(kind, name)`` (exact; ``name`` NFC+strip). Raises ``KnowledgeError
    ('entity_not_found')``, saying to use find_entity when ``name`` is only an alias."""
    _check_kind(kind, allow_none=False)
    name = knowledge.norm_text(name, 'name', knowledge.NAME_MAX)
    row = db.execute('SELECT id FROM entities WHERE kind = ? AND name = ?', (kind, name)).fetchone()
    if row is not None:
        return row[0]
    folded = name.casefold()
    alias = any(folded in (item.casefold() for item in json.loads(aliases))
                for (aliases,) in db.execute('SELECT aliases_json FROM entities').fetchall())
    hint = ('the name is an alias of an entity: use find_entity' if alias
            else 'find_entity searches names and aliases')
    raise knowledge.KnowledgeError('entity_not_found', f'no {kind} entity with that exact name ({hint})')


def find_entities(db, query: str, kind: str | None, limit: int) -> dict:
    """``find_entity`` (design M4 §6.3): name or alias contains ``query`` (case-insensitive); exact name, then
    exact alias, then partial, then by name. Returns ``FIND_RESULT_KEYS`` + ``notice``; no document text.

    Case is folded in Python (``str.casefold``), so it holds for every script, not only ASCII as SQLite's
    ``lower`` would."""
    folded = knowledge.norm_text(query, 'query', FIND_QUERY_MAX).casefold()
    _check_kind(kind, allow_none=True)
    sql, params = 'SELECT id, name, aliases_json FROM entities', []
    if kind is not None:
        sql, params = sql + ' WHERE kind = ?', [kind]
    ranked = []
    for entity_id, name, aliases_json in db.execute(sql, params).fetchall():
        own, aliases = name.casefold(), [item.casefold() for item in json.loads(aliases_json)]
        if own == folded:
            rank = 0
        elif folded in aliases:
            rank = 1
        elif folded in own or any(folded in item for item in aliases):
            rank = 2
        else:
            continue
        ranked.append((rank, own, name, entity_id))
    ranked.sort()
    return {'entities': [entity_row(db, entity_id) for *_, entity_id in ranked[:limit]], 'total': len(ranked),
            'limit': limit, 'has_more': len(ranked) > limit, 'notice': [] if ranked else [FIND_EMPTY_NOTICE]}


# Orders inside get_entity's lists: facts by predicate then period; decisions newest first; tasks by due.
_FACT_ORDER = 'a.predicate, a.valid_from IS NOT NULL, a.valid_from, a.id'
_RECENT_ORDER = 'julianday(a.recorded_from) DESC, a.id DESC'
_TASK_ORDER = "json_extract(a.data_json, '$.due') IS NULL, json_extract(a.data_json, '$.due'), " + _RECENT_ORDER
_ENTITY_LISTS = {'facts': ("a.kind = 'fact' AND a.entity_id = ?", _FACT_ORDER),
                 'related': ("a.kind = 'fact' AND a.object_entity_id = ?", _FACT_ORDER),
                 'decisions': ("a.kind = 'decision' AND a.entity_id = ?", _RECENT_ORDER),
                 'tasks': ("a.kind = 'todo' AND a.entity_id = ?", _TASK_ORDER)}


def entity_detail(db, entity_id: int, *, as_of_valid: str | None, as_of_recorded: str | None,
                  include_closed: bool, source_kind: str, limit: int) -> dict:
    """``get_entity`` (design M4 §6.3): the entity, its facts, facts naming it as object (``related``),
    decisions and tasks about it (each at most ``limit``, ``has_more`` per list), ``predicates_in_use``,
    ``as_of``. Assertions carry evidence with quotes and ``requote``. Returns ``ENTITY_RESULT_KEYS`` +
    ``notice``. Raises ``KnowledgeError('entity_not_found')``.

    Every list and ``predicates_in_use`` use the same time axes and ``source_kind`` filter; facts come by
    predicate then validity start (none first), decisions newest recorded first, tasks by ``due`` (none
    last)."""
    entity = entity_row(db, entity_id)
    recorded_at = recorded_instant(as_of_recorded)
    visible, visible_params = visible_sql(as_of_valid, recorded_at, include_closed)
    source, source_params = _source_filter(source_kind)
    where, params = f'({visible}) AND {source}', [*visible_params, *source_params]
    result: dict = {'entity': entity}
    has_more = {}
    for key, (condition, order) in _ENTITY_LISTS.items():
        found = [row[0] for row in db.execute(
            f'SELECT a.id FROM assertions a WHERE {condition} AND {where} ORDER BY {order} LIMIT ?',
            (entity_id, *params, limit + 1)).fetchall()]
        has_more[key] = len(found) > limit
        result[key] = [_row(db, found_id, with_evidence=True, with_requote=True, recorded_at=recorded_at)
                       for found_id in found[:limit]]
    result['predicates_in_use'] = [row[0] for row in db.execute(
        f"SELECT DISTINCT a.predicate FROM assertions a WHERE a.kind = 'fact' AND a.entity_id = ? AND {where} "
        'ORDER BY a.predicate', (entity_id, *params)).fetchall()]
    result['has_more'] = has_more
    result['as_of'] = _as_of(as_of_valid, recorded_at)
    result['notice'] = _notices([row for key in ENTITY_LISTS for row in result[key]], quotes=True)
    return result


def _status_sql(status: str, recorded_at: str | None) -> tuple[str, list, bool]:
    """``(sql, params, include_closed)`` of a ``list_knowledge`` status at ``recorded_at`` (None: now):
    open = active or conflict then; conflict = an open conflict link then; closed = closed by then."""
    if status not in LIST_STATUSES:
        raise knowledge.KnowledgeError('argument_invalid', f"status must be one of {'|'.join(LIST_STATUSES)}")
    if status == 'open':
        return '1', [], False
    if status == 'all':
        return '1', [], True
    if status == 'conflict':
        if recorded_at is None:
            return "a.status = 'conflict'", [], False
        return ("EXISTS(SELECT 1 FROM links l WHERE l.relation = 'conflicts' AND (l.from_id = a.id OR l.to_id = a.id) "
                'AND julianday(l.recorded_at) <= julianday(?) '
                'AND (l.resolved_at IS NULL OR julianday(?) < julianday(l.resolved_at)))',
                [recorded_at, recorded_at], False)
    if recorded_at is None:
        return 'a.recorded_to IS NOT NULL', [], True
    return 'a.recorded_to IS NOT NULL AND julianday(a.recorded_to) <= julianday(?)', [recorded_at], True


def list_assertions(db, filters: Mapping, limit: int, offset: int) -> dict:
    """``list_knowledge`` (design M4 §6.3): assertions matching ``filters`` (``FILTER_KEYS``), newest recorded
    first (tasks by ``due``, none last), ``evidence`` None and ``evidence_count``, ``source_state`` the worst.
    Returns ``LIST_RESULT_KEYS`` + ``notice``; no document text.

    A missing filter takes the tool default (kind all, status open, source_kind all, none for the rest).
    ``state`` and ``due_before`` (``due < due_before``) select tasks only; ``recorded_since`` keeps rows
    recorded at or after it. An unknown filter key is ``argument_invalid``."""
    if unknown := set(filters) - set(FILTER_KEYS):
        raise knowledge.KnowledgeError('argument_invalid', f'unknown filter {sorted(unknown)[0]}')
    kind = filters.get('kind') or 'all'
    if kind not in LIST_KINDS:
        raise knowledge.KnowledgeError('argument_invalid', f"kind must be one of {'|'.join(LIST_KINDS)}")
    recorded_at = recorded_instant(filters.get('as_of_recorded'))
    as_of_valid = filters.get('as_of_valid')
    status_sql, status_params, include_closed = _status_sql(filters.get('status') or 'open', recorded_at)
    visible, visible_params = visible_sql(as_of_valid, recorded_at, include_closed)
    source, source_params = _source_filter(filters.get('source_kind') or 'all')
    clauses, params = [f'({visible})', status_sql, source], [*visible_params, *status_params, *source_params]
    if kind != 'all':
        clauses.append('a.kind = ?')
        params.append(kind)
    if filters.get('entity_id') is not None:
        clauses.append('a.entity_id = ?')
        params.append(filters['entity_id'])
    if filters.get('predicate') is not None:
        clauses.append('a.predicate = ?')
        params.append(knowledge.norm_text(filters['predicate'], 'predicate', knowledge.PREDICATE_MAX))
    if filters.get('state') is not None:
        if filters['state'] not in store.TASK_STATES:
            raise knowledge.KnowledgeError('argument_invalid', f"state must be one of {'|'.join(store.TASK_STATES)}")
        clauses.append("a.kind = 'todo' AND json_extract(a.data_json, '$.state') = ?")
        params.append(filters['state'])
    if filters.get('due_before') is not None:
        clauses.append("a.kind = 'todo' AND json_extract(a.data_json, '$.due') < ?")
        params.append(knowledge.check_date(filters['due_before'], 'due_before'))
    if filters.get('recorded_since') is not None:
        clauses.append('julianday(a.recorded_from) >= julianday(?)')
        params.append(recorded_instant(filters['recorded_since'], 'recorded_since'))
    where = ' AND '.join(clauses)
    total = db.execute(f'SELECT COUNT(*) FROM assertions a WHERE {where}', params).fetchone()[0]
    order = _TASK_ORDER if kind == 'todo' else _RECENT_ORDER
    found = [row[0] for row in db.execute(f'SELECT a.id FROM assertions a WHERE {where} ORDER BY {order} '
                                          'LIMIT ? OFFSET ?', (*params, limit, offset)).fetchall()]
    items = [_row(db, found_id, with_evidence=False, with_requote=False, recorded_at=recorded_at) for found_id in found]
    notice = _notices(items, quotes=False) if items else ([] if total else [LIST_EMPTY_NOTICE])
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset, 'has_more': offset + len(items) < total,
            'as_of': _as_of(as_of_valid, recorded_at), 'notice': notice}


_EVENT_COLUMNS = ('id', 'at', 'kind', 'reason', 'related_id', 'recorded_by')
_LINK_COLUMNS = ('id', 'from_id', 'to_id', 'relation', 'recorded_at', 'recorded_by', 'resolved_at', 'resolved_by_id')


def _events(db, assertion_id: int) -> list[dict]:
    return [dict(zip(_EVENT_COLUMNS, row)) for row in db.execute(
        f"SELECT {', '.join(_EVENT_COLUMNS)} FROM assertion_events WHERE assertion_id = ? ORDER BY id",
        (assertion_id,)).fetchall()]


def _links(db, assertion_id: int) -> list[dict]:
    return [dict(zip(_LINK_COLUMNS, row)) for row in db.execute(
        f"SELECT {', '.join(_LINK_COLUMNS)} FROM links WHERE from_id = ? OR to_id = ? ORDER BY id",
        (assertion_id, assertion_id)).fetchall()]


def chain_ids(db, assertion_id: int) -> tuple[list[int], bool]:
    """``(ids, complete)``: every row joined to ``assertion_id`` by ``supersedes_id`` in either direction, in
    recorded order (``recorded_from``, then id). Supersede links form a graph, not a line: an open cut row C
    can be superseded again while its later-period row N also names it (review M4 v1 M2). ``complete`` is
    False when the walk stopped at ``HISTORY_GRAPH_MAX`` rows."""
    seen, queue = {assertion_id}, [assertion_id]
    while queue:
        current = queue.pop()
        parent = db.execute('SELECT supersedes_id FROM assertions WHERE id = ?', (current,)).fetchone()[0]
        children = [row[0] for row in db.execute('SELECT id FROM assertions WHERE supersedes_id = ?', (current,))]
        for other in ([parent] if parent is not None else []) + children:
            if other not in seen:
                if len(seen) >= HISTORY_GRAPH_MAX:
                    queue = []
                    break
                seen.add(other)
                queue.append(other)
    marks = ','.join('?' * len(seen))
    ordered = [row[0] for row in db.execute(
        f'SELECT id FROM assertions WHERE id IN ({marks}) ORDER BY julianday(recorded_from), id', sorted(seen))]
    return ordered, len(seen) < HISTORY_GRAPH_MAX


def _branches(db, ids: list[int]) -> list[dict]:
    """``BRANCH_KEYS`` for every row among ``ids`` that two or more of them name as ``supersedes_id``."""
    marks = ','.join('?' * len(ids))
    children: dict[int, list[int]] = {}
    for row_id, parent in db.execute(f'SELECT id, supersedes_id FROM assertions WHERE id IN ({marks}) '
                                     'AND supersedes_id IS NOT NULL ORDER BY id', ids):
        children.setdefault(parent, []).append(row_id)
    return [{'id': parent, 'children': kids} for parent, kids in sorted(children.items()) if len(kids) > 1]


HISTORY_BRANCH_NOTICE = ('대체 이력이 갈라집니다(행 {}) — 중간 행(old_valid_to)을 다시 대체하면 생깁니다; '
                         'branches와 각 행의 superseded_by_id·continued_by_ids를 보세요')


def history(db, assertion_id: int) -> dict:
    """``get_history`` (design M4 §5.2, review M4 v1 M2): every row joined to ``assertion_id`` by supersede
    links, in recorded order (at most ``HISTORY_MAX``, latest kept, ``truncated``), each row ``ASSERTION_KEYS``
    + ``events`` (``EVENT_KEYS``) + ``links`` (``LINK_KEYS``), evidence with quotes and ``requote``;
    ``branches`` (``BRANCH_KEYS``) names the rows superseded by two or more rows. Returns
    ``HISTORY_RESULT_KEYS`` + ``notice``. Raises ``KnowledgeError('assertion_not_found')``.

    No time axis: every row with its stored status (``status_as_of`` now). ``links`` holds the row's
    supersede and conflict links in either direction, resolved ones too."""
    if db.execute('SELECT 1 FROM assertions WHERE id = ?', (assertion_id,)).fetchone() is None:
        raise knowledge.KnowledgeError('assertion_not_found', f'no assertion {assertion_id}')
    chain, complete = chain_ids(db, assertion_id)
    truncated = len(chain) > HISTORY_MAX or not complete
    kept = chain[-HISTORY_MAX:]
    rows = [{**_row(db, row_id, with_evidence=True, with_requote=True, recorded_at=None),
             'events': _events(db, row_id), 'links': _links(db, row_id)} for row_id in kept]
    branches = _branches(db, kept)
    notice = _notices(rows, quotes=True)
    if branches:
        notice.insert(0, HISTORY_BRANCH_NOTICE.format(', '.join(str(b['id']) for b in branches)))
    if truncated:
        notice.insert(0, HISTORY_TRUNCATED_NOTICE)
    return {'chain': rows, 'truncated': truncated, 'branches': branches, 'notice': notice}

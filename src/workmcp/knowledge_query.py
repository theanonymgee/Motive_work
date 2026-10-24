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
status first: ``deleted``, ``skipped``; then ``changed`` for a superseded version; else ``current``).
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
ASSERTION_KEYS = frozenset({'id', 'kind', 'entity_id', 'entity', 'predicate', 'object_entity_id', 'object',
                            'statement', 'data', 'valid_from', 'valid_to', 'recorded_from', 'recorded_to', 'status',
                            'status_as_of', 'source_kind', 'source_label', 'source_state', 'supersedes_id',
                            'superseded_by_id', 'recorded_by', 'conflicts', 'evidence', 'evidence_count'})
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
STATUS_AS_OF = ('open', 'withdrawn', 'superseded')

# Tool results (``notice`` comes on top and goes to the envelope).
FIND_RESULT_KEYS = frozenset({'entities', 'total', 'limit', 'has_more'})
ENTITY_RESULT_KEYS = frozenset({'entity', 'facts', 'related', 'decisions', 'tasks', 'predicates_in_use',
                                'has_more', 'as_of'})
ENTITY_LISTS = ('facts', 'related', 'decisions', 'tasks')   # the keys of ``has_more``
AS_OF_KEYS = frozenset({'valid', 'recorded'})
LIST_RESULT_KEYS = frozenset({'items', 'total', 'limit', 'offset', 'has_more', 'as_of'})
HISTORY_RESULT_KEYS = frozenset({'chain', 'truncated'})
HISTORY_MAX = 50

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
    period; ``include_closed`` keeps rows closed before ``recorded_at`` (recorded by then)."""
    raise NotImplementedError('knowledge_query.visible_sql: M4 stage 1')


def recorded_instant(value) -> str | None:
    """``value`` (an ISO 8601 date or time) as an aware ISO time to the second, the local time zone when it
    has none (``search._instant``'s rule); None stays None (now). ``KnowledgeError('argument_invalid')``."""
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        raise knowledge.KnowledgeError('argument_invalid', 'as_of_recorded must be an ISO 8601 date or time') from None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.isoformat(timespec='seconds')


_ROW_SQL = ('SELECT id, kind, entity_id, predicate, object_entity_id, statement, data_json, valid_from, valid_to, '
            'recorded_from, recorded_to, status, source_kind, supersedes_id, recorded_by FROM assertions WHERE id = ?')
_EVIDENCE_SQL = ('SELECT e.id, e.locator_json, e.quote, v.superseded_at, d.status, d.id, d.path '
                 'FROM evidence e JOIN versions v ON v.id = e.version_id JOIN documents d ON d.id = v.document_id '
                 'WHERE e.assertion_id = ? ORDER BY e.id')


def _entity_ref(db, entity_id: int | None) -> dict | None:
    if entity_id is None:
        return None
    row = db.execute('SELECT kind, name FROM entities WHERE id = ?', (entity_id,)).fetchone()
    return None if row is None else {'kind': row[0], 'name': row[1]}


def source_state(document_status: str, superseded_at: str | None) -> str:
    """``SOURCE_STATES`` of one stored source (design M4 §3.4): the document's status first."""
    if document_status in ('deleted', 'skipped'):
        return document_status
    return 'changed' if superseded_at is not None else 'current'


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
    for evidence_id, locator_json, quote, superseded_at, doc_status, doc_id, path in db.execute(
            _EVIDENCE_SQL, (assertion_id,)).fetchall():
        obj = json.loads(locator_json)
        state = source_state(doc_status, superseded_at)
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
    later_sql = 'SELECT MIN(id) FROM assertions WHERE supersedes_id = ?'
    later_params: list = [row_id]
    if recorded_at is not None:
        later_sql += ' AND julianday(recorded_from) <= julianday(?)'
        later_params.append(recorded_at)
    closed = _closed_at(db, recorded_to, recorded_at)
    count = len(evidence)
    return {'id': row_id, 'kind': kind, 'entity_id': entity_id, 'entity': _entity_ref(db, entity_id),
            'predicate': predicate, 'object_entity_id': object_id, 'object': _entity_ref(db, object_id),
            'statement': statement, 'data': json.loads(data_json), 'valid_from': valid_from, 'valid_to': valid_to,
            'recorded_from': recorded_from, 'recorded_to': recorded_to, 'status': status,
            'status_as_of': status if closed else 'open', 'source_kind': source_kind,
            'source_label': knowledge.MY_NOTE_LABEL if source_kind == 'my_note' else knowledge.EVIDENCE_LABEL.format(count),
            'source_state': worst_state(item['source_state'] for item in evidence),
            'supersedes_id': supersedes_id,
            'superseded_by_id': db.execute(later_sql, later_params).fetchone()[0],
            'recorded_by': recorded_by,
            'conflicts': [] if closed else [_conflict(db, other) for other in _conflict_ids(db, row_id, recorded_at)],
            'evidence': evidence if with_evidence else None, 'evidence_count': count}


def assertion_row(db, assertion_id: int, *, with_evidence: bool,
                  as_of_recorded: str | None = None) -> dict:
    """One assertion as ``ASSERTION_KEYS`` (``evidence`` a list of ``EVIDENCE_KEYS`` when ``with_evidence``,
    else None); ``status_as_of`` and ``conflicts`` at ``as_of_recorded`` (None: now). Raises
    ``KnowledgeError('assertion_not_found')``.

    ``status_as_of`` is ``open`` unless the row was closed by then (then its stored status); a closed row
    has no ``conflicts``. ``superseded_by_id`` is the row that superseded it, if recorded by then.
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


def entity_by_name(db, kind: str, name: str) -> int:
    """The id of entity ``(kind, name)`` (exact; ``name`` NFC+strip). Raises ``KnowledgeError
    ('entity_not_found')``, saying to use find_entity when ``name`` is only an alias."""
    raise NotImplementedError('knowledge_query.entity_by_name: M4 stage 1')


def find_entities(db, query: str, kind: str | None, limit: int) -> dict:
    """``find_entity`` (design M4 §6.3): name or alias contains ``query`` (case-insensitive); exact name, then
    exact alias, then partial, then by name. Returns ``FIND_RESULT_KEYS`` + ``notice``; no document text."""
    raise NotImplementedError('knowledge_query.find_entities: M4 stage 1')


def entity_detail(db, entity_id: int, *, as_of_valid: str | None, as_of_recorded: str | None,
                  include_closed: bool, source_kind: str, limit: int) -> dict:
    """``get_entity`` (design M4 §6.3): the entity, its facts, facts naming it as object (``related``),
    decisions and tasks about it (each at most ``limit``, ``has_more`` per list), ``predicates_in_use``,
    ``as_of``. Assertions carry evidence with quotes and ``requote``. Returns ``ENTITY_RESULT_KEYS`` +
    ``notice``. Raises ``KnowledgeError('entity_not_found')``."""
    raise NotImplementedError('knowledge_query.entity_detail: M4 stage 1')


def list_assertions(db, filters: Mapping, limit: int, offset: int) -> dict:
    """``list_knowledge`` (design M4 §6.3): assertions matching ``filters`` (``FILTER_KEYS``), newest recorded
    first (tasks by ``due``, none last), ``evidence`` None and ``evidence_count``, ``source_state`` the worst.
    Returns ``LIST_RESULT_KEYS`` + ``notice``; no document text."""
    raise NotImplementedError('knowledge_query.list_assertions: M4 stage 1')


def history(db, assertion_id: int) -> dict:
    """``get_history`` (design M4 §5.2): the supersede chain root to latest (at most ``HISTORY_MAX``, latest
    kept, ``truncated``), each row ``ASSERTION_KEYS`` + ``events`` (``EVENT_KEYS``) + ``links``
    (``LINK_KEYS``), evidence with quotes and ``requote``. Returns ``HISTORY_RESULT_KEYS`` + ``notice``.
    Raises ``KnowledgeError('assertion_not_found')``."""
    raise NotImplementedError('knowledge_query.history: M4 stage 1')

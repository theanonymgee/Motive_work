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

Stage 0 (design M4 §13.1): signatures and result key sets; the bodies raise ``NotImplementedError``.
"""
from __future__ import annotations

from collections.abc import Mapping

from . import knowledge, store  # noqa: F401 - knowledge: KnowledgeError, check_date (stage 1)

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


def assertion_row(db, assertion_id: int, *, with_evidence: bool,
                  as_of_recorded: str | None = None) -> dict:
    """One assertion as ``ASSERTION_KEYS`` (``evidence`` a list of ``EVIDENCE_KEYS`` when ``with_evidence``,
    else None); ``status_as_of`` and ``conflicts`` at ``as_of_recorded`` (None: now). Raises
    ``KnowledgeError('assertion_not_found')``."""
    raise NotImplementedError('knowledge_query.assertion_row: M4 stage 1')


def entity_row(db, entity_id: int) -> dict:
    """One entity as ``ENTITY_KEYS`` (``aliases`` a list, ``counts`` ``ENTITY_COUNT_KEYS`` of open rows now).
    Raises ``KnowledgeError('entity_not_found')``."""
    raise NotImplementedError('knowledge_query.entity_row: M4 stage 1')


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

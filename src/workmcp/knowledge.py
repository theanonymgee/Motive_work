"""Knowledge write path (design M4 §2-§4, §8): input normalisation, source binding by quote, recording
entities, facts, decisions and tasks, supersede, retract, conflict detection and assertion events.

Every write is one ``store.begin_immediate(db) ... store.commit(db)`` on the knowledge write connection
(``server`` keeps it, busy timeout 5 s); a lock that does not come free (``store.Busy``) becomes
``KnowledgeError('store_busy', retryable=True)`` with nothing written, and any failure inside rolls back.
Like the collector, this module does not import ``sqlite3`` (``tests/test_readonly.py``). Binding reads the stored unit text only
(``units.text``): this module never opens a file. Rows are only inserted, and the only updates are the
two the v1/v3 triggers allow (closing a row; marking or clearing a conflict) plus a new alias or note on
an entity. Error messages carry argument names, numbers and unit labels, never a statement, value,
quote, name or reason.

Every public writer returns the tool's ``result`` dict plus ``'notice'`` (a list of lines) which
``tools`` moves into the envelope. Assertion and entity summaries come from ``knowledge_query``
(one representation for writes and reads). Import this module and ``knowledge_query`` in module form
only (``from . import knowledge``): the two import each other.

Stage 0 (design M4 §13.1): signatures, the error class, constants and the two normalisation helpers;
the writers raise ``NotImplementedError``.
"""
from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from . import knowledge_query, store  # noqa: F401 - knowledge_query: summaries of written rows (stage 1)

ENTITY_KINDS = store.ENTITY_KINDS
ASSERTION_KINDS = store.ASSERTION_KINDS
TASK_STATES = store.TASK_STATES
SOURCE_KINDS = ('evidence', 'my_note')

# Content arguments per kind (design M4 §6.2); ``supersede`` accepts the subset of the old row's kind.
FIELDS = {
    'fact': ('entity_id', 'predicate', 'value', 'unit', 'object_entity_id', 'statement', 'valid_from', 'valid_to'),
    'decision': ('statement', 'entity_id', 'options', 'chosen', 'rationale', 'decided_on', 'valid_to'),
    'todo': ('statement', 'entity_id', 'due', 'owner_entity_id', 'state', 'done_at'),
}
# data_json keys per kind (design M4 §2.2); the server builds data_json, a client never sends it.
DATA_KEYS = {'fact': ('value', 'unit'), 'decision': ('options', 'chosen', 'rationale'),
             'todo': ('state', 'due', 'owner_entity_id', 'done_at')}

# Limits after NFC and strip (design M4 §2.2, §3.1), in characters.
STATEMENT_MAX = 1_000
VALUE_MAX = 1_000
UNIT_MAX = 50
PREDICATE_MAX = 100
OPTIONS_MAX = 20
OPTION_MAX = 200
CHOSEN_MAX = 1_000
RATIONALE_MAX = 1_000
REASON_MAX = 500
NAME_MAX = 200
ALIASES_MAX = 20
ALIAS_MAX = 100
NOTE_MAX = 1_000
SOURCES_MAX = 5
QUOTE_MIN = 2
QUOTE_MAX = 1_000
DATE_LENGTH = 10                  # YYYY-MM-DD

MY_NOTE_LABEL = '내 메모'
EVIDENCE_LABEL = '근거 {}건'

# Error codes this module raises (design M4 §6.1); all are in tools.ERROR_CODES, only store_busy retries.
ERROR_CODES = ('argument_invalid', 'evidence_required', 'evidence_quote_not_found', 'evidence_quote_ambiguous',
               'chunk_not_found', 'locator_invalid', 'not_found', 'entity_not_found', 'assertion_not_found',
               'already_closed', 'kind_mismatch', 'store_busy')

# Control characters other than tab, newline and carriage return are refused in every text argument.
_CONTROL = {chr(c) for c in (*range(0x00, 0x20), *range(0x7f, 0xa0))} - {'\t', '\n', '\r'}


class KnowledgeError(ValueError):
    """A refused knowledge call: ``code`` is one of ``ERROR_CODES``; ``message`` names arguments, numbers
    and unit labels only; ``retryable`` is True for ``store_busy`` only. ``server.handle`` turns it into
    ``tools.error(code, message, retryable=retryable)``."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class Bound:
    """One source bound to the stored text (design M4 §3.2): what an ``evidence`` row stores, plus what the
    response notices need. ``locator`` is made by the server (``locator.make``: its ``chars`` and
    ``text_sha256`` are re-derived; the client's are never stored); ``quote`` is ``unit_text[start:end]``."""
    version_id: int
    chunk_id: int | None        # hint only (no foreign key)
    locator: dict
    quote: str
    quote_sha256: str
    current: bool               # the cited version is the file's latest and the document is active
    flexible: bool              # matched ignoring whitespace: the stored quote may differ from the one sent


def norm_text(value, name: str, max_chars: int, *, min_chars: int = 1) -> str:
    """``value`` NFC-normalised and stripped, ``min_chars``-``max_chars`` characters long, without control
    characters (tab and newlines allowed); raises ``KnowledgeError('argument_invalid')`` naming ``name``."""
    if not isinstance(value, str):
        raise KnowledgeError('argument_invalid', f'{name} must be a string')
    text = unicodedata.normalize('NFC', value).strip()
    if not min_chars <= len(text) <= max_chars:
        raise KnowledgeError('argument_invalid', f'{name} must be {min_chars}-{max_chars} characters')
    if any(ch in _CONTROL for ch in text):
        raise KnowledgeError('argument_invalid', f'{name} has control characters')
    return text


def check_date(value, name: str) -> str:
    """``value`` when it is a date ``YYYY-MM-DD`` (``date.fromisoformat``, 10 characters); raises
    ``KnowledgeError('argument_invalid')`` naming ``name``."""
    if not isinstance(value, str) or len(value) != DATE_LENGTH:
        raise KnowledgeError('argument_invalid', f'{name} must be a date YYYY-MM-DD')
    try:
        date.fromisoformat(value)
    except ValueError:
        raise KnowledgeError('argument_invalid', f'{name} must be a date YYYY-MM-DD') from None
    return value


def bind_sources(db, sources: Sequence[Mapping]) -> list[Bound]:
    """Bind each ``{chunk_id | locator, quote}`` to the stored unit text (design M4 §3.2): find the unit(s),
    NFC+strip the quote, exact match, else whitespace-flexible match, else the un-normalised quote; exactly
    one place is taken (or the one the client's ``locator.chars`` names). Same unit and range twice is
    kept once. Runs inside the caller's transaction. Raises ``KnowledgeError`` (``chunk_not_found`` with
    ``tools.CHUNK_GONE_HINT``'s advice, ``locator_invalid``, ``not_found``, ``evidence_quote_not_found``,
    ``evidence_quote_ambiguous``, ``argument_invalid``)."""
    raise NotImplementedError('knowledge.bind_sources: M4 stage 1')


def record_entity(db, *, kind: str, name: str, aliases: Sequence[str] | None = None,
                  note: str | None = None, recorded_by: str) -> dict:
    """``record_entity`` (design M4 §6.2): create ``(kind, name)`` or, when it exists, add the new aliases
    (and the note when it has none). Returns ``{'entity', 'created', 'added_aliases', 'alias_of', 'notice'}``;
    ``alias_of`` lists other entities that have ``name`` as an alias (never merged)."""
    raise NotImplementedError('knowledge.record_entity: M4 stage 1')


def record(db, kind: str, *, recorded_by: str, call_id: str | None,
           sources: Sequence[Mapping] | None = None, my_note: bool = False, **fields) -> dict:
    """``record_fact``/``record_decision``/``record_task`` (design M4 §4.1, §6.2, appendix A): ``kind`` in
    ``ASSERTION_KINDS``, ``fields`` from ``FIELDS[kind]``; exactly one of ``sources`` (1-``SOURCES_MAX``) or
    ``my_note`` (``evidence_required``). One transaction: assertion, evidence, conflict detection (facts),
    links, events. Returns ``{'assertion', 'conflicts', 'duplicates', 'notice'}`` (``assertion`` with
    evidence; ``conflicts``/``duplicates`` empty for decisions and tasks)."""
    raise NotImplementedError('knowledge.record: M4 stage 1')


def supersede(db, assertion_id: int, *, reason: str, recorded_by: str, call_id: str | None,
              sources: Sequence[Mapping] | None = None, my_note: bool = False, old_valid_to: str | None = None,
              **fields) -> dict:
    """``supersede`` (design M4 §4.3): a new row with ``fields`` changed (the rest copied from the old row,
    ``kind_mismatch`` for a field of another kind), the old row closed ``superseded``; with
    ``old_valid_to`` a cut row (old content ending at that date, evidence copied) in between. Sources are
    always needed again. Returns ``{'old', 'cut', 'new', 'conflicts', 'duplicates', 'cleared_conflicts',
    'notice'}``. ``assertion_not_found``, ``already_closed``."""
    raise NotImplementedError('knowledge.supersede: M4 stage 1')


def retract(db, assertion_id: int, *, reason: str, recorded_by: str,
            call_id: str | None) -> dict:
    """``retract`` (design M4 §4.2): close the row ``withdrawn`` with ``reason``, resolve its conflicts.
    Returns ``{'assertion', 'cleared_conflicts', 'notice'}``. ``assertion_not_found``, ``already_closed``."""
    raise NotImplementedError('knowledge.retract: M4 stage 1')

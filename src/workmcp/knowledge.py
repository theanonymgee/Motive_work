"""Knowledge write path (design M4 §2-§4, §8): input normalisation, source binding by quote, recording
entities, facts, decisions and tasks, supersede, retract, conflict detection and assertion events.

Every write is one ``store.begin_immediate(db) ... store.commit(db)`` on the knowledge write connection
(``server`` keeps it, busy timeout 5 s); a lock that does not come free (``store.Busy``) becomes
``KnowledgeError('store_busy', retryable=True)`` with nothing written, and any failure inside rolls back.
Like the collector, this module does not import ``sqlite3`` (``tests/test_readonly.py``). Binding reads
the stored unit text only (``units.text``): this module never opens a file. Rows are only inserted, and
the only updates are the ones the v1/v3 triggers allow: closing a row (``withdrawn``/``superseded`` with
``recorded_to`` and ``change_reason``), marking or clearing a conflict (``status``), resolving a conflict
link once, and new aliases or a first note on an entity. Error messages carry argument names, numbers,
ids and unit labels, never a statement, value, quote, name or reason.

Every public writer returns the tool's ``result`` dict plus ``'notice'`` (a list of lines) which
``tools`` moves into the envelope. Assertion and entity summaries come from ``knowledge_query`` after the
commit (one representation for writes and reads). Import this module and ``knowledge_query`` in module
form only (``from . import knowledge``): the two import each other.

Supersede with ``old_valid_to`` (design M4 §4.3, coordinator decision (c)): the cut row C (old content,
``valid_to = old_valid_to``, evidence copied and re-checked against the stored unit) stays **open**, and
the new row N supersedes C (``N.supersedes_id = C``, ``C.supersedes_id = old``); the old row is closed. So
current knowledge answers C before that date and N from it (design §5.1).
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from . import knowledge_query, locator, parsers, store

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
DATE_MIN, DATE_MAX = '0000-00-00', '9999-12-31'   # open ends of a validity period in comparisons

MY_NOTE_LABEL = '내 메모'
EVIDENCE_LABEL = '근거 {}건'

# Error codes this module raises (design M4 §6.1); all are in tools.ERROR_CODES, only store_busy retries.
ERROR_CODES = ('argument_invalid', 'evidence_required', 'evidence_quote_not_found', 'evidence_quote_ambiguous',
               'chunk_not_found', 'locator_invalid', 'not_found', 'entity_not_found', 'assertion_not_found',
               'already_closed', 'kind_mismatch', 'store_busy')

BUSY_MESSAGE = 'scan 또는 index가 저장소를 쓰는 중입니다; 잠시 뒤 다시 부르세요(아무것도 기록하지 않음)'
CHUNK_GONE_HINT = 'scan/index가 청크를 바꿨을 수 있습니다 — 다시 검색하거나 hit의 locator로 주세요'
QUOTE_HINT = 'search snippet이나 get_snippet text를 그대로, 단위(줄·문단·셀) 하나 안에서 인용하세요'

OLD_SOURCE_NOTICE = '출처 {}건이 옛 버전 또는 삭제 문서의 것입니다(evidence[].source_state 참고)'
FLEXIBLE_NOTICE = '인용 {}건은 원문의 공백에 맞춰 저장됐습니다(evidence[].quote가 저장된 원문)'
DUPLICATE_NOTICE = '같은 사실이 이미 있습니다(id {}) — 출처를 더하려는 것이면 괜찮습니다'
CONFLICT_NOTICE = ('이 사실은 id {}와 충돌로 표시됐습니다(같은 주어·속성, 겹치는 기간, 다른 값) — '
                   'retract 또는 supersede로 해소하세요')
ALIAS_OF_NOTICE = '이 이름은 다른 엔티티(id {})의 별칭과 같습니다 — 같은 대상이면 그 엔티티를 쓰세요(자동 병합 안 함)'

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


def _invalid(message: str) -> KnowledgeError:
    return KnowledgeError('argument_invalid', message)


def _optional_text(value, name: str, max_chars: int) -> str | None:
    return None if value is None else norm_text(value, name, max_chars)


def _optional_id(value, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise _invalid(f'{name} must be a positive integer')
    return value


def _optional_date(value, name: str) -> str | None:
    return None if value is None else check_date(value, name)


def _dumps(value) -> str:
    """Deterministic JSON for ``data_json`` and ``aliases_json`` (design M4 §2.2)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# ---- transactions and small row helpers ------------------------------------------------------------

def _write(db, work: Callable[[str], object]):
    """Run ``work(now)`` in one ``BEGIN IMMEDIATE ... COMMIT``; anything raised rolls back (nothing written).
    ``now`` is the one time of this event: every row it writes carries it (design M4 §4.1)."""
    try:
        store.begin_immediate(db)
    except store.Busy:
        raise KnowledgeError('store_busy', BUSY_MESSAGE, retryable=True) from None
    try:
        out = work(store.now())
        store.commit(db)
    except store.Busy:
        raise KnowledgeError('store_busy', BUSY_MESSAGE, retryable=True) from None
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    return out


def _require_entity(db, entity_id: int | None, name: str) -> None:
    if entity_id is not None and db.execute('SELECT 1 FROM entities WHERE id=?', (entity_id,)).fetchone() is None:
        raise KnowledgeError('entity_not_found', f'{name} {entity_id} not found')


def _entity_name(db, entity_id: int) -> str:
    return db.execute('SELECT name FROM entities WHERE id=?', (entity_id,)).fetchone()[0]


def _event(db, assertion_id: int, at: str, kind: str, *, reason: str | None = None, related_id: int | None = None,
           recorded_by: str, call_id: str | None) -> None:
    db.execute('INSERT INTO assertion_events(assertion_id, at, kind, reason, related_id, recorded_by, call_id) '
               'VALUES(?,?,?,?,?,?,?)', (assertion_id, at, kind, reason, related_id, recorded_by, call_id))


def _link(db, source: int, target: int, relation: str, at: str, recorded_by: str) -> None:
    db.execute('INSERT INTO links(from_id, to_id, relation, recorded_at, recorded_by) VALUES(?,?,?,?,?)',
               (source, target, relation, at, recorded_by))


ROW_COLUMNS = ('id', 'kind', 'entity_id', 'predicate', 'object_entity_id', 'statement', 'data_json', 'valid_from',
               'valid_to', 'recorded_to', 'status', 'source_kind')


def _open_row(db, assertion_id: int) -> dict:
    """The assertion as ``ROW_COLUMNS`` (``data`` decoded); ``assertion_not_found`` or ``already_closed``."""
    if type(assertion_id) is not int or assertion_id < 1:
        raise _invalid('assertion_id must be a positive integer')
    row = db.execute(f'SELECT {", ".join(ROW_COLUMNS)} FROM assertions WHERE id=?', (assertion_id,)).fetchone()
    if row is None:
        raise KnowledgeError('assertion_not_found', f'assertion {assertion_id} not found')
    out = dict(zip(ROW_COLUMNS, row))
    if out['recorded_to'] is not None:
        raise KnowledgeError('already_closed', f"assertion {assertion_id} is already {out['status']} "
                                               f"(recorded_to {out['recorded_to']})")
    out['data'] = json.loads(out.pop('data_json') or '{}')
    return out


# ---- normalisation (design M4 §2.2) --------------------------------------------------------------------

def _options(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > OPTIONS_MAX:
        raise _invalid(f'options must be a list of at most {OPTIONS_MAX} strings')
    return [norm_text(item, f'options[{n}]', OPTION_MAX) for n, item in enumerate(value, 1)]


def _period(valid_from: str | None, valid_to: str | None) -> None:
    if valid_from is not None and valid_to is not None and not valid_from < valid_to:
        raise _invalid('valid_from must be before valid_to')


def _content(kind: str, fields: Mapping, today: str) -> dict:
    """Checked content of a new row of ``kind``: ``entity_id, predicate, object_entity_id, statement`` (None
    for a fact without one: made from the content), ``data``, ``valid_from``, ``valid_to``. ``fields`` holds
    every argument of ``FIELDS[kind]`` (None: not given)."""
    if kind == 'fact':
        entity_id = _optional_id(fields.get('entity_id'), 'entity_id')
        if entity_id is None:
            raise _invalid('entity_id is required for a fact')
        value = _optional_text(fields.get('value'), 'value', VALUE_MAX)
        unit = _optional_text(fields.get('unit'), 'unit', UNIT_MAX)
        object_entity_id = _optional_id(fields.get('object_entity_id'), 'object_entity_id')
        if value is None and object_entity_id is None:
            raise _invalid('a fact needs value or object_entity_id')
        if unit is not None and value is None:
            raise _invalid('unit needs value')
        valid_from = _optional_date(fields.get('valid_from'), 'valid_from')
        valid_to = _optional_date(fields.get('valid_to'), 'valid_to')
        _period(valid_from, valid_to)
        return {'entity_id': entity_id, 'predicate': norm_text(fields.get('predicate'), 'predicate', PREDICATE_MAX),
                'object_entity_id': object_entity_id,
                'statement': _optional_text(fields.get('statement'), 'statement', STATEMENT_MAX),
                'data': {'value': value, 'unit': unit}, 'valid_from': valid_from, 'valid_to': valid_to}
    statement = norm_text(fields.get('statement'), 'statement', STATEMENT_MAX)
    entity_id = _optional_id(fields.get('entity_id'), 'entity_id')
    if kind == 'decision':
        valid_from = _optional_date(fields.get('decided_on'), 'decided_on')
        valid_to = _optional_date(fields.get('valid_to'), 'valid_to')
        _period(valid_from, valid_to)
        data = {'options': _options(fields.get('options')),
                'chosen': _optional_text(fields.get('chosen'), 'chosen', CHOSEN_MAX),
                'rationale': _optional_text(fields.get('rationale'), 'rationale', RATIONALE_MAX)}
        return {'entity_id': entity_id, 'predicate': None, 'object_entity_id': None, 'statement': statement,
                'data': data, 'valid_from': valid_from, 'valid_to': valid_to}
    if kind != 'todo':
        raise _invalid(f'kind must be one of {", ".join(ASSERTION_KINDS)}')
    state = 'open' if fields.get('state') is None else fields['state']
    if state not in TASK_STATES:
        raise _invalid(f'state must be one of {"|".join(TASK_STATES)}')
    done_at = _optional_date(fields.get('done_at'), 'done_at')
    if state == 'open' and done_at is not None:
        raise _invalid('done_at is only for a task that is done or cancelled')
    if state == 'done' and done_at is None:
        done_at = today
    data = {'state': state, 'due': _optional_date(fields.get('due'), 'due'),
            'owner_entity_id': _optional_id(fields.get('owner_entity_id'), 'owner_entity_id'), 'done_at': done_at}
    return {'entity_id': entity_id, 'predicate': None, 'object_entity_id': None, 'statement': statement,
            'data': data, 'valid_from': fields.get('valid_from') or today, 'valid_to': None}


def _fields_of(kind: str, row: Mapping) -> dict:
    """The ``FIELDS[kind]`` arguments that would make ``row`` again (supersede copies what is not given)."""
    data = row['data']
    if kind == 'fact':
        return {'entity_id': row['entity_id'], 'predicate': row['predicate'], 'value': data.get('value'),
                'unit': data.get('unit'), 'object_entity_id': row['object_entity_id'], 'statement': row['statement'],
                'valid_from': row['valid_from'], 'valid_to': row['valid_to']}
    if kind == 'decision':
        return {'statement': row['statement'], 'entity_id': row['entity_id'], 'options': data.get('options') or None,
                'chosen': data.get('chosen'), 'rationale': data.get('rationale'), 'decided_on': row['valid_from'],
                'valid_to': row['valid_to']}
    return {'statement': row['statement'], 'entity_id': row['entity_id'], 'due': data.get('due'),
            'owner_entity_id': data.get('owner_entity_id'), 'state': data.get('state'), 'done_at': data.get('done_at')}


def _require_content_entities(db, spec: Mapping) -> None:
    _require_entity(db, spec['entity_id'], 'entity_id')
    _require_entity(db, spec['object_entity_id'], 'object_entity_id')
    _require_entity(db, spec['data'].get('owner_entity_id'), 'owner_entity_id')


def _statement(db, spec: Mapping) -> str:
    """The given statement, or for a fact ``"{entity} {predicate}: {value}{unit}"`` / ``"{entity} {predicate}
    {object}"`` (design M4 §6.2), cut to ``STATEMENT_MAX``."""
    if spec['statement'] is not None:
        return spec['statement']
    head = f"{_entity_name(db, spec['entity_id'])} {spec['predicate']}"
    value, unit = spec['data']['value'], spec['data']['unit']
    if value is not None:
        text = f"{head}: {value}{unit or ''}"
    else:
        text = f"{head} {_entity_name(db, spec['object_entity_id'])}"
    return text[:STATEMENT_MAX]


def _check_evidence_args(sources, my_note) -> None:
    if type(my_note) is not bool:
        raise _invalid('my_note must be true or false')
    if sources is not None and not isinstance(sources, (list, tuple)):
        raise _invalid('sources must be a list')
    if (sources is not None) == my_note or (sources is not None and not sources):
        raise KnowledgeError('evidence_required', 'give exactly one of sources (a non-empty list) or my_note: true')
    if sources is not None and len(sources) > SOURCES_MAX:
        raise _invalid(f'sources must have 1-{SOURCES_MAX} items')


# ---- source binding (design M4 §3.2) -------------------------------------------------------------------

def _version_facts(db, version_id: int) -> tuple[int, str, str, bool]:
    """``(document_id, path, sha256, current)`` of a stored version."""
    row = db.execute("SELECT v.document_id, d.path, v.sha256, v.superseded_at IS NULL AND d.status = 'active' "
                     'FROM versions v JOIN documents d ON d.id = v.document_id WHERE v.id = ?', (version_id,)).fetchone()
    return row[0], row[1], row[2], bool(row[3])


def _targets(db, n: int, item: Mapping) -> tuple[int, list[tuple[dict, str]], str]:
    """``(version_id, [(unit, unit_text)], label)`` a source item points at (design M4 §3.2 step 1)."""
    if item.get('chunk_id') is not None:
        chunk_id = item['chunk_id']
        row = db.execute('SELECT version_id, unit_first, unit_last FROM chunks WHERE id=?', (chunk_id,)).fetchone()
        if row is None:
            raise KnowledgeError('chunk_not_found', f'sources[{n}]: chunk {chunk_id} not found; {CHUNK_GONE_HINT}')
        version_id, first, last = row
        units = [(json.loads(unit), text) for unit, text in db.execute(
            'SELECT locator_json, text FROM units WHERE version_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal',
            (version_id, first, last))]
        return version_id, units, f'chunk {chunk_id}'
    obj = item['locator']
    try:
        locator.check_shape(obj)
    except locator.LocatorError as exc:
        raise KnowledgeError('locator_invalid', f'sources[{n}]: {exc.code}') from None
    row = db.execute('SELECT id FROM versions WHERE document_id=? AND sha256=? ORDER BY id DESC LIMIT 1',
                     (obj['doc'], obj['sha256'])).fetchone()   # the lookup of binding.validate_locator
    if row is None:
        raise KnowledgeError('not_found', f"sources[{n}]: version not found (doc {obj['doc']} sha {obj['sha256'][:12]})")
    label = locator.unit_label(obj['unit'])
    unit = db.execute('SELECT text FROM units WHERE version_id=? AND locator_json=?',
                      (row[0], parsers.unit_json(obj['unit']))).fetchone()
    if unit is None:
        raise KnowledgeError('not_found', f'sources[{n}]: unit not found ({label})')
    return row[0], [(obj['unit'], unit[0])], label


def _exact(text: str, quote: str) -> list[tuple[int, int]]:
    found, at = [], text.find(quote)
    while at >= 0:
        found.append((at, at + len(quote)))
        at = text.find(quote, at + len(quote))
    return found


def _flexible(text: str, quote: str) -> list[tuple[int, int]]:
    """Places where ``quote`` occurs with any run of whitespace where it has whitespace."""
    parts = quote.split()
    if len(parts) < 2:
        return []
    return [match.span() for match in re.finditer(r'\s+'.join(re.escape(part) for part in parts), text)]


def _candidates(units: list[tuple[dict, str]], quote: str, raw: str) -> tuple[list[tuple[int, int, int]], bool]:
    """``([(unit index, start, end)], flexible)``: the first of exact, whitespace-flexible, and both again
    with the quote as sent (no NFC) that finds anything (design M4 §3.2 step 3)."""
    attempts = [(quote, _exact, False), (quote, _flexible, True)]
    if raw != quote:
        attempts += [(raw, _exact, False), (raw, _flexible, True)]
    for text, find, flexible in attempts:
        found = [(index, start, end) for index, (_, unit_text) in enumerate(units) for start, end in find(unit_text, text)]
        if found:
            return found, flexible
    return [], False


def _bind_one(db, n: int, item) -> Bound:
    if not isinstance(item, Mapping) or set(item) - {'chunk_id', 'locator', 'quote'}:
        raise _invalid(f'sources[{n}] must be an object with chunk_id or locator, and quote')
    if (item.get('chunk_id') is None) == (item.get('locator') is None):
        raise _invalid(f'sources[{n}] needs exactly one of chunk_id or locator')
    if item.get('chunk_id') is not None and (type(item['chunk_id']) is not int or item['chunk_id'] < 1):
        raise _invalid(f'sources[{n}].chunk_id must be a positive integer')
    quote = norm_text(item.get('quote'), f'sources[{n}].quote', QUOTE_MAX, min_chars=QUOTE_MIN)
    version_id, units, label = _targets(db, n, item)
    found, flexible = _candidates(units, quote, item['quote'].strip())
    if not found:
        raise KnowledgeError('evidence_quote_not_found',
                             f'sources[{n}]: the quote ({len(quote)} characters) is not in {label}; {QUOTE_HINT}')
    if len(found) > 1:
        count = len(found)
        hint = item['locator'].get('chars') if item.get('locator') is not None else None
        found = [place for place in found if isinstance(hint, list) and [place[1], place[2]] == hint]
        if len(found) != 1:
            raise KnowledgeError('evidence_quote_ambiguous',
                                 f'sources[{n}]: the quote occurs {count} times in {label}; '
                                 'give a locator whose chars names one place')
    index, start, end = found[0]
    unit, unit_text = units[index]
    doc, path, sha256, current = _version_facts(db, version_id)
    obj = locator.make(doc, path, sha256, unit, unit_text, [start, end])
    stored = unit_text[start:end]
    return Bound(version_id=version_id, chunk_id=item.get('chunk_id'), locator=obj, quote=stored,
                 quote_sha256=_sha(stored), current=current, flexible=flexible and stored != quote)


def bind_sources(db, sources: Sequence[Mapping]) -> list[Bound]:
    """Bind each ``{chunk_id | locator, quote}`` to the stored unit text (design M4 §3.2): find the unit(s),
    NFC+strip the quote, exact match, else whitespace-flexible match, else the un-normalised quote; exactly
    one place is taken (or the one the client's ``locator.chars`` names). Same unit and range twice is
    kept once. Runs inside the caller's transaction. Raises ``KnowledgeError`` (``chunk_not_found`` with
    ``CHUNK_GONE_HINT``, ``locator_invalid``, ``not_found``, ``evidence_quote_not_found``,
    ``evidence_quote_ambiguous``, ``argument_invalid``)."""
    bound, seen = [], set()
    for n, item in enumerate(sources, 1):
        one = _bind_one(db, n, item)
        key = (one.version_id, parsers.unit_json(one.locator['unit']), tuple(one.locator['chars']))
        if key not in seen:
            seen.add(key)
            bound.append(one)
    return bound


def _copy_evidence(db, source_id: int) -> list[Bound]:
    """The evidence of ``source_id`` re-checked against its stored unit (the cut row of supersede)."""
    out = []
    for version_id, chunk_id, locator_json, quote in db.execute(
            'SELECT version_id, chunk_id, locator_json, quote FROM evidence WHERE assertion_id=? ORDER BY id',
            (source_id,)).fetchall():
        obj = json.loads(locator_json)
        unit = db.execute('SELECT text FROM units WHERE version_id=? AND locator_json=?',
                          (version_id, parsers.unit_json(obj['unit']))).fetchone()
        if unit is None or locator.check_text(obj, unit[0]) != quote:
            raise KnowledgeError('not_found', f'evidence of assertion {source_id} no longer matches its stored unit')
        current = _version_facts(db, version_id)[3]
        out.append(Bound(version_id, chunk_id, obj, quote, _sha(quote), current, False))
    return out


# ---- rows, conflicts, events (design M4 §4) -----------------------------------------------------------

def _insert(db, kind: str, spec: Mapping, *, statement: str, now: str, recorded_by: str, supersedes_id: int | None,
            source_kind: str, evidence: Sequence[Bound]) -> int:
    new_id = db.execute(
        'INSERT INTO assertions(kind, entity_id, predicate, object_entity_id, statement, data_json, valid_from, '
        "valid_to, recorded_from, status, supersedes_id, source_kind, recorded_by) VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,?)",
        (kind, spec['entity_id'], spec['predicate'], spec['object_entity_id'], statement, _dumps(spec['data']),
         spec['valid_from'], spec['valid_to'], now, supersedes_id, source_kind, recorded_by)).lastrowid
    db.executemany('INSERT INTO evidence(assertion_id, version_id, chunk_id, locator_json, quote, quote_sha256) '
                   'VALUES(?,?,?,?,?,?)',
                   [(new_id, b.version_id, b.chunk_id, json.dumps(b.locator, ensure_ascii=False), b.quote,
                     b.quote_sha256) for b in evidence])
    return new_id


def _value_key(data: Mapping, object_entity_id: int | None) -> tuple:
    """What makes two facts with the same subject and predicate agree (design M4 §4.4)."""
    value = data.get('value')
    return (unicodedata.normalize('NFC', value).strip() if isinstance(value, str) else '', data.get('unit') or '',
            object_entity_id or 0)


def _detect(db, new_id: int, now: str, recorded_by: str, call_id: str | None) -> tuple[list[dict], list[int]]:
    """Mark the open facts that conflict with fact ``new_id`` (same subject and predicate, overlapping
    validity, another value): both ``conflict``, a ``conflicts`` link, a ``conflict_marked`` event each.
    Returns ``(conflicts as knowledge_query.CONFLICT_KEYS, duplicate ids)``."""
    entity_id, predicate, object_entity_id, data_json, valid_from, valid_to = db.execute(
        'SELECT entity_id, predicate, object_entity_id, data_json, valid_from, valid_to FROM assertions WHERE id=?',
        (new_id,)).fetchone()
    key = _value_key(json.loads(data_json), object_entity_id)
    rows = db.execute(
        "SELECT id, object_entity_id, data_json, valid_from, valid_to, recorded_from FROM assertions WHERE kind='fact' "
        'AND entity_id=? AND predicate=? AND recorded_to IS NULL AND id<>? '
        'AND COALESCE(valid_from, ?) < ? AND ? < COALESCE(valid_to, ?) ORDER BY id',
        (entity_id, predicate, new_id, DATE_MIN, valid_to or DATE_MAX, valid_from or DATE_MIN, DATE_MAX)).fetchall()
    conflicts, duplicates = [], []
    for other, other_object, other_data, other_from, other_to, other_recorded in rows:
        data = json.loads(other_data)
        if _value_key(data, other_object) == key:
            duplicates.append(other)
            continue
        db.execute("UPDATE assertions SET status='conflict' WHERE id IN (?, ?) AND status='active'", (new_id, other))
        _link(db, new_id, other, 'conflicts', now, recorded_by)
        _event(db, new_id, now, 'conflict_marked', related_id=other, recorded_by=recorded_by, call_id=call_id)
        _event(db, other, now, 'conflict_marked', related_id=new_id, recorded_by=recorded_by, call_id=call_id)
        conflicts.append({'id': other, 'value': data.get('value'), 'valid_from': other_from, 'valid_to': other_to,
                          'recorded_from': other_recorded})
    return conflicts, duplicates


def _resolve(db, closed: int, closer: int, now: str, recorded_by: str, call_id: str | None) -> list[int]:
    """Resolve the open conflict links of the row ``closed`` (design M4 §4.5); a partner with no other open
    conflict goes back to ``active`` with a ``conflict_cleared`` event. Returns the partners' ids."""
    links = db.execute("SELECT id, from_id, to_id FROM links WHERE relation='conflicts' AND resolved_at IS NULL "
                       'AND (from_id=? OR to_id=?) ORDER BY id', (closed, closed)).fetchall()
    partners = []
    for link_id, source, target in links:
        db.execute('UPDATE links SET resolved_at=?, resolved_by_id=? WHERE id=?', (now, closer, link_id))
        partner = target if source == closed else source
        if partner not in partners:
            partners.append(partner)
    for partner in partners:
        still = db.execute("SELECT 1 FROM links WHERE relation='conflicts' AND resolved_at IS NULL "
                           'AND (from_id=? OR to_id=?)', (partner, partner)).fetchone()
        if still is None and db.execute("UPDATE assertions SET status='active' WHERE id=? AND status='conflict'",
                                        (partner,)).rowcount:
            _event(db, partner, now, 'conflict_cleared', related_id=closed, recorded_by=recorded_by, call_id=call_id)
    return partners


def _close(db, assertion_id: int, status: str, reason: str, now: str) -> None:
    db.execute('UPDATE assertions SET status=?, recorded_to=?, change_reason=? WHERE id=? AND recorded_to IS NULL',
               (status, now, reason, assertion_id))


def _evidence_notices(bound: Sequence[Bound]) -> list[str]:
    notice = []
    if old := sum(not b.current for b in bound):
        notice.append(OLD_SOURCE_NOTICE.format(old))
    if flexible := sum(b.flexible for b in bound):
        notice.append(FLEXIBLE_NOTICE.format(flexible))
    return notice


def _found_notices(conflicts: Sequence[Mapping], duplicates: Sequence[int]) -> list[str]:
    notice = []
    if duplicates:
        notice.append(DUPLICATE_NOTICE.format(', '.join(map(str, duplicates))))
    if conflicts:
        notice.append(CONFLICT_NOTICE.format(', '.join(str(c['id']) for c in conflicts)))
    return notice


def _row(db, assertion_id: int, with_evidence: bool = True) -> dict:
    return knowledge_query.assertion_row(db, assertion_id, with_evidence=with_evidence)


# ---- the writers -----------------------------------------------------------------------------------------

def record_entity(db, *, kind: str, name: str, aliases: Sequence[str] | None = None,
                  note: str | None = None, recorded_by: str) -> dict:
    """``record_entity`` (design M4 §6.2): create ``(kind, name)`` or, when it exists, add the new aliases
    (and the note when it has none). Returns ``{'entity', 'created', 'added_aliases', 'alias_of', 'notice'}``;
    ``alias_of`` lists other entities that have ``name`` as an alias (never merged)."""
    if kind not in ENTITY_KINDS:
        raise _invalid(f'kind must be one of {"|".join(ENTITY_KINDS)}')
    name = norm_text(name, 'name', NAME_MAX)
    if aliases is not None and not isinstance(aliases, (list, tuple)):
        raise _invalid('aliases must be a list of strings')
    wanted = []
    for n, alias in enumerate(aliases or (), 1):
        alias = norm_text(alias, f'aliases[{n}]', ALIAS_MAX)
        if alias != name and alias not in wanted:
            wanted.append(alias)
    note = _optional_text(note, 'note', NOTE_MAX)

    def work(now: str):
        row = db.execute('SELECT id, aliases_json, note FROM entities WHERE kind=? AND name=?', (kind, name)).fetchone()
        if row is None:
            if len(wanted) > ALIASES_MAX:
                raise _invalid(f'an entity has at most {ALIASES_MAX} aliases')
            entity_id = db.execute('INSERT INTO entities(kind, name, aliases_json, note, created_at, created_by) '
                                   'VALUES(?,?,?,?,?,?)', (kind, name, _dumps(wanted), note, now, recorded_by)).lastrowid
            created, added = True, wanted
        else:
            entity_id, known, old_note = row[0], json.loads(row[1] or '[]'), row[2]
            added = [alias for alias in wanted if alias not in known]
            if len(known) + len(added) > ALIASES_MAX:
                raise _invalid(f'an entity has at most {ALIASES_MAX} aliases')
            if added or (note is not None and old_note is None):
                db.execute('UPDATE entities SET aliases_json=?, note=? WHERE id=?',
                           (_dumps(known + added), old_note if old_note is not None else note, entity_id))
            created = False
        alias_of = [{'id': other, 'kind': other_kind, 'name': other_name} for other, other_kind, other_name in db.execute(
            'SELECT DISTINCT e.id, e.kind, e.name FROM entities e, json_each(e.aliases_json) j '
            'WHERE j.value = ? AND e.id <> ? ORDER BY e.id', (name, entity_id))]
        return entity_id, created, added, alias_of

    entity_id, created, added, alias_of = _write(db, work)
    notice = [ALIAS_OF_NOTICE.format(', '.join(str(a['id']) for a in alias_of))] if alias_of else []
    return {'entity': knowledge_query.entity_row(db, entity_id), 'created': created, 'added_aliases': added,
            'alias_of': alias_of, 'notice': notice}


def record(db, kind: str, *, recorded_by: str, call_id: str | None,
           sources: Sequence[Mapping] | None = None, my_note: bool = False, **fields) -> dict:
    """``record_fact``/``record_decision``/``record_task`` (design M4 §4.1, §6.2, appendix A): ``kind`` in
    ``ASSERTION_KINDS``, ``fields`` from ``FIELDS[kind]``; exactly one of ``sources`` (1-``SOURCES_MAX``) or
    ``my_note`` (``evidence_required``). One transaction: assertion, evidence, conflict detection (facts),
    links, events. Returns ``{'assertion', 'conflicts', 'duplicates', 'notice'}`` (``assertion`` with
    evidence; ``conflicts``/``duplicates`` empty for decisions and tasks)."""
    if kind not in ASSERTION_KINDS:
        raise _invalid(f'kind must be one of {"|".join(ASSERTION_KINDS)}')
    if unknown := sorted(set(fields) - set(FIELDS[kind])):
        raise _invalid(f'{kind} has no argument {unknown[0]}')
    _check_evidence_args(sources, my_note)

    def work(now: str):
        spec = _content(kind, fields, now[:DATE_LENGTH])
        _require_content_entities(db, spec)
        bound = bind_sources(db, sources) if sources else []
        new_id = _insert(db, kind, spec, statement=_statement(db, spec), now=now, recorded_by=recorded_by,
                         supersedes_id=None, source_kind='evidence' if bound else 'my_note', evidence=bound)
        _event(db, new_id, now, 'recorded', recorded_by=recorded_by, call_id=call_id)
        conflicts, duplicates = _detect(db, new_id, now, recorded_by, call_id) if kind == 'fact' else ([], [])
        return new_id, bound, conflicts, duplicates

    new_id, bound, conflicts, duplicates = _write(db, work)
    return {'assertion': _row(db, new_id), 'conflicts': conflicts, 'duplicates': duplicates,
            'notice': _evidence_notices(bound) + _found_notices(conflicts, duplicates)}


CONTENT_FIELDS = {'fact': ('entity_id', 'predicate', 'value', 'unit', 'object_entity_id')}  # remake the statement
# Events of a supersede: the old row 'superseded' (related: C or N); C 'recorded' (related: old) when cut;
# N 'recorded' (related: its supersedes_id). Links: C -> old and N -> C, or N -> old ('supersedes').


def supersede(db, assertion_id: int, *, reason: str, recorded_by: str, call_id: str | None,
              sources: Sequence[Mapping] | None = None, my_note: bool = False, old_valid_to: str | None = None,
              **fields) -> dict:
    """``supersede`` (design M4 §4.3): a new row with ``fields`` changed (the rest copied from the old row,
    ``kind_mismatch`` for a field of another kind), the old row closed ``superseded``; with
    ``old_valid_to`` a cut row (old content ending at that date, evidence copied) in between, which stays
    open (module docstring). Sources are always needed again. Returns ``{'old', 'cut', 'new', 'conflicts',
    'duplicates', 'cleared_conflicts', 'notice'}``. ``assertion_not_found``, ``already_closed``."""
    reason = norm_text(reason, 'reason', REASON_MAX)
    _check_evidence_args(sources, my_note)
    old_valid_to = _optional_date(old_valid_to, 'old_valid_to')
    given = {name: value for name, value in fields.items() if value is not None}

    def work(now: str):
        old = _open_row(db, assertion_id)
        kind = old['kind']
        if kind not in FIELDS:
            raise KnowledgeError('kind_mismatch', f'assertion {assertion_id} is a {kind}, which M4 does not write')
        if wrong := sorted(set(given) - set(FIELDS[kind])):
            raise KnowledgeError('kind_mismatch', f'a {kind} has no argument {wrong[0]}')
        merged = {**_fields_of(kind, old), **given}
        if kind == 'fact' and 'statement' not in given and any(name in given for name in CONTENT_FIELDS['fact']):
            merged['statement'] = None                      # made again from the new content
        if kind == 'todo':
            merged['valid_from'] = old['valid_from']
            if 'state' in given and given['state'] != 'done' and 'done_at' not in given:
                merged['done_at'] = None
            if given.get('state') == 'done' and 'done_at' not in given and old['data'].get('state') != 'done':
                merged['done_at'] = None                    # defaults to today
        cut = None
        if old_valid_to is not None:
            if kind == 'todo':
                raise KnowledgeError('kind_mismatch', 'a task has no validity period to cut (old_valid_to)')
            if (old['valid_from'] is not None and not old['valid_from'] < old_valid_to) or \
                    (old['valid_to'] is not None and not old_valid_to < old['valid_to']):
                raise _invalid('old_valid_to must fall inside the old validity period')
            start = 'valid_from' if kind == 'fact' else 'decided_on'
            if start not in given:
                merged[start] = old_valid_to                # the new content holds from the cut on
            cut = {**_content(kind, _fields_of(kind, old), now[:DATE_LENGTH]), 'valid_to': old_valid_to}
        spec = _content(kind, merged, now[:DATE_LENGTH])
        _require_content_entities(db, spec)
        bound = bind_sources(db, sources) if sources else []
        # Order: close the old row first (it is no conflict partner), then C and N each meet the open facts.
        _close(db, assertion_id, 'superseded', reason, now)
        conflicts, duplicates = [], []

        def add(kind_spec, statement, supersedes_id, source_kind, evidence) -> int:
            row_id = _insert(db, kind, kind_spec, statement=statement, now=now, recorded_by=recorded_by,
                             supersedes_id=supersedes_id, source_kind=source_kind, evidence=evidence)
            _link(db, row_id, supersedes_id, 'supersedes', now, recorded_by)
            _event(db, row_id, now, 'recorded', related_id=supersedes_id, recorded_by=recorded_by, call_id=call_id)
            if kind == 'fact':
                found, same = _detect(db, row_id, now, recorded_by, call_id)
                conflicts.extend(found)
                duplicates.extend(d for d in same if d not in duplicates)
            return row_id

        cut_id = None if cut is None else add(cut, old['statement'], assertion_id, old['source_kind'],
                                              _copy_evidence(db, assertion_id))
        parent = cut_id or assertion_id
        new_id = add(spec, _statement(db, spec), parent, 'evidence' if bound else 'my_note', bound)
        _event(db, assertion_id, now, 'superseded', reason=reason, related_id=cut_id or new_id,
               recorded_by=recorded_by, call_id=call_id)
        cleared = _resolve(db, assertion_id, new_id, now, recorded_by, call_id)
        return cut_id, new_id, bound, conflicts, duplicates, cleared

    cut_id, new_id, bound, conflicts, duplicates, cleared = _write(db, work)
    return {'old': _row(db, assertion_id, with_evidence=False), 'cut': _row(db, cut_id) if cut_id else None,
            'new': _row(db, new_id), 'conflicts': conflicts, 'duplicates': duplicates, 'cleared_conflicts': cleared,
            'notice': _evidence_notices(bound) + _found_notices(conflicts, duplicates)}


def retract(db, assertion_id: int, *, reason: str, recorded_by: str,
            call_id: str | None) -> dict:
    """``retract`` (design M4 §4.2): close the row ``withdrawn`` with ``reason``, resolve its conflicts.
    Returns ``{'assertion', 'cleared_conflicts', 'notice'}`` (the closed row without evidence quotes).
    ``assertion_not_found``, ``already_closed``."""
    reason = norm_text(reason, 'reason', REASON_MAX)

    def work(now: str):
        _open_row(db, assertion_id)
        _close(db, assertion_id, 'withdrawn', reason, now)
        _event(db, assertion_id, now, 'retracted', reason=reason, recorded_by=recorded_by, call_id=call_id)
        return _resolve(db, assertion_id, assertion_id, now, recorded_by, call_id)

    cleared = _write(db, work)
    return {'assertion': _row(db, assertion_id, with_evidence=False), 'cleared_conflicts': cleared, 'notice': []}

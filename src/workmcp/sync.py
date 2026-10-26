"""Wire schema of ``workmcp push``, shared by the collector (``push.py``) and the operating server
(``receive.py``, ``remote.py``) (design M5 remote §3).

No network, no process, no file access: the batch envelope and the allowed keys of each batch kind, the
size limits both sides cut by, the batch id (SHA-256 of the canonical body), gzip JSON bodies with a
decompressed-size limit, fp32 vector blobs as base64, the two summary hashes both sides compute with the
same function from their own store (``summary``), the server URL rules and endpoint paths, and the error
codes of a push answer. The answer envelope has the tool envelope's shape: ``{"ok": true, "result": ...}``
or ``{"ok": false, "error": {"code", "message", "retryable"}}``.

Stage 1 additions to the stage-0 names (recorded as design deviations): ``begin`` carries ``started_at`` (two
runs never share a batch id) and ``model_key`` (the collector's vectors); a version in ``documents`` carries its
``units_sha256`` (the server answers ``have`` only for the same unit set); a unit longer than one part travels
as consecutive pieces with the same ordinal (``unit_pieces``); one ``units`` batch may hold the parts of several
versions; ``GET push/state`` also answers ``events_count``.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from . import chunks, store

PROTOCOL = 1
KINDS = ('begin', 'documents', 'forget', 'units', 'vectors', 'events', 'end')
ENVELOPE_KEYS = frozenset({'protocol', 'collector', 'push_id', 'batch_id', 'kind', 'chunk_version', 'schema_version',
                           'client_version', 'items'})
# The keys an item of each kind may have (all of them, design §3.3); nested lists have their own keys.
# ``path_key`` is the collector's own normalised key (risk 15: the server stores it, never recomputes it).
WIRE_KEYS = {
    'begin': frozenset({'roots', 'counts_expected', 'started_at', 'nonce', 'model_key', 'events_reset'}),
    'documents': frozenset({'local_document_id', 'local_root_id', 'path', 'path_key', 'ext', 'status', 'skip_reason',
                            'first_seen_at', 'last_seen_at', 'deleted_at', 'versions'}),
    'forget': frozenset({'local_document_id'}),
    'units': frozenset({'local_version_id', 'units_sha256', 'part', 'parts', 'units'}),
    'vectors': frozenset({'model_key', 'dims', 'items'}),
    'events': frozenset({'local_event_id', 'local_document_id', 'local_version_id', 'kind', 'at'}),
    'end': frozenset({'counts', 'bytes_sent', 'complete'}),
}
NESTED_KEYS = {
    ('begin', 'roots'): frozenset({'local_root_id', 'path', 'enabled'}),
    ('documents', 'versions'): frozenset({'local_version_id', 'seq', 'sha256', 'size', 'mtime_ns', 'observed_at',
                                          'superseded_at', 'parser', 'parser_version', 'encoding', 'parse_status',
                                          'parse_error', 'unit_count', 'text_chars', 'units_sha256',
                                          'unit_chars_max'}),
    ('units', 'units'): frozenset({'ordinal', 'kind', 'locator_json', 'text', 'text_sha256'}),
    ('vectors', 'items'): frozenset({'text_sha256', 'vector_b64'}),
}
COUNT_FIELDS = {('begin', 'counts_expected'), ('end', 'counts')}   # {name: int} maps, names free

# Limits (design §3.1-4); the collector cuts by the same values the server checks.
BATCH_MAX_BYTES = 8 * 2**20          # one batch body after gzip decompression ([server] push_max_mb default)
DOCUMENTS_PER_BATCH = 200
UNITS_PER_BATCH = 2_000
UNIT_CHARS_PER_BATCH = 2 * 2**20     # unit text characters in one units part
VECTORS_PER_BATCH = 1_024
EVENTS_PER_BATCH = 5_000
MAX_PARTS = 256                      # one version: 200,000 units / 2,000 = 100; 50 M chars of long units / 256 Ki = 191
# The largest version the server takes (the collector's default parsers.Limits; a test keeps them equal): a
# document with a larger version is refused alone (``version_too_large``).
VERSION_MAX_UNITS = 200_000
VERSION_MAX_CHARS = 50_000_000
UNIT_MAX_CHARS = 8 * 2**20          # one unit's text (parsers.Limits.max_unit_chars; review v2 N-2)
INT_MAX = 2**63 - 1                  # every integer on the wire fits SQLite's INTEGER
UNIT_PIECE_CHARS = 2**18             # a longer unit text travels as pieces of at most this many characters
ENVELOPE_RESERVE = 2**16             # bytes kept free in a batch for the envelope and the item keys
STATE_PAGE_DOCUMENTS = 500
STATE_PAGE_VECTORS = 5_000
VECTOR_DIMS = 1_024
VECTOR_B64_CHARS = 4 * ((VECTOR_DIMS * 4 + 2) // 3)   # 5,464

UNIT_STATES = ('have', 'copied', 'need', 'none')
SUMMARY_KEYS = ('documents_sha256', 'vectors_sha256')
# GET push/state: one small answer (design §3.2).
STATE_KEYS = ('collector', 'schema_version', 'chunk_version', 'model_keys', 'documents_sha256', 'vectors_sha256',
              'events_sha256', 'events_max')
# GET push/state?full=1&after=N: one page of per-document summaries.
# No sequence number: the server numbers its versions itself (a rebuilt collector store starts again at 1);
# ``superseded`` counts the superseded versions.
DOCUMENT_SUMMARY_KEYS = ('local_document_id', 'status', 'path', 'sha256', 'parse_status', 'parser_version',
                         'units_sha256', 'deleted_at', 'skip_reason', 'superseded')
STATE_PAGE_KEYS = ('documents', 'next_after')
# GET push/state/vectors?model_key=K&after=SHA: text_sha256 of target chunks the server has no vector for.
VECTORS_PAGE_KEYS = ('missing', 'next_after')
# ``result`` of a successful batch per kind. ``units``: one ``versions`` entry per item, UNITS_VERSION_KEYS for a
# version whose units are now stored, UNITS_PARTIAL_KEYS for a part of a split version that is not the last.
RESULT_KEYS = {
    'begin': ('push_id', 'root_ids'),
    'documents': ('documents', 'versions', 'refused'),
    'forget': ('forgotten', 'ignored'),
    'units': ('versions', 'vectors_missing'),
    'vectors': ('stored', 'ignored', 'invalid'),
    'events': ('stored', 'ignored'),
    'end': ('documents_sha256', 'vectors_sha256', 'vectors_missing_total', 'swept'),
}
UNITS_PARTIAL_KEYS = ('local_version_id', 'received', 'of')
UNITS_VERSION_KEYS = ('local_version_id', 'version_id', 'units', 'chunks')   # units result: versions[]
VERSION_RESULT_KEYS = ('id', 'units')          # documents result: versions[local_version_id]
REFUSED_KEYS = ('local_document_id', 'code')   # documents result: refused[]
ENDPOINTS = {'state': 'push/state', 'vectors': 'push/state/vectors', 'batch': 'push/batch'}

# Error codes of a push answer (design §11.1) and the HTTP status the server sends with each.
ERROR_CODES = ('invalid_token', 'wrong_token_kind', 'collector_retired', 'schema_version_mismatch',
               'chunk_version_mismatch', 'batch_too_large', 'bad_units_hash', 'version_identity_conflict',
               'root_owned_by_other_collector', 'push_not_running', 'version_not_awaiting_units', 'invalid_vector',
               'bad_batch', 'store_busy', 'version_too_large', 'path_outside_root')
ERROR_HTTP_STATUS = {'invalid_token': 401, 'wrong_token_kind': 401, 'collector_retired': 403,
                     'schema_version_mismatch': 409, 'chunk_version_mismatch': 409, 'batch_too_large': 413,
                     'bad_units_hash': 400, 'version_identity_conflict': 409, 'root_owned_by_other_collector': 409,
                     'push_not_running': 409, 'version_not_awaiting_units': 409, 'invalid_vector': 400,
                     'bad_batch': 400, 'store_busy': 503, 'version_too_large': 413, 'path_outside_root': 400}
RETRYABLE = frozenset({'store_busy'})

LOOPBACK_HOSTS = ('127.0.0.1', '::1', 'localhost')
_QUERY_VALUE = re.compile(r'[A-Za-z0-9_.-]+')
_HEX64 = re.compile(r'[0-9a-f]{64}')


class SyncError(Exception):
    """A refused batch or request: ``code`` is one of ``ERROR_CODES``; the message names counts, ids or
    hashes only, never unit text."""

    def __init__(self, code: str, message: str = ''):
        if code not in ERROR_CODES:
            raise ValueError(f'unknown sync error code {code!r}')
        super().__init__(f'{code}: {message}' if message else code)
        self.code = code
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE


def ok(result) -> dict:
    return {'ok': True, 'result': result}


def error(code: str, message: str = '') -> dict:
    if code not in ERROR_CODES:
        raise ValueError(f'unknown sync error code {code!r}')
    return {'ok': False, 'error': {'code': code, 'message': message, 'retryable': code in RETRYABLE}}


# ---- bodies ---------------------------------------------------------------------------------------------

def canonical(obj) -> bytes:
    """Canonical JSON bytes (sorted keys, no spaces, UTF-8): the input of ``batch_id``."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def batch_id(body: Mapping) -> str:
    """SHA-256 of the canonical body without its own ``batch_id``: the same batch sent again has the same id
    (the server answers it from ``push_batches`` and applies nothing)."""
    return hashlib.sha256(canonical({k: v for k, v in body.items() if k != 'batch_id'})).hexdigest()


def envelope(kind: str, items: list, *, collector: str, push_id: int | None, client_version: str) -> dict:
    """A batch body of ``kind`` with the envelope fields and its ``batch_id``."""
    if kind not in KINDS:
        raise ValueError(f'unknown batch kind {kind!r}')
    body = {'protocol': PROTOCOL, 'collector': collector, 'push_id': push_id, 'kind': kind,
            'chunk_version': chunks.CHUNK_VERSION, 'schema_version': store.SCHEMA_VERSION,
            'client_version': client_version, 'items': items}
    body['batch_id'] = batch_id(body)
    return body


def _key_problems(where: str, item, allowed: frozenset, kind: str) -> list[str]:
    if not isinstance(item, dict):
        return [f'{where} is not an object']
    found = [f'{where} has unknown key {key!r}' for key in sorted(set(item) - allowed)]
    for key, value in item.items():
        if (kind, key) in NESTED_KEYS:
            if not isinstance(value, list):
                found.append(f'{where}.{key} is not a list')
                continue
            for number, inner in enumerate(value):
                found += _key_problems(f'{where}.{key}[{number}]', inner, NESTED_KEYS[(kind, key)], kind)
        elif (kind, key) in COUNT_FIELDS:
            if not isinstance(value, dict) or not all(isinstance(k, str) and type(v) is int for k, v in value.items()):
                found.append(f'{where}.{key} is not a map of integers')
    return found


def body_problems(body) -> list[str]:
    """Every key outside ``ENVELOPE_KEYS``/``WIRE_KEYS``/``NESTED_KEYS`` and every wrong container type, as
    messages naming the place (never a value). Empty for a well-formed body."""
    if not isinstance(body, dict):
        return ['body is not an object']
    found = [f'envelope has unknown key {key!r}' for key in sorted(set(body) - ENVELOPE_KEYS)]
    found += [f'envelope lacks {key!r}' for key in sorted(ENVELOPE_KEYS - set(body))]
    kind = body.get('kind')
    if kind not in KINDS:
        return found + ['unknown kind']
    items = body.get('items')
    if not isinstance(items, list):
        return found + ['items is not a list']
    for number, item in enumerate(items):
        found += _key_problems(f'items[{number}]', item, WIRE_KEYS[kind], kind)
    return found


def check_body(body) -> None:
    """``SyncError`` for a body the server must refuse before reading its items: malformed (``bad_batch``),
    another protocol (``bad_batch``), schema or chunk version (``*_mismatch``), a ``batch_id`` that is not
    the body's hash (``bad_batch``)."""
    try:
        problems = body_problems(body)
    except RecursionError:
        raise SyncError('bad_batch', 'the body is nested too deep') from None
    if problems:
        raise SyncError('bad_batch', f'{len(problems)} problem(s), first: {problems[0]}')
    if body['protocol'] != PROTOCOL:
        raise SyncError('bad_batch', f'protocol {body["protocol"]!r}, this side speaks {PROTOCOL}')
    if body['schema_version'] != store.SCHEMA_VERSION:
        raise SyncError('schema_version_mismatch', f'{body["schema_version"]!r} != {store.SCHEMA_VERSION}')
    if body['chunk_version'] != chunks.CHUNK_VERSION:
        raise SyncError('chunk_version_mismatch', f'{body["chunk_version"]!r} != {chunks.CHUNK_VERSION}')
    try:
        same = body['batch_id'] == batch_id(body)
    except (ValueError, TypeError, RecursionError):      # NaN, a huge or deeply nested value
        raise SyncError('bad_batch', 'the body has no canonical form') from None
    if not same:
        raise SyncError('bad_batch', 'batch_id is not the hash of the body')


def gzip_json(obj) -> bytes:
    """The canonical JSON of ``obj``, gzip-compressed with a fixed mtime (same bytes for the same body)."""
    return gzip.compress(canonical(obj), mtime=0)


def ungzip_json(data: bytes, max_bytes: int = BATCH_MAX_BYTES):
    """The JSON value of a gzip body; ``batch_too_large`` when it decompresses to more than ``max_bytes``
    (read stops there: no decompression bomb), ``bad_batch`` when it is not gzip, UTF-8 or JSON."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            raw = stream.read(max_bytes + 1)
    except (OSError, EOFError) as exc:
        raise SyncError('bad_batch', f'not a gzip body ({type(exc).__name__})') from None
    if len(raw) > max_bytes:
        raise SyncError('batch_too_large', f'over {max_bytes} bytes after decompression')
    try:
        return json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise SyncError('bad_batch', 'body is not UTF-8 JSON (or nested too deep)') from None


def vector_to_b64(blob: bytes) -> str:
    """A stored vector blob (fp32 little-endian, ``index.to_blob``) as base64 text."""
    return base64.b64encode(blob).decode('ascii')


def vector_from_b64(text: str, dims: int = VECTOR_DIMS) -> bytes:
    """The fp32 blob of ``vector_b64``; ``invalid_vector`` unless it is strict base64 of exactly ``dims * 4``
    bytes (finiteness and unit norm are ``index.from_blob``'s check on the server)."""
    if not isinstance(text, str):
        raise SyncError('invalid_vector', 'vector_b64 is not a string')
    try:
        blob = base64.b64decode(text.encode('ascii'), validate=True)
    except (ValueError, UnicodeEncodeError):
        raise SyncError('invalid_vector', 'vector_b64 is not base64') from None
    if len(blob) != dims * 4:
        raise SyncError('invalid_vector', f'{len(blob)} bytes, expected {dims * 4}')
    return blob


# ---- unit pieces ----------------------------------------------------------------------------------------

def unit_pieces(unit: Mapping, max_bytes: int = BATCH_MAX_BYTES - ENVELOPE_RESERVE) -> list[dict]:
    """A wire unit (``NESTED_KEYS[('units', 'units')]``) as one or more pieces whose canonical JSON is at most
    ``max_bytes``: a long text is cut into consecutive pieces of at most ``UNIT_PIECE_CHARS`` characters that
    keep the unit's ordinal, kind, locator and ``text_sha256`` (the hash of the whole text). ``join_pieces``
    puts them back together."""
    whole = dict(unit)
    if len(whole['text']) <= UNIT_PIECE_CHARS and len(canonical(whole)) <= max_bytes:
        return [whole]
    text, pieces, at, step = whole['text'], [], 0, UNIT_PIECE_CHARS
    while at < len(text):
        piece = {**whole, 'text': text[at:at + step]}
        if len(canonical(piece)) > max_bytes and step > 1:
            step //= 2
            continue
        pieces.append(piece)
        at += len(piece['text'])
    return pieces


def join_pieces(units: Iterable[Mapping]) -> list[dict]:
    """Consecutive entries with the same ordinal joined into one unit (their texts concatenated); a piece that
    disagrees with the first one on kind, locator or hash raises ``SyncError('bad_units_hash')``."""
    joined: list[dict] = []
    for unit in units:
        if joined and joined[-1]['ordinal'] == unit['ordinal']:
            last = joined[-1]
            if (last['kind'], last['locator_json'], last['text_sha256']) != \
                    (unit['kind'], unit['locator_json'], unit['text_sha256']):
                raise SyncError('bad_units_hash', f'pieces of unit {unit["ordinal"]} disagree')
            last['text'] += unit['text']
        else:
            joined.append(dict(unit))
    return joined


def canonical_size(obj) -> int:
    """Bytes of ``canonical(obj)``: what a batch body counts against ``BATCH_MAX_BYTES``."""
    return len(canonical(obj))


# ---- summary hashes (design §3.2): the same function on both sides -------------------------------------

def _field(value) -> str:
    return '' if value is None else str(value)


class _LineHash:
    """SHA-256 of lines joined with ``\\n``, fed one line at a time (no list of lines in memory)."""

    def __init__(self):
        self._hash = hashlib.sha256()
        self._first = True

    def add(self, line: str) -> None:
        self._hash.update((line if self._first else '\n' + line).encode('utf-8'))
        self._first = False

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def units_sha256(rows: Iterable) -> str:
    """SHA-256 of ``ordinal|kind|locator_json|text_sha256`` lines (rows in ordinal order; tuples of those four
    or wire unit dicts). Unit text enters through its hash only; rows are read one at a time."""
    digest = _LineHash()
    for row in rows:
        if isinstance(row, Mapping):
            row = (row['ordinal'], row['kind'], row['locator_json'], row['text_sha256'])
        ordinal, kind, locator_json, text_sha256 = row
        digest.add(f'{ordinal}|{kind}|{locator_json}|{text_sha256}')
    return digest.hexdigest()


def version_units_sha256(db, version_id: int) -> str:
    """``units_sha256`` of a stored version's units."""
    return units_sha256(db.execute('SELECT ordinal, kind, locator_json, text_sha256 FROM units WHERE version_id = ? '
                                   'ORDER BY ordinal', (version_id,)))


def document_summaries(db, *, collector_id: int | None = None, after: int = 0, limit: int | None = None) -> list[dict]:
    """Per-document summaries (``DOCUMENT_SUMMARY_KEYS``) in ``local_document_id`` order after ``after``.

    On a collector (``collector_id`` None) the local id is ``documents.id`` and the versions are all of the
    document's. On the server the local id comes from ``collector_documents`` and the versions are those
    ``collector_versions`` maps for that collector (the server may hold more history than a rebuilt collector
    store); the latest is the one with the highest sequence on that side. No sequence number enters the
    summary: the two sides number versions independently."""
    if collector_id is None:
        sql = ('SELECT d.id, d.id, d.status, d.path, d.deleted_at, d.skip_reason FROM documents d '
               'WHERE d.id > ? ORDER BY d.id LIMIT ?')
        args = (after, -1 if limit is None else limit)
        versions = 'SELECT v.id, v.sha256, v.parse_status, v.parser_version, v.superseded_at, NULL FROM versions v ' \
                   'WHERE v.document_id = ? ORDER BY v.seq'
    else:
        sql = ('SELECT m.local_document_id, d.id, d.status, d.path, d.deleted_at, d.skip_reason '
               'FROM collector_documents m JOIN documents d ON d.id = m.document_id '
               'WHERE m.collector_id = ? AND m.local_document_id > ? ORDER BY m.local_document_id LIMIT ?')
        args = (collector_id, after, -1 if limit is None else limit)
        versions = 'SELECT v.id, v.sha256, v.parse_status, v.parser_version, v.superseded_at, cv.units_sha256 ' \
                   'FROM versions v JOIN collector_versions cv ON cv.version_id = v.id AND cv.collector_id = ? ' \
                   'WHERE v.document_id = ? ORDER BY v.seq'
    result = []
    for local_id, doc_id, status, path, deleted_at, skip_reason in db.execute(sql, args).fetchall():
        rows = db.execute(versions, (doc_id,) if collector_id is None else (collector_id, doc_id)).fetchall()
        latest = rows[-1] if rows else (None, None, None, None, None, None)
        # the server keeps the hash of a unit set it checked when it stored it (collector_versions.units_sha256;
        # verify --stored compares it with the rows); without one the rows are hashed here
        units_hash = latest[5] or (None if latest[0] is None else version_units_sha256(db, latest[0]))
        result.append({'local_document_id': local_id, 'status': status, 'path': path, 'sha256': latest[1],
                       'parse_status': latest[2], 'parser_version': latest[3], 'units_sha256': units_hash,
                       'deleted_at': deleted_at, 'skip_reason': skip_reason,
                       'superseded': sum(1 for row in rows if row[4] is not None)})
    return result


def document_line(row: Mapping) -> str:
    """The ``documents_sha256`` line of one summary (design §3.2)."""
    return '|'.join(_field(row[key]) for key in DOCUMENT_SUMMARY_KEYS)


def target_vectors(db, model_key: str | None, *, collector_id: int | None = None) -> tuple[list[str], int]:
    """``(sorted distinct text_sha256 of the target chunks with a vector for model_key, count of those without)``
    in one pass (latest eligible versions, this ``CHUNK_VERSION``; on the server only that collector's versions)."""
    if model_key is None:
        return [], 0
    sql = (f'SELECT DISTINCT c.text_sha256, EXISTS(SELECT 1 FROM chunk_vectors x WHERE x.model_key = ? AND '
           f'x.text_sha256 = c.text_sha256) FROM {store.latest_chunks_sql()} AND c.chunk_version = ?')
    args: list = [model_key, chunks.CHUNK_VERSION]
    if collector_id is not None:
        sql += ' AND c.version_id IN (SELECT version_id FROM collector_versions WHERE collector_id = ?)'
        args.append(collector_id)
    have, missing = [], 0
    for sha, present in db.execute(sql + ' ORDER BY 1', args):
        if present:
            have.append(sha)
        else:
            missing += 1
    return have, missing


def vector_shas(db, model_key: str | None, *, collector_id: int | None = None) -> list[str]:
    """Sorted distinct ``text_sha256`` of the target chunks (latest eligible versions, this ``CHUNK_VERSION``)
    that have a vector for ``model_key``; on the server only that collector's versions."""
    return target_vectors(db, model_key, collector_id=collector_id)[0]


def _sha_of_lines(lines: Iterable[str]) -> str:
    digest = _LineHash()
    for line in lines:
        digest.add(line)
    return digest.hexdigest()


def summary(db, model_key: str | None, *, collector_id: int | None = None) -> dict:
    """``{documents_sha256, vectors_sha256}`` of this store (a collector) or of one collector's mirror on the
    server. Equal on both sides means a push has nothing to send (design §3.2)."""
    return summary_and_missing(db, model_key, collector_id=collector_id)[0]


def summary_and_missing(db, model_key: str | None, *, collector_id: int | None = None) -> tuple[dict, int]:
    """``summary`` and the number of target chunk texts without a vector, from one pass over the chunks."""
    lines = sorted(document_line(row) for row in document_summaries(db, collector_id=collector_id))
    have, missing = target_vectors(db, model_key, collector_id=collector_id)
    return {'documents_sha256': _sha_of_lines(lines), 'vectors_sha256': _sha_of_lines(have)}, missing


def event_rows(db, *, collector_id: int | None = None, upto: int | None = None, after: int = 0):
    """``(local_event_id, local_document_id, local_version_id, kind, at)`` of this store's events, or of the
    events the server holds for one collector (``collector_events``; document and version through the current
    maps), in local id order, ``after < id <= upto``."""
    upper = INT_MAX if upto is None else upto
    if collector_id is None:
        return db.execute('SELECT id, document_id, version_id, kind, at FROM document_events WHERE id > ? AND id <= ? '
                          'ORDER BY id', (after, upper))
    return db.execute(
        'SELECT ce.local_event_id, m.local_document_id, cv.local_version_id, e.kind, e.at FROM collector_events ce '
        'JOIN document_events e ON e.id = ce.event_id '
        'LEFT JOIN collector_documents m ON m.document_id = e.document_id AND m.collector_id = ce.collector_id '
        'LEFT JOIN collector_versions cv ON cv.version_id = e.version_id AND cv.collector_id = ce.collector_id '
        'WHERE ce.collector_id = ? AND ce.local_event_id > ? AND ce.local_event_id <= ? ORDER BY ce.local_event_id',
        (collector_id, after, upper))


def events_sha256(db, *, collector_id: int | None = None, upto: int | None = None) -> str:
    """SHA-256 of the ``event_rows`` lines up to ``upto``: equal on both sides means the server has exactly this
    collector's events (with ``upto`` the server's ``events_max``: the server has a prefix, send the rest)."""
    return _sha_of_lines('|'.join(_field(value) for value in row)
                         for row in event_rows(db, collector_id=collector_id, upto=upto))


def events_max(db, collector_id: int) -> int:
    """The highest local event id the server holds for one collector (0 for none)."""
    return db.execute('SELECT COALESCE(MAX(local_event_id), 0) FROM collector_events WHERE collector_id = ?',
                      (collector_id,)).fetchone()[0]


# ---- server URL and endpoints -----------------------------------------------------------------------------

@dataclass(frozen=True)
class UrlParts:
    scheme: str
    host: str          # lower case, IPv6 without brackets
    port: int
    path: str          # '' or '/prefix' without a trailing slash


def parse_url(url: str) -> UrlParts:
    """``[push] url`` split by hand (no URL library): ``https://HOST[:PORT][/PATH]`` or ``http://`` (loopback
    only, see ``url_problem``). ``ValueError`` for user info, a query, a fragment, spaces or a bad port."""
    if not isinstance(url, str) or not url or any(ch.isspace() or ord(ch) < 0x20 for ch in url):
        raise ValueError('url must be a non-empty string without spaces')
    scheme, sep, rest = url.partition('://')
    if not sep or scheme not in ('https', 'http'):
        raise ValueError('url must start with https:// (http:// for a loopback test server only)')
    authority, slash, path = rest.partition('/')
    if not authority or '@' in authority or '?' in rest or '#' in rest:
        raise ValueError('url must have a host and no user info, query or fragment')
    if authority.startswith('['):
        host, close, after = authority[1:].partition(']')
        if not close or (after and not after.startswith(':')):
            raise ValueError('bad IPv6 host')
        port_text = after[1:]
    else:
        if authority.count(':') > 1:
            raise ValueError('an IPv6 host must be written in brackets')
        host, _, port_text = authority.partition(':')
    if not host:
        raise ValueError('url has no host')
    if port_text:
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise ValueError('port must be 1-65535')
        port = int(port_text)
    else:
        port = 443 if scheme == 'https' else 80
    return UrlParts(scheme, host.lower(), port, (slash + path).rstrip('/'))


def url_problem(url: str, *, allow_insecure_loopback: bool = False) -> str | None:
    """Why ``url`` cannot be the push destination, else None: unparsable, ``http://`` to a non-loopback host,
    or ``http://`` to a loopback host without ``allow_insecure_loopback`` (``push --allow-insecure-loopback``)."""
    try:
        parts = parse_url(url)
    except ValueError as exc:
        return str(exc)
    if parts.scheme == 'http' and parts.host not in LOOPBACK_HOSTS:
        return 'http:// is allowed for a loopback test server only; use https://'
    if parts.scheme == 'http' and not allow_insecure_loopback:
        return 'http:// to a loopback server needs --allow-insecure-loopback'
    return None


def endpoint(url: str, name: str, query: Mapping[str, str | int] | None = None) -> str:
    """The URL of push endpoint ``name`` (``ENDPOINTS``) under the configured server ``url``; query values
    are ids, hashes or flags (``[A-Za-z0-9_.-]``), so no encoding is needed. The destination is always built
    here from the configured URL (the push module holds no URL literal)."""
    parse_url(url)
    if name not in ENDPOINTS:
        raise ValueError(f'unknown endpoint {name!r}')
    result = f"{url.rstrip('/')}/{ENDPOINTS[name]}"
    if query:
        pairs = []
        for key, value in query.items():
            if not _QUERY_VALUE.fullmatch(key) or not _QUERY_VALUE.fullmatch(str(value)):
                raise ValueError(f'query {key!r} needs [A-Za-z0-9_.-] only')
            pairs.append(f'{key}={value}')
        result += '?' + '&'.join(pairs)
    return result


def is_sha256(value) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def windows_normcase(path: str) -> str:
    """``ntpath.normcase`` on any OS: ``/`` -> ``\\`` and lower case. The server (Linux) compares collector
    paths (Windows) with this, so a ``path_prefix`` filter behaves as on the collector (risk 15)."""
    return path.replace('/', '\\').lower()

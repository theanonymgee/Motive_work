"""Operating server: apply one push batch to the store and answer the collector's state requests (design M5
remote §3.2, §3.3, appendix A; review M5 remote v1 M-1, M-2, L1, L3).

Pure functions over a SQLite connection the caller owns (the server's ``push_db``, autocommit): a parsed body
in, the answer envelope out (``sync.ok`` / ``sync.error``). No starlette, no network, no file access;
``remote.py`` checks the token and the request size and calls these (or ``dispatch``).

Rules: one batch is one ``BEGIN IMMEDIATE`` transaction and nothing is written for a refused batch (a
document that breaks a rule is refused alone, before anything of it is written); a ``batch_id`` seen before
answers its stored response and applies nothing; ids are the server's own; unit sets are checked against their
hashes; chunks and their FTS rows are built here with the same ``CHUNK_VERSION`` (the collector never sends
chunk text) and only for eligible versions, as ``index`` does on the collector; vectors are content addressed
``(model_key, text_sha256)`` and checked (dimension, finite, unit norm); a deleted document's versions and units
are kept. Messages name counts, ids and hashes, never unit text.

Identity (M-1): a document is the collector's file by its ``path_key`` under a root that collector owns; a
version is matched within its document by content (``sha256``, a locked placeholder, the supersede state) in
the same order, never by the collector's rowids. The id maps (``collector_documents``, ``collector_versions``,
``collector_events``) follow the collector's current rowids and are re-pointed when they change, so a rebuilt or
restored collector store converges with the server's history kept (the server numbers its own versions; a
server version the collector no longer has is superseded, never deleted). A root equal to, inside or around
another live collector's root is refused (compared with ``sync.windows_normcase``); a root of a retired
collector is taken over by the collector that pushes it next (``collectors retire`` is the explicit step).

Size (M-2): a version over ``sync.VERSION_MAX_UNITS``/``VERSION_MAX_CHARS`` is refused (``version_too_large``).
A split version is applied part by part, one short transaction per part: its units are inserted as they come
(a unit that continues into the next part waits in ``push_parts``), and its chunks and FTS rows are built in
unit order by a resumable chunk stream (``ChunkStream``, the rules of ``chunks.build_chunks``) whose small state
is kept between parts. The whole-version checks (count, characters, ``units_sha256``) run at the last part on
the stored rows without loading the text; a version that fails them, or that a push leaves unfinished (``end``,
the next ``begin``), loses its partial units and chunks.

Review v2: a push that sent every changed document says so at ``end`` (``complete``); the server then marks
deleted every active document under that push's roots that the collector does not map (N-1: a file deleted
before its store was rebuilt, whose old id another file now has; L-e: a retired collector's file the successor
does not have). A cut push (``--limit-docs``, ``--max-mb``) sweeps and forgets nothing (L-a). A unit is at most
``sync.UNIT_MAX_CHARS`` (the collector's ``max_unit_chars``); a long one arrives one piece per batch, is chunked
as it grows (``long_unit_pieces``), hashed piece by piece and stored once, by SQLite, when it ends (N-2).

Push-run working state (the model key, the collector's roots of the running push) lives in ``pushes.note`` as JSON.
"""
from __future__ import annotations

import hashlib
import json

from . import chunks, index, locator, store, sync

# The collector a push token belongs to (``remote.lookup_token`` -> ``collectors`` row).
COLLECTOR_KEYS = ('id', 'name', 'retired_at')
VERSION_COLUMNS = ('seq', 'sha256', 'size', 'mtime_ns', 'observed_at', 'superseded_at', 'parser', 'parser_version',
                   'encoding', 'parse_status', 'parse_error', 'unit_count', 'text_chars')
# Columns a matched version may change (the v1 trigger's rule): mtime_ns and the parser columns always,
# superseded_at NULL -> value once, sha256/size of a 'locked' placeholder. seq and observed_at stay the server's.
MUTABLE_VERSION_COLUMNS = ('mtime_ns', 'parser', 'parser_version', 'encoding', 'parse_status', 'parse_error',
                           'unit_count', 'text_chars')
DOCUMENT_STATUSES = ('active', 'deleted', 'skipped')
PARSE_STATUSES = ('ok', 'failed', 'locked', 'unsupported', 'too_large', 'encrypted', 'pending')
COPY_CANDIDATES = 5
LOCKED_SHA256 = '0' * 64
CHUNK_PAGE_UNITS = 500             # units read per step when chunks are built from stored units
PROGRESS_PART = 0                  # push_parts row holding a split version's state


def _now(now: str | None) -> str:
    return now or store.now()


def _need(item, keys, where: str) -> None:
    if not isinstance(item, dict):
        raise sync.SyncError('bad_batch', f'{where} is not an object')
    missing = [key for key in keys if key not in item]
    if missing:
        raise sync.SyncError('bad_batch', f'{where} lacks {missing[0]!r}')


def _int(value, where: str, *, low: int = 0) -> int:
    if type(value) is not int or not low <= value <= sync.INT_MAX:
        raise sync.SyncError('bad_batch', f'{where} is not an integer in {low}..2**63-1')
    return value


def _text(value, where: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise sync.SyncError('bad_batch', f'{where} is not a string')
    return value


def _sha(value, where: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not sync.is_sha256(value):
        raise sync.SyncError('bad_batch', f'{where} is not a SHA-256 hex digest')
    return value


def _key(local_id) -> str:
    return str(local_id)


def unit_json(unit: dict) -> str:
    """``parsers.unit_json`` (this module may not import the parsers; a test keeps the two equal)."""
    return json.dumps(unit, sort_keys=True, ensure_ascii=False)


def _check_locator(locator_json: str, kind: str, where: str) -> dict:
    """The unit locator of a wire unit: valid JSON in its stored form, a known shape, the unit's own kind."""
    try:
        unit = locator.check_unit(json.loads(locator_json))
    except (ValueError, RecursionError):            # LocatorError and JSONDecodeError are ValueErrors
        raise sync.SyncError('bad_batch', f'{where}.locator_json is not a unit locator') from None
    if unit['kind'] != kind or unit_json(unit) != locator_json:
        raise sync.SyncError('bad_batch', f'{where}.locator_json is not the stored form of a {kind!r} locator')
    return unit


class _Discard(sync.SyncError):
    """A refusal after which a split version's partial units and chunks are removed (in their own transaction,
    once the refused batch is rolled back)."""

    def __init__(self, code: str, message: str, cleanup):
        super().__init__(code, message)
        self.cleanup = cleanup


# ---- chunks ------------------------------------------------------------------------------------------------

class ChunkStream:
    """``chunks.build_chunks`` fed one unit at a time and resumable: ``state`` (the units carried into the next
    chunk, at most about ``CHUNK_MAX`` characters, the last group key and the next chunk ordinal) is plain JSON,
    so a split version's chunks are built part by part across batches. ``feed``/``finish`` return the chunks
    completed so far; together they equal ``build_chunks`` of all the units."""

    def __init__(self, state: dict | None = None):
        self.state = state or {'current': [], 'key': None, 'next': 1}

    def _flush(self) -> list[chunks.ChunkSpec]:
        current = self.state['current']
        if not current:
            return []
        joined = '\n'.join(text for _, text in current)
        spec = chunks.ChunkSpec(self.state['next'], current[0][0], current[-1][0], 0, len(joined), joined)
        self.state['next'] += 1
        return [spec]

    def feed(self, ordinal: int, unit: dict, text: str) -> list[chunks.ChunkSpec]:
        state, out = self.state, []
        key = list(chunks.group_key(unit))
        if len(text) > chunks.CHUNK_MAX:
            out += self._flush()
            state['current'] = []
            pieces = chunks.split_large_unit(ordinal, text, state['next'])
            state['next'] += len(pieces)
            state['key'] = key
            return out + pieces
        current = [tuple(pair) for pair in state['current']]
        if current and (key != state['key'] or chunks._joined_length(current) + 1 + len(text) > chunks.CHUNK_TARGET):
            out += self._flush()
            current = chunks.carry(current) if key == state['key'] else []
            while current and chunks._joined_length(current) + 1 + len(text) > chunks.CHUNK_MAX:
                current = current[1:]
        current.append((ordinal, text))
        state['current'] = [list(pair) for pair in current]
        state['key'] = key
        return out

    def finish(self) -> list[chunks.ChunkSpec]:
        out = self._flush()
        self.state['current'] = []
        return out


def build_stored_chunks(db, version_id: int) -> int:
    """The chunks of one version from its stored units, read ``CHUNK_PAGE_UNITS`` at a time (never the whole
    version in memory), inserted as they complete. Returns the number of chunk rows."""
    stream, stored, last, expected = ChunkStream(), 0, 0, 1
    while True:
        page = db.execute('SELECT ordinal, locator_json, text FROM units WHERE version_id = ? AND ordinal > ? '
                          'ORDER BY ordinal LIMIT ?', (version_id, last, CHUNK_PAGE_UNITS)).fetchall()
        if not page:
            break
        specs = []
        for ordinal, unit_text_json, text in page:
            if ordinal != expected:
                raise RuntimeError('units are not numbered 1..n')
            expected += 1
            specs += stream.feed(ordinal, json.loads(unit_text_json), text)
        stored += store.insert_chunks(db, version_id, chunks.CHUNK_VERSION, specs, append=True) if specs else 0
        last = page[-1][0]
    tail = stream.finish()
    return stored + (store.insert_chunks(db, version_id, chunks.CHUNK_VERSION, tail, append=True) if tail else 0)


def _chunk_version(db, version_id: int, sha256: str, parser_version: str | None) -> int:
    """Chunks of one version: copied in SQL from a version of the same content and parser version that has
    them (as ``index._chunk_version``), else built from the stored units."""
    source = db.execute(
        'SELECT c.version_id FROM chunks c JOIN versions s ON s.id = c.version_id '
        'WHERE s.sha256 = ? AND s.parser_version IS ? AND c.chunk_version = ? AND c.version_id <> ? LIMIT 1',
        (sha256, parser_version, chunks.CHUNK_VERSION, version_id)).fetchone()
    if source is not None:
        return store.insert_chunks(db, version_id, chunks.CHUNK_VERSION, copy_from=source[0])
    return build_stored_chunks(db, version_id)


def _in_progress(db, version_id: int) -> bool:
    return db.execute('SELECT 1 FROM push_parts p JOIN pushes s ON s.id = p.push_id JOIN collector_versions m ON '
                      'm.collector_id = s.collector_id AND m.local_version_id = p.local_version_id '
                      'WHERE m.version_id = ? AND p.part = ? LIMIT 1', (version_id, PROGRESS_PART)).fetchone() is not None


def refresh_chunks(db, document_id: int) -> int:
    """The chunks of one document as ``index`` leaves them on the collector: built (or copied) under
    ``CHUNK_VERSION`` for an eligible version that holds all its units, dropped for every other version (a split
    version being applied keeps the chunks it has built so far). Returns the number of chunk rows stored."""
    stored = 0
    eligible = {row[0] for row in db.execute(f'SELECT v.id FROM {store.eligible_versions_sql()} AND d.id = ?',
                                             (document_id,))}
    for version_id, sha256, parser_version, unit_count in db.execute(
            'SELECT id, sha256, parser_version, unit_count FROM versions WHERE document_id = ? ORDER BY seq',
            (document_id,)).fetchall():
        if version_id in eligible and _in_progress(db, version_id):
            continue
        held = db.execute('SELECT COUNT(*) FROM units WHERE version_id = ?', (version_id,)).fetchone()[0]
        if version_id not in eligible or held != unit_count:
            store.drop_chunks(db, version_id)
            continue
        store.drop_chunks(db, version_id, keep=chunks.CHUNK_VERSION)
        if db.execute('SELECT 1 FROM chunks WHERE version_id = ? AND chunk_version = ? LIMIT 1',
                      (version_id, chunks.CHUNK_VERSION)).fetchone() is None:
            stored += _chunk_version(db, version_id, sha256, parser_version)
    return stored


# ---- state requests --------------------------------------------------------------------------------------

def _check_collector(collector: dict) -> None:
    if collector.get('retired_at'):
        raise sync.SyncError('collector_retired', f'collector {collector["name"]} is retired')


def state(db, collector: dict, *, model_key: str | None) -> dict:
    """``GET push/state[?model_key=K]``: ``sync.STATE_KEYS`` for this collector (its summary hashes from
    ``sync.summary`` for the collector's model key, the server's schema and chunk version, the model keys it
    holds vectors for, the hash and highest local id of the collector's events it holds)."""
    _check_collector(collector)
    _sha(model_key, 'model_key', optional=True)
    hashes = sync.summary(db, model_key, collector_id=collector['id'])
    keys = [row[0] for row in db.execute('SELECT DISTINCT model_key FROM chunk_vectors ORDER BY 1')]
    return {'collector': collector['name'], 'schema_version': store.SCHEMA_VERSION,
            'chunk_version': chunks.CHUNK_VERSION, 'model_keys': keys, **hashes,
            'events_sha256': sync.events_sha256(db, collector_id=collector['id']),
            'events_max': sync.events_max(db, collector['id'])}


def state_page(db, collector: dict, *, after: int = 0, limit: int = sync.STATE_PAGE_DOCUMENTS) -> dict:
    """``GET push/state?full=1&after=N``: ``sync.STATE_PAGE_KEYS`` (``documents``: ``sync.document_summaries``
    of this collector after ``N``; ``next_after``: the last id, or None at the end)."""
    _check_collector(collector)
    limit = max(1, min(int(limit), sync.STATE_PAGE_DOCUMENTS))
    rows = sync.document_summaries(db, collector_id=collector['id'], after=int(after), limit=limit)
    return {'documents': rows, 'next_after': rows[-1]['local_document_id'] if len(rows) == limit else None}


def _missing_sql(collector_id: int) -> tuple[str, list]:
    return (f'SELECT DISTINCT c.text_sha256 FROM {store.latest_chunks_sql()} AND c.chunk_version = ? '
            'AND c.version_id IN (SELECT version_id FROM collector_versions WHERE collector_id = ?) '
            'AND NOT EXISTS(SELECT 1 FROM chunk_vectors x WHERE x.model_key = ? AND x.text_sha256 = c.text_sha256)',
            [chunks.CHUNK_VERSION, collector_id])


def vectors_page(db, collector: dict, *, model_key: str, after: str = '',
                 limit: int = sync.STATE_PAGE_VECTORS) -> dict:
    """``GET push/state/vectors?model_key=K&after=SHA``: ``sync.VECTORS_PAGE_KEYS`` (``missing``: sorted
    ``text_sha256`` of this collector's target chunks without a vector for ``model_key``)."""
    _check_collector(collector)
    _sha(model_key, 'model_key')
    limit = max(1, min(int(limit), sync.STATE_PAGE_VECTORS))
    sql, args = _missing_sql(collector['id'])
    rows = [row[0] for row in db.execute(sql + ' AND c.text_sha256 > ? ORDER BY 1 LIMIT ?',
                                         [*args, model_key, after or '', limit])]
    return {'missing': rows, 'next_after': rows[-1] if len(rows) == limit else None}


# ---- batches ---------------------------------------------------------------------------------------------

def apply(db, collector: dict, body: dict, *, raw_bytes: int, now: str | None = None) -> dict:
    """Apply one checked-size, decompressed batch ``body`` of ``collector`` (``COLLECTOR_KEYS``) and return
    ``sync.ok(result)`` with ``sync.RESULT_KEYS[kind]`` or ``sync.error`` (``sync.ERROR_CODES``). A malformed
    body (deep nesting, huge integers, a bad locator) is ``bad_batch``, never an exception.
    ``raw_bytes`` is the request body size for ``pushes.bytes_in``."""
    try:
        _check_collector(collector)
        sync.check_body(body)
        if body['collector'] != collector['name']:
            raise sync.SyncError('bad_batch', 'the batch names another collector than its token')
    except sync.SyncError as exc:
        return sync.error(exc.code, exc.message)
    row = db.execute('SELECT b.response_json FROM push_batches b JOIN pushes p ON p.id = b.push_id '
                     'WHERE b.batch_id = ? AND p.collector_id = ?', (body['batch_id'], collector['id'])).fetchone()
    if row is not None:
        return json.loads(row[0])                       # seen before: the stored answer, nothing applied
    try:
        store.begin_immediate(db)
    except store.Busy:
        return sync.error('store_busy', 'the store is busy; retry')
    try:
        at = _now(now)
        if body['kind'] == 'begin':
            if body['push_id'] is not None:
                raise sync.SyncError('bad_batch', 'begin has no push_id')
            result = _begin(db, collector, body, at)
            push = {'id': result['push_id']}
        else:
            push = _running_push(db, collector, body['push_id'])
            result = HANDLERS[body['kind']](db, collector, push, body['items'], at)
        answer = sync.ok(result)
        db.execute('INSERT INTO push_batches(batch_id, push_id, kind, applied_at, bytes_in, response_json) '
                   'VALUES(?,?,?,?,?,?)', (body['batch_id'], push['id'], body['kind'], at, int(raw_bytes),
                                           json.dumps(answer, ensure_ascii=False, sort_keys=True)))
        db.execute('UPDATE pushes SET batches = batches + 1, bytes_in = bytes_in + ? WHERE id = ?',
                   (int(raw_bytes), push['id']))
        store.commit(db)
    except sync.SyncError as exc:
        db.execute('ROLLBACK')
        if isinstance(exc, _Discard):
            _run_cleanup(db, exc.cleanup)
        return sync.error(exc.code, exc.message)
    except store.Busy:
        return sync.error('store_busy', 'the store is busy; retry')
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        if db.in_transaction:
            db.execute('ROLLBACK')
        return sync.error('bad_batch', f'malformed batch ({type(exc).__name__})')
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    return answer


def _run_cleanup(db, cleanup) -> None:
    try:
        store.begin_immediate(db)
    except store.Busy:
        return                                          # the next begin or end removes it
    try:
        cleanup(db)
        store.commit(db)
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise


def _running_push(db, collector: dict, push_id) -> dict:
    if type(push_id) is not int:
        raise sync.SyncError('push_not_running', 'no push_id')
    row = db.execute("SELECT id, scan_id, note FROM pushes WHERE id = ? AND collector_id = ? AND status = 'running'",
                     (push_id, collector['id'])).fetchone()
    if row is None:
        raise sync.SyncError('push_not_running', f'push {push_id} is not running for this collector')
    work = json.loads(row[2] or '{}')
    return {'id': row[0], 'scan_id': row[1], 'model_key': work.get('model_key'), 'roots': work.get('roots', {}),
            'root_keys': work.get('root_keys', {})}


def _discard_parts(db, push_id: int) -> int:
    """Remove the unfinished split versions of one push: their partial units and chunks, and their parts."""
    rows = db.execute('SELECT p.local_version_id, m.version_id FROM push_parts p JOIN pushes s ON s.id = p.push_id '
                      'LEFT JOIN collector_versions m ON m.collector_id = s.collector_id AND '
                      'm.local_version_id = p.local_version_id WHERE p.push_id = ? AND p.part = ?',
                      (push_id, PROGRESS_PART)).fetchall()
    for _, version_id in rows:
        if version_id is not None:
            _drop_units(db, version_id)
            db.execute('UPDATE collector_versions SET units_sha256 = NULL WHERE version_id = ?', (version_id,))
    db.execute('DELETE FROM push_parts WHERE push_id = ?', (push_id,))
    return len(rows)


def _roots(db) -> list[dict]:
    return [{'id': row[0], 'path': row[1], 'key': sync.windows_normcase(row[1]), 'collector_id': row[2],
             'retired': row[3] is not None}
            for row in db.execute('SELECT r.id, r.path, r.collector_id, c.retired_at FROM source_roots r '
                                  'LEFT JOIN collectors c ON c.id = r.collector_id ORDER BY r.id')]


def _nested(first: str, second: str) -> bool:
    """One normalised path is the other or lies inside it."""
    a, b = first.rstrip('\\'), second.rstrip('\\')
    return a == b or a.startswith(b + '\\') or b.startswith(a + '\\')


def _begin(db, collector: dict, body: dict, at: str) -> dict:
    if len(body['items']) != 1:
        raise sync.SyncError('bad_batch', 'begin has exactly one item')
    item = body['items'][0]
    _need(item, ('roots', 'started_at'), 'begin item')
    model_key = _sha(item.get('model_key'), 'begin model_key', optional=True)
    _text(item['started_at'], 'begin started_at')
    _text(item.get('nonce'), 'begin nonce', optional=True)
    if not isinstance(item['roots'], list):
        raise sync.SyncError('bad_batch', 'begin roots is not a list')
    stale = [row[0] for row in db.execute("SELECT id FROM pushes WHERE collector_id = ? AND status = 'running'",
                                          (collector['id'],))]
    for push_id in stale:      # a push that never ended (collect.scan closes a stale running scan the same way)
        _discard_parts(db, push_id)
        db.execute("UPDATE pushes SET status = 'aborted', finished_at = ? WHERE id = ?", (at, push_id))
        db.execute("UPDATE scans SET status = 'aborted', finished_at = ?, note = 'push left running' "
                   "WHERE id = (SELECT scan_id FROM pushes WHERE id = ?) AND status = 'running'", (at, push_id))
    roots, keys, known = {}, {}, _roots(db)
    for number, root in enumerate(item['roots']):
        where = f'roots[{number}]'
        _need(root, ('local_root_id', 'path', 'enabled'), where)
        local = _int(root['local_root_id'], f'{where}.local_root_id', low=1)
        path = _text(root['path'], f'{where}.path')
        key = sync.windows_normcase(path)
        if not key.strip('\\'):
            raise sync.SyncError('bad_batch', f'{where}.path is empty')
        enabled = 1 if root['enabled'] else 0
        if any(r['collector_id'] != collector['id'] and (r['collector_id'] is None or not r['retired'])
               and _nested(r['key'], key) for r in known):
            raise sync.SyncError('root_owned_by_other_collector', f'root {local} is, holds or lies inside a root '
                                                                  'of another collector')
        same = next((r for r in known if r['key'] == key), None)
        if same is None:
            server_id = db.execute('INSERT INTO source_roots(path, enabled, added_at, collector_id) VALUES(?,?,?,?)',
                                   (path, enabled, at, collector['id'])).lastrowid
            known.append({'id': server_id, 'path': path, 'key': key, 'collector_id': collector['id'],
                          'retired': False})
        else:                                       # its own root, or a retired collector's root taken over
            server_id = same['id']
            db.execute('UPDATE source_roots SET enabled = ?, collector_id = ? WHERE id = ?',
                       (enabled, collector['id'], server_id))
            same['collector_id'], same['retired'] = collector['id'], False
        roots[_key(local)] = server_id
        keys[_key(local)] = key
    if item.get('events_reset'):                    # the collector's event history differs: map it again
        db.execute('DELETE FROM collector_events WHERE collector_id = ?', (collector['id'],))
    scan_id = db.execute("INSERT INTO scans(started_at, status, partial, roots_json, note) VALUES(?, 'running', 1, ?, ?)",
                         (at, json.dumps([known_root['path'] for known_root in known if known_root['id'] in
                                          roots.values()], ensure_ascii=False), f'push:{collector["name"]}')).lastrowid
    push_id = db.execute("INSERT INTO pushes(collector_id, scan_id, started_at, status, client_version, note) "
                         "VALUES(?, ?, ?, 'running', ?, ?)",
                         (collector['id'], scan_id, at, body['client_version'],
                          json.dumps({'model_key': model_key, 'roots': roots, 'root_keys': keys},
                                     sort_keys=True))).lastrowid
    db.execute('UPDATE collectors SET last_push_at = ? WHERE id = ?', (at, collector['id']))
    return {'push_id': push_id, 'root_ids': roots}


# -- documents -----------------------------------------------------------------------------------------------

def _version_fields(version, where: str) -> dict:
    _need(version, ('local_version_id', *VERSION_COLUMNS, 'units_sha256'), where)
    fields = {'local_version_id': _int(version['local_version_id'], f'{where}.local_version_id', low=1),
              'seq': _int(version['seq'], f'{where}.seq', low=1), 'sha256': _sha(version['sha256'], f'{where}.sha256'),
              'size': _int(version['size'], f'{where}.size'), 'mtime_ns': _int(version['mtime_ns'], f'{where}.mtime_ns'),
              'observed_at': _text(version['observed_at'], f'{where}.observed_at'),
              'units_sha256': _sha(version['units_sha256'], f'{where}.units_sha256', optional=True)}
    for column in ('superseded_at', 'parser', 'parser_version', 'encoding', 'parse_error'):
        fields[column] = _text(version[column], f'{where}.{column}', optional=True)
    if version['parse_status'] not in PARSE_STATUSES:
        raise sync.SyncError('bad_batch', f'{where}.parse_status is unknown')
    fields['parse_status'] = version['parse_status']
    for column in ('unit_count', 'text_chars'):
        fields[column] = None if version[column] is None else _int(version[column], f'{where}.{column}')
    longest = version.get('unit_chars_max')                       # the longest unit (review v2 N-2)
    fields['unit_chars_max'] = None if longest is None else _int(longest, f'{where}.unit_chars_max')
    return fields


def _compatible(old: dict, new: dict) -> bool:
    """The server version ``old`` can stand for the collector's version ``new`` (the v1 guard's rule without the
    sequence and observation time, which the two sides keep apart): the same content (or a locked placeholder
    being read), not turned back into a placeholder, not un-superseded."""
    if old['sha256'] != new['sha256'] and old['parse_status'] != 'locked':
        return False
    if new['parse_status'] == 'locked' and old['parse_status'] != 'locked':
        return False
    return not (old['superseded_at'] is not None and new['superseded_at'] is None)


def _server_version(db, version_id: int) -> dict:
    row = db.execute(f'SELECT id, document_id, {", ".join(VERSION_COLUMNS)} FROM versions WHERE id = ?',
                     (version_id,)).fetchone()
    return dict(zip(('id', 'document_id', *VERSION_COLUMNS), row))


def _match_versions(db, collector_id: int, doc_id: int | None, wanted: list[dict]) -> list[int | None]:
    """The server version each of the collector's versions (in its sequence order) stands for, or None for a new
    one: its current map when that still fits, else the first fitting version of the document after the
    previous match. Matches keep the order on both sides."""
    if doc_id is None:
        return [None] * len(wanted)
    server = [_server_version(db, row[0]) for row in db.execute(
        'SELECT id FROM versions WHERE document_id = ? ORDER BY seq', (doc_id,))]
    by_id = {version['id']: version for version in server}
    mapped = dict(db.execute('SELECT m.local_version_id, m.version_id FROM collector_versions m JOIN versions v ON '
                             'v.id = m.version_id WHERE m.collector_id = ? AND v.document_id = ?', (collector_id, doc_id)))
    plan, last_seq, claimed = [], 0, set()
    for fields in wanted:
        target = by_id.get(mapped.get(fields['local_version_id']))
        if target is None or target['seq'] <= last_seq or target['id'] in claimed or not _compatible(target, fields):
            target = next((v for v in server if v['seq'] > last_seq and v['id'] not in claimed
                           and _compatible(v, fields)), None)
        if target is None:
            plan.append(None)
            last_seq = 1 << 62                  # every later version is new as well
        else:
            plan.append(target['id'])
            claimed.add(target['id'])
            last_seq = target['seq']
    return plan


def _drop_units(db, version_id: int) -> None:
    store.drop_chunks(db, version_id)
    db.execute('DELETE FROM units WHERE version_id = ?', (version_id,))


def _copy_source(db, version_id: int, fields: dict) -> int | None:
    """Another server version of the same content and parser version holding exactly this unit set."""
    for (candidate,) in db.execute(
            "SELECT v.id FROM versions v WHERE v.sha256 = ? AND v.parser_version IS ? AND v.parse_status = 'ok' "
            'AND v.id <> ? AND EXISTS(SELECT 1 FROM units u WHERE u.version_id = v.id) ORDER BY v.id DESC LIMIT ?',
            (fields['sha256'], fields['parser_version'], version_id, COPY_CANDIDATES)).fetchall():
        if _counts_match(db, candidate, fields) and sync.version_units_sha256(db, candidate) == fields['units_sha256']:
            return candidate
    return None


def _counts_match(db, version_id: int, fields: dict) -> bool:
    """The stored units of ``version_id`` are as many and as long as the version says (review v2 L-f)."""
    count, chars = db.execute('SELECT COUNT(*), COALESCE(SUM(LENGTH(text)), 0) FROM units WHERE version_id = ?',
                              (version_id,)).fetchone()
    return count == fields['unit_count'] and chars == (fields['text_chars'] or 0)


def _units_state(db, collector_id: int, version_id: int, fields: dict) -> str:
    """``have``/``copied``/``need``/``none`` for one mirrored version (design §3.3); a ``need`` version keeps no
    stale units (it serves nothing until its units arrive), a ``none`` version keeps none at all."""
    holds = db.execute('SELECT 1 FROM units WHERE version_id = ? LIMIT 1', (version_id,)).fetchone() is not None
    if fields['parse_status'] != 'ok' or not fields['unit_count']:
        if holds:
            _drop_units(db, version_id)
        db.execute('UPDATE collector_versions SET units_sha256 = NULL WHERE collector_id = ? AND version_id = ?',
                   (collector_id, version_id))
        return 'none'
    wanted = fields['units_sha256']
    if holds and wanted is not None and _counts_match(db, version_id, fields) and \
            sync.version_units_sha256(db, version_id) == wanted:
        state_ = 'have'
    elif wanted is not None and (source := _copy_source(db, version_id, fields)) is not None:
        if holds:
            _drop_units(db, version_id)
        db.execute('INSERT INTO units(version_id, ordinal, kind, locator_json, text, text_sha256) '
                   'SELECT ?, ordinal, kind, locator_json, text, text_sha256 FROM units WHERE version_id = ? '
                   'ORDER BY ordinal', (version_id, source))
        state_ = 'copied'
    else:
        if holds:
            _drop_units(db, version_id)
        db.execute('UPDATE collector_versions SET units_sha256 = NULL WHERE collector_id = ? AND version_id = ?',
                   (collector_id, version_id))
        return 'need'
    db.execute('UPDATE collector_versions SET units_sha256 = ? WHERE collector_id = ? AND version_id = ?',
               (wanted, collector_id, version_id))
    return state_


def _map_document(db, collector_id: int, local: int, doc_id: int) -> None:
    """``collector_documents``: this collector's ``local`` id names ``doc_id`` and nothing else does."""
    db.execute('DELETE FROM collector_documents WHERE (collector_id = ? AND local_document_id = ? AND document_id <> ?) '
               'OR (document_id = ? AND NOT (collector_id = ? AND local_document_id = ?))',
               (collector_id, local, doc_id, doc_id, collector_id, local))
    db.execute('INSERT OR IGNORE INTO collector_documents(collector_id, local_document_id, document_id) VALUES(?,?,?)',
               (collector_id, local, doc_id))


def _map_version(db, collector_id: int, local: int, version_id: int) -> None:
    db.execute('DELETE FROM collector_versions WHERE (collector_id = ? AND local_version_id = ? AND version_id <> ?) '
               'OR (version_id = ? AND NOT (collector_id = ? AND local_version_id = ?))',
               (collector_id, local, version_id, version_id, collector_id, local))
    db.execute('INSERT OR IGNORE INTO collector_versions(collector_id, local_version_id, version_id) VALUES(?,?,?)',
               (collector_id, local, version_id))


def _document_refusal(db, collector: dict, push: dict, item: dict, wanted: list[dict]) -> tuple[str | None, int | None]:
    """``(refusal code, server document id)`` of one documents item: its path under the root it names, no
    version over the size limit, and its ``path_key`` free or already this collector's (or a retired one's)."""
    root_key = push['root_keys'].get(_key(item['local_root_id']))
    path_key = item['path_key']
    if root_key is None or not sync.windows_normcase(path_key).startswith(root_key.rstrip('\\') + '\\'):
        return 'path_outside_root', None
    # the display path is the same file as the key (review v2 L-b): no other collector's path as a label
    if sync.windows_normcase(item['path']).casefold() != sync.windows_normcase(path_key).casefold():
        return 'path_outside_root', None
    if any((fields['unit_count'] or 0) > sync.VERSION_MAX_UNITS or (fields['text_chars'] or 0) > sync.VERSION_MAX_CHARS
           or (fields['unit_chars_max'] or 0) > sync.UNIT_MAX_CHARS for fields in wanted):
        return 'version_too_large', None
    found = db.execute('SELECT d.id, r.collector_id, c.retired_at FROM documents d JOIN source_roots r ON '
                       'r.id = d.root_id LEFT JOIN collectors c ON c.id = r.collector_id WHERE d.path_key = ?',
                       (path_key,)).fetchone()
    if found is None:
        return None, None
    doc_id, owner, retired = found
    if owner != collector['id'] and (owner is None or retired is None):
        return 'root_owned_by_other_collector', None
    return None, doc_id


def _documents(db, collector: dict, push: dict, items: list, at: str) -> dict:
    if len(items) > sync.DOCUMENTS_PER_BATCH:
        raise sync.SyncError('bad_batch', f'over {sync.DOCUMENTS_PER_BATCH} documents in one batch')
    documents, versions, refused, cid = {}, {}, [], collector['id']
    for number, item in enumerate(items):
        where = f'items[{number}]'
        _need(item, ('local_document_id', 'local_root_id', 'path', 'path_key', 'ext', 'status', 'skip_reason',
                     'first_seen_at', 'last_seen_at', 'deleted_at', 'versions'), where)
        local_doc = _int(item['local_document_id'], f'{where}.local_document_id', low=1)
        root_id = push['roots'].get(_key(_int(item['local_root_id'], f'{where}.local_root_id', low=1)))
        if root_id is None:
            raise sync.SyncError('bad_batch', f'{where}: root {item["local_root_id"]} was not in begin')
        if item['status'] not in DOCUMENT_STATUSES:
            raise sync.SyncError('bad_batch', f'{where}.status is unknown')
        for column in ('path', 'path_key', 'ext', 'first_seen_at', 'last_seen_at'):
            _text(item[column], f'{where}.{column}')
        for column in ('skip_reason', 'deleted_at'):
            _text(item[column], f'{where}.{column}', optional=True)
        if not isinstance(item['versions'], list):
            raise sync.SyncError('bad_batch', f'{where}.versions is not a list')
        wanted = sorted((_version_fields(v, f'{where}.versions[{n}]') for n, v in enumerate(item['versions'])),
                        key=lambda fields: fields['seq'])
        if len({fields['seq'] for fields in wanted}) != len(wanted) or \
                len({fields['local_version_id'] for fields in wanted}) != len(wanted):
            raise sync.SyncError('bad_batch', f'{where}: two versions with one seq or one id')
        code, doc_id = _document_refusal(db, collector, push, item, wanted)
        if code:
            refused.append({'local_document_id': local_doc, 'code': code})
            continue
        plan = _match_versions(db, cid, doc_id, wanted)
        if doc_id is None:
            doc_id = db.execute(
                'INSERT INTO documents(root_id, path, path_key, ext, status, skip_reason, first_seen_at, last_seen_at, '
                'deleted_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (root_id, item['path'], item['path_key'], item['ext'], item['status'], item['skip_reason'],
                 item['first_seen_at'], item['last_seen_at'], item['deleted_at'])).lastrowid
        else:
            db.execute('UPDATE documents SET root_id = ?, path = ?, ext = ?, status = ?, skip_reason = ?, '
                       'last_seen_at = ?, deleted_at = ? WHERE id = ?',
                       (root_id, item['path'], item['ext'], item['status'], item['skip_reason'], item['last_seen_at'],
                        item['deleted_at'], doc_id))
        _map_document(db, cid, local_doc, doc_id)
        documents[_key(local_doc)] = doc_id
        next_seq = db.execute('SELECT COALESCE(MAX(seq), 0) FROM versions WHERE document_id = ?', (doc_id,)).fetchone()[0]
        claimed = set()
        for fields, version_id in zip(wanted, plan):
            if version_id is None:
                next_seq += 1
                values = {**fields, 'seq': next_seq}
                version_id = db.execute(
                    f'INSERT INTO versions(document_id, {", ".join(VERSION_COLUMNS)}) '
                    f'VALUES(?{",?" * len(VERSION_COLUMNS)})',
                    (doc_id, *(values[column] for column in VERSION_COLUMNS))).lastrowid
            else:
                old = _server_version(db, version_id)
                columns = list(MUTABLE_VERSION_COLUMNS)
                if old['parse_status'] == 'locked':
                    columns += ['sha256', 'size']
                if old['superseded_at'] is None:
                    columns.append('superseded_at')
                if any(old[column] != fields[column] for column in columns):
                    db.execute(f'UPDATE versions SET {", ".join(f"{c} = ?" for c in columns)} WHERE id = ?',
                               (*(fields[column] for column in columns), version_id))
            _map_version(db, cid, fields['local_version_id'], version_id)
            claimed.add(version_id)
            versions[_key(fields['local_version_id'])] = {
                'id': version_id, 'units': _units_state(db, cid, version_id, fields)}
        # server versions the collector no longer has: history, superseded once, unmapped
        for (version_id,) in db.execute('SELECT id FROM versions WHERE document_id = ? ORDER BY seq', (doc_id,)).fetchall():
            if version_id in claimed:
                continue
            db.execute('DELETE FROM collector_versions WHERE version_id = ?', (version_id,))
            db.execute('UPDATE versions SET superseded_at = ? WHERE id = ? AND superseded_at IS NULL', (at, version_id))
        refresh_chunks(db, doc_id)
    store.bump_data_generation(db)
    return {'documents': documents, 'versions': versions, 'refused': refused}


def _forget(db, collector: dict, push: dict, items: list, at: str) -> dict:
    """Local document ids the collector no longer has (its store was rebuilt or restored): the server document
    is unmapped and, unless already deleted, marked deleted (with a ``deleted`` event); its rows stay."""
    if len(items) > sync.EVENTS_PER_BATCH:
        raise sync.SyncError('bad_batch', f'over {sync.EVENTS_PER_BATCH} documents in one batch')
    forgotten = ignored = 0
    for number, item in enumerate(items):
        _need(item, ('local_document_id',), f'items[{number}]')
        local = _int(item['local_document_id'], f'items[{number}].local_document_id', low=1)
        row = db.execute('SELECT d.id, d.status FROM collector_documents m JOIN documents d ON d.id = m.document_id '
                         'WHERE m.collector_id = ? AND m.local_document_id = ?', (collector['id'], local)).fetchone()
        if row is None:
            ignored += 1
            continue
        doc_id, status = row
        db.execute('DELETE FROM collector_versions WHERE collector_id = ? AND version_id IN '
                   '(SELECT id FROM versions WHERE document_id = ?)', (collector['id'], doc_id))
        db.execute('DELETE FROM collector_documents WHERE collector_id = ? AND local_document_id = ?',
                   (collector['id'], local))
        if status != 'deleted':
            db.execute("UPDATE documents SET status = 'deleted', deleted_at = ? WHERE id = ?", (at, doc_id))
            store.add_document_event(db, push['scan_id'], doc_id, None, 'deleted', at)
        refresh_chunks(db, doc_id)
        forgotten += 1
    if forgotten:
        store.bump_data_generation(db)
    return {'forgotten': forgotten, 'ignored': ignored}


# -- units ---------------------------------------------------------------------------------------------------

def _unit_rows(units, where: str) -> list[dict]:
    if not isinstance(units, list) or not units:
        raise sync.SyncError('bad_batch', f'{where} is not a non-empty list')
    rows = []
    for number, unit in enumerate(units):
        at = f'{where}[{number}]'
        _need(unit, ('ordinal', 'kind', 'locator_json', 'text', 'text_sha256'), at)
        row = {'ordinal': _int(unit['ordinal'], f'{at}.ordinal', low=1), 'kind': _text(unit['kind'], f'{at}.kind'),
               'locator_json': _text(unit['locator_json'], f'{at}.locator_json'),
               'text': _text(unit['text'], f'{at}.text'), 'text_sha256': _sha(unit['text_sha256'], f'{at}.text_sha256')}
        if not rows or rows[-1]['ordinal'] != row['ordinal']:
            _check_locator(row['locator_json'], row['kind'], at)
        rows.append(row)
    return rows


def _vectors_missing(db, version_id: int, model_key: str | None) -> list[str]:
    if model_key is None:
        return []
    return [row[0] for row in db.execute(
        'SELECT DISTINCT c.text_sha256 FROM chunks c WHERE c.version_id = ? AND c.chunk_version = ? AND NOT EXISTS('
        'SELECT 1 FROM chunk_vectors x WHERE x.model_key = ? AND x.text_sha256 = c.text_sha256) ORDER BY 1',
        (version_id, chunks.CHUNK_VERSION, model_key))]


def _progress(db, push_id: int, local: int) -> dict | None:
    row = db.execute('SELECT body_json FROM push_parts WHERE push_id = ? AND local_version_id = ? AND part = ?',
                     (push_id, local, PROGRESS_PART)).fetchone()
    return json.loads(row[0]) if row else None


def _save_progress(db, push_id: int, local: int, progress: dict, at: str) -> None:
    db.execute('INSERT OR REPLACE INTO push_parts(push_id, local_version_id, part, parts, body_json, received_at) '
               'VALUES(?,?,?,?,?,?)', (push_id, local, PROGRESS_PART, progress['parts'],
                                       json.dumps(progress, ensure_ascii=False), at))


def _discard_version(push_id: int, local: int, version_id: int):
    def cleanup(db):
        _drop_units(db, version_id)
        db.execute('UPDATE collector_versions SET units_sha256 = NULL WHERE version_id = ?', (version_id,))
        db.execute('DELETE FROM push_parts WHERE push_id = ? AND local_version_id = ?', (push_id, local))
    return cleanup


LONG_MARGIN = 3 * chunks.CHUNK_MAX     # atoms ending this close to the end of the text so far wait for more text


def long_unit_pieces(stream_state: dict, long: dict, ordinal: int, text: str, final: bool) -> list[chunks.ChunkSpec]:
    """The chunks of a long unit (``len > CHUNK_MAX``) that arrives in pieces, exactly as
    ``chunks.split_large_unit`` makes them over the whole text (review v2 N-2).

    ``split_large_unit`` is one forward pass over sentence atoms (``chunks._atoms``) with a small state (the
    current piece's start and its last atom). Here that pass runs over the text kept since a restart point plus
    ``text``, consumes only atoms that more text cannot change (ending more than ``LONG_MARGIN`` characters
    before the end, unless ``final``), and keeps its state in ``long`` (JSON: ``base``, ``buffer``,
    ``piece_start``, ``last``; offsets in the whole unit). The kept text restarts at the current piece's start
    when that is a word start: atoms of the text from such a point are the whole text's atoms from there."""
    base, buffer = long['base'], long['buffer'] + text
    piece_start, last = long.get('piece_start'), long.get('last')
    out = []

    def emit(start: int, end: int) -> None:
        out.append(chunks.ChunkSpec(stream_state['next'], ordinal, ordinal, start, end, buffer[start - base:end - base]))
        stream_state['next'] += 1

    for rel_start, rel_end in chunks._atoms(buffer):
        start, end = base + rel_start, base + rel_end
        if last is not None and end <= last[1]:
            continue                                # consumed before the restart point
        if not final and rel_end > len(buffer) - LONG_MARGIN:
            break                                   # may still change with more text
        if piece_start is None:
            piece_start, last = start, [start, end]
            continue
        if end - piece_start <= chunks.CHUNK_TARGET:
            last = [start, end]
            continue
        emit(piece_start, last[1])
        last_start, last_end = last
        overlap = (last_start > piece_start and last_end - last_start <= chunks.CHUNK_OVERLAP
                   and end - last_start <= chunks.CHUNK_MAX)
        piece_start = last_start if overlap else start
        last = [start, end]
    if final:
        if piece_start is not None:
            emit(piece_start, last[1])
        long.update(base=base + len(buffer), buffer='', piece_start=None, last=None)
        return out
    cut = (piece_start - base) if piece_start is not None else 0
    if cut and not chunks._is_word_start(buffer, cut):
        cut = 0                                     # not a safe restart: keep the text from the old one
    long.update(base=base + cut, buffer=buffer[cut:], piece_start=piece_start, last=last)
    return out


def _insert_joined_unit(db, push_id: int, local: int, version_id: int, pending: dict) -> None:
    """The unit whose pieces wait in ``push_parts``, stored once when it ends: SQLite joins the pieces (in part
    order) so the whole text is never a Python string, and writes the row once (not once per piece)."""
    if tuple(int(n) for n in db.execute('SELECT sqlite_version()').fetchone()[0].split('.')[:2]) >= (3, 44):
        db.execute("INSERT INTO units(version_id, ordinal, kind, locator_json, text, text_sha256) "
                   "SELECT ?, ?, ?, ?, group_concat(json_extract(body_json, '$.text'), '' ORDER BY part), ? "
                   "FROM push_parts WHERE push_id = ? AND local_version_id = ? AND part > ?",
                   (version_id, pending['ordinal'], pending['kind'], pending['locator_json'], pending['text_sha256'],
                    push_id, local, PROGRESS_PART))
    else:                                           # an older SQLite: join here
        db.execute('INSERT INTO units(version_id, ordinal, kind, locator_json, text, text_sha256) VALUES(?,?,?,?,?,?)',
                   (version_id, pending['ordinal'], pending['kind'], pending['locator_json'],
                    ''.join(_piece_texts(db, push_id, local)), pending['text_sha256']))


def _piece_texts(db, push_id: int, local: int):
    for (text,) in db.execute("SELECT json_extract(body_json, '$.text') FROM push_parts WHERE push_id = ? AND "
                              'local_version_id = ? AND part > ? ORDER BY part', (push_id, local, PROGRESS_PART)):
        yield text


def _apply_part(db, push: dict, local: int, version_id: int, fields: dict, progress: dict, rows: list[dict],
                part: int, last: bool, at: str) -> None:
    """Insert the complete units of one part and the chunks they complete (one short transaction per part).

    A unit that may continue in the next part is stored at once and grows in place (``text || piece``); its
    pieces also wait in ``push_parts`` so that its hash is checked piece by piece when it ends, and a long one
    is chunked as it grows (``long_unit_pieces``): no part ever holds a whole long unit in memory (review v2
    N-2). Checks each unit's ordinal, length (``sync.UNIT_MAX_CHARS``), hash and the version's limits."""
    cleanup = _discard_version(push['id'], local, version_id)
    joined = sync.join_pieces(rows)
    stream = ChunkStream(progress['chunks']) if progress['chunks'] is not None else None
    specs: list[chunks.ChunkSpec] = []

    def store_piece(text: str) -> None:
        db.execute('INSERT INTO push_parts(push_id, local_version_id, part, parts, body_json, received_at) '
                   'VALUES(?,?,?,?,?,?)', (push['id'], local, part, progress['parts'],
                                           json.dumps({'text': text}, ensure_ascii=False), at))

    def grow(pending: dict, text: str) -> None:
        progress['pending_chars'] += len(text)
        if progress['pending_chars'] > sync.UNIT_MAX_CHARS:
            raise _Discard('version_too_large', f'unit {pending["ordinal"]} is longer than the server takes', cleanup)
        if stream is not None and text:
            long = progress.get('long')
            if long is None and progress['pending_chars'] > chunks.CHUNK_MAX:
                so_far = ''.join(_piece_texts(db, push['id'], local))   # at most CHUNK_MAX characters so far
                specs.extend(stream._flush())               # as ChunkStream.feed does for a unit over CHUNK_MAX
                stream.state['current'] = []
                long = progress['long'] = {'base': 0, 'buffer': so_far, 'piece_start': None, 'last': None}
            if long is not None:
                specs.extend(long_unit_pieces(stream.state, long, pending['ordinal'], text, final=False))
        if text:
            store_piece(text)

    pending = progress['pending']
    if pending is not None:
        head = None
        if joined and joined[0]['ordinal'] == pending['ordinal']:
            head = joined.pop(0)
            if (head['kind'], head['locator_json'], head['text_sha256']) != \
                    (pending['kind'], pending['locator_json'], pending['text_sha256']):
                raise _Discard('bad_units_hash', f'pieces of unit {head["ordinal"]} disagree', cleanup)
            grow(pending, head['text'])
        if joined or last:                          # the waiting unit ends here: hash it piece by piece
            digest = hashlib.sha256()
            for text in _piece_texts(db, push['id'], local):
                digest.update(text.encode('utf-8'))
            if digest.hexdigest() != pending['text_sha256']:
                raise _Discard('bad_units_hash', f'unit {pending["ordinal"]}: text_sha256 is not the hash of its text',
                               cleanup)
            _insert_joined_unit(db, push['id'], local, version_id, pending)
            db.execute('DELETE FROM push_parts WHERE push_id = ? AND local_version_id = ? AND part > ?',
                       (push['id'], local, PROGRESS_PART))
            if stream is not None:
                unit = json.loads(pending['locator_json'])
                if progress.get('long') is not None:
                    specs += long_unit_pieces(stream.state, progress['long'], pending['ordinal'], '', final=True)
                    stream.state['key'] = list(chunks.group_key(unit))
                else:                               # it stayed short: chunk it as any other unit
                    text = db.execute('SELECT text FROM units WHERE version_id = ? AND ordinal = ?',
                                      (version_id, pending['ordinal'])).fetchone()[0]
                    specs += stream.feed(pending['ordinal'], unit, text)
            progress['next_ordinal'] += 1
            progress['units'] += 1
            progress['chars'] += progress['pending_chars']
            progress.update(pending=None, pending_chars=0, long=None)
    tail = joined.pop() if joined and not last else None   # the part's last unit may continue in the next part
    rows_out = []
    for unit in joined:
        if len(unit['text']) > sync.UNIT_MAX_CHARS:
            raise _Discard('version_too_large', f'unit {unit["ordinal"]} is longer than the server takes', cleanup)
        if unit['ordinal'] != progress['next_ordinal']:
            raise _Discard('bad_units_hash', f'unit {unit["ordinal"]} where {progress["next_ordinal"]} was due', cleanup)
        if hashlib.sha256(unit['text'].encode('utf-8')).hexdigest() != unit['text_sha256']:
            raise _Discard('bad_units_hash', f'unit {unit["ordinal"]}: text_sha256 is not the hash of its text', cleanup)
        progress['next_ordinal'] += 1
        progress['units'] += 1
        progress['chars'] += len(unit['text'])
        rows_out.append((version_id, unit['ordinal'], unit['kind'], unit['locator_json'], unit['text'],
                         unit['text_sha256']))
        if stream is not None:
            specs += stream.feed(unit['ordinal'], json.loads(unit['locator_json']), unit['text'])
    if rows_out:
        db.executemany('INSERT INTO units(version_id, ordinal, kind, locator_json, text, text_sha256) '
                       'VALUES(?,?,?,?,?,?)', rows_out)
    if tail is not None:
        if tail['ordinal'] != progress['next_ordinal']:
            raise _Discard('bad_units_hash', f'unit {tail["ordinal"]} where {progress["next_ordinal"]} was due', cleanup)
        progress['pending'] = {key: tail[key] for key in ('ordinal', 'kind', 'locator_json', 'text_sha256')}
        progress.update(pending_chars=0, long=None)
        grow(progress['pending'], tail['text'])
    if progress['units'] + (progress['pending'] is not None) > (fields['unit_count'] or 0) or \
            progress['chars'] + progress.get('pending_chars', 0) > (fields['text_chars'] or 0):
        raise _Discard('bad_units_hash', 'more units or characters than the version has', cleanup)
    if stream is not None:
        if last:
            specs += stream.finish()
        if specs:
            store.insert_chunks(db, version_id, chunks.CHUNK_VERSION, specs, append=True)
        progress['chunks'] = stream.state


def _units(db, collector: dict, push: dict, items: list, at: str) -> dict:
    results, missing = [], []
    if sum(len(item.get('units') or ()) for item in items if isinstance(item, dict)) > sync.UNITS_PER_BATCH:
        raise sync.SyncError('bad_batch', f'over {sync.UNITS_PER_BATCH} units in one batch')
    for number, item in enumerate(items):
        where = f'items[{number}]'
        _need(item, ('local_version_id', 'units_sha256', 'part', 'parts', 'units'), where)
        local = _int(item['local_version_id'], f'{where}.local_version_id', low=1)
        units_sha256 = _sha(item['units_sha256'], f'{where}.units_sha256')
        parts = _int(item['parts'], f'{where}.parts', low=1)
        part = _int(item['part'], f'{where}.part', low=1)
        if part > parts or parts > sync.MAX_PARTS:
            raise sync.SyncError('bad_batch', f'{where}: part {part} of {parts} (at most {sync.MAX_PARTS})')
        rows = _unit_rows(item['units'], f'{where}.units')
        if sum(len(row['text']) for row in rows) > sync.UNIT_CHARS_PER_BATCH:
            raise sync.SyncError('bad_batch', f'{where}: over {sync.UNIT_CHARS_PER_BATCH} characters in one part')
        found = db.execute('SELECT m.version_id, m.units_sha256 FROM collector_versions m JOIN versions v ON '
                           "v.id = m.version_id WHERE m.collector_id = ? AND m.local_version_id = ? "
                           "AND v.parse_status = 'ok' AND v.unit_count > 0", (collector['id'], local)).fetchone()
        if found is None:
            raise sync.SyncError('version_not_awaiting_units', f'version {local} is not a mirrored version with units')
        version_id, held = found
        fields = _server_version(db, version_id)
        if (fields['unit_count'] or 0) > sync.VERSION_MAX_UNITS or (fields['text_chars'] or 0) > sync.VERSION_MAX_CHARS:
            raise sync.SyncError('version_too_large', f'version {local} is over the server limit')
        progress = _progress(db, push['id'], local)
        if part == 1:
            if parts == 1 and held == units_sha256 and sync.version_units_sha256(db, version_id) == units_sha256:
                count = db.execute('SELECT COUNT(*) FROM chunks WHERE version_id = ? AND chunk_version = ?',
                                   (version_id, chunks.CHUNK_VERSION)).fetchone()[0]
                results.append({'local_version_id': local, 'version_id': version_id, 'units': fields['unit_count'],
                                'chunks': count})
                continue                            # already here: nothing to do
            if progress is None and db.execute('SELECT 1 FROM push_parts WHERE push_id = ? AND part = ? LIMIT 1',
                                               (push['id'], PROGRESS_PART)).fetchone():
                raise sync.SyncError('bad_batch', 'another split version of this push is not finished')
            db.execute('DELETE FROM push_parts WHERE push_id = ? AND local_version_id = ?', (push['id'], local))
            _drop_units(db, version_id)
            db.execute('UPDATE collector_versions SET units_sha256 = NULL WHERE version_id = ?', (version_id,))
            eligible = db.execute(f'SELECT 1 FROM {store.eligible_versions_sql()} AND v.id = ?',
                                  (version_id,)).fetchone() is not None
            progress = {'part': 0, 'parts': parts, 'units_sha256': units_sha256, 'next_ordinal': 1, 'units': 0,
                        'chars': 0, 'pending': None, 'pending_chars': 0, 'long': None,
                        'chunks': ChunkStream().state if eligible else None}
        elif progress is None or (progress['part'], progress['parts'], progress['units_sha256']) != \
                (part - 1, parts, units_sha256):
            raise sync.SyncError('bad_batch', f'version {local}: part {part} of {parts} out of order')
        _apply_part(db, push, local, version_id, fields, progress, rows, part, part == parts, at)
        progress['part'] = part
        if part < parts:
            _save_progress(db, push['id'], local, progress, at)
            results.append({'local_version_id': local, 'received': part, 'of': parts})
            continue
        cleanup = _discard_version(push['id'], local, version_id)
        if progress['units'] != fields['unit_count'] or progress['chars'] != (fields['text_chars'] or 0):
            raise _Discard('bad_units_hash', 'unit or character count differs from the version', cleanup)
        if sync.version_units_sha256(db, version_id) != units_sha256:
            raise _Discard('bad_units_hash', 'units_sha256 is not the hash of the units', cleanup)
        db.execute('DELETE FROM push_parts WHERE push_id = ? AND local_version_id = ?', (push['id'], local))
        db.execute('UPDATE collector_versions SET units_sha256 = ? WHERE collector_id = ? AND version_id = ?',
                   (units_sha256, collector['id'], version_id))
        count = db.execute('SELECT COUNT(*) FROM chunks WHERE version_id = ? AND chunk_version = ?',
                           (version_id, chunks.CHUNK_VERSION)).fetchone()[0]
        results.append({'local_version_id': local, 'version_id': version_id, 'units': progress['units'],
                        'chunks': count})
        missing += _vectors_missing(db, version_id, push['model_key'])
    return {'versions': results, 'vectors_missing': sorted(set(missing))}


# -- vectors, events, end ------------------------------------------------------------------------------------

def _vectors(db, collector: dict, push: dict, items: list, at: str) -> dict:
    stored = ignored = invalid = 0
    for number, item in enumerate(items):
        where = f'items[{number}]'
        _need(item, ('model_key', 'dims', 'items'), where)
        model_key = _sha(item['model_key'], f'{where}.model_key')
        if item['dims'] != sync.VECTOR_DIMS:
            raise sync.SyncError('invalid_vector', f'{where}: dims {item["dims"]!r}, expected {sync.VECTOR_DIMS}')
        if not isinstance(item['items'], list) or len(item['items']) > sync.VECTORS_PER_BATCH:
            raise sync.SyncError('bad_batch', f'{where}: items is not a list of at most {sync.VECTORS_PER_BATCH}')
        rows = []
        for entry in item['items']:
            sha = entry.get('text_sha256') if isinstance(entry, dict) else None
            try:
                blob = sync.vector_from_b64(entry.get('vector_b64') if isinstance(entry, dict) else None)
            except sync.SyncError:
                blob = None
            if not sync.is_sha256(sha) or blob is None or index.from_blob(blob, sync.VECTOR_DIMS) is None:
                invalid += 1
                continue
            used = db.execute('SELECT 1 FROM chunks WHERE text_sha256 = ? LIMIT 1', (sha,)).fetchone()
            if used is None:
                ignored += 1                         # no chunk here has this text: nothing to attach it to
                continue
            rows.append((model_key, sha, sync.VECTOR_DIMS, blob, at))
        for row in rows:
            if db.execute('INSERT OR IGNORE INTO chunk_vectors(model_key, text_sha256, dims, vector, embedded_at) '
                          'VALUES(?,?,?,?,?)', row).rowcount:
                stored += 1
            else:
                ignored += 1
    if stored:
        store.meta_set(db, 'index_generation', str(int(store.meta_get(db, 'index_generation', '0') or 0) + 1))
    return {'stored': stored, 'ignored': ignored, 'invalid': invalid}


def _events(db, collector: dict, push: dict, items: list, at: str) -> dict:
    """The collector's events by its own event id (``collector_events``): an event the server already has (same
    document, version, kind and time) is mapped, not copied; a local id that now names another event is
    re-pointed."""
    if len(items) > sync.EVENTS_PER_BATCH:
        raise sync.SyncError('bad_batch', f'over {sync.EVENTS_PER_BATCH} events in one batch')
    stored = ignored = 0
    cid = collector['id']
    for number, item in enumerate(items):
        where = f'items[{number}]'
        _need(item, ('local_event_id', 'local_document_id', 'local_version_id', 'kind', 'at'), where)
        if item['kind'] not in store.EVENT_KINDS:
            raise sync.SyncError('bad_batch', f'{where}.kind is unknown')
        local_event = _int(item['local_event_id'], f'{where}.local_event_id', low=1)
        when = _text(item['at'], f'{where}.at')
        doc = db.execute('SELECT document_id FROM collector_documents WHERE collector_id = ? AND local_document_id = ?',
                         (cid, _int(item['local_document_id'], f'{where}.local_document_id', low=1))).fetchone()
        version = None
        if item['local_version_id'] is not None:
            version = db.execute('SELECT version_id FROM collector_versions WHERE collector_id = ? AND '
                                 'local_version_id = ?', (cid, _int(item['local_version_id'],
                                                                    f'{where}.local_version_id', low=1))).fetchone()
        if doc is None or (item['local_version_id'] is not None and version is None):
            ignored += 1                             # its document was not mirrored (refused): nothing to attach
            continue
        version_id = version[0] if version else None
        existing = db.execute('SELECT id FROM document_events WHERE document_id = ? AND version_id IS ? AND kind = ? '
                              'AND at = ? ORDER BY id LIMIT 1', (doc[0], version_id, item['kind'], when)).fetchone()
        mapped = db.execute('SELECT event_id FROM collector_events WHERE collector_id = ? AND local_event_id = ?',
                            (cid, local_event)).fetchone()
        if existing is not None and mapped is not None and mapped[0] == existing[0]:
            ignored += 1
            continue
        event_id = existing[0] if existing else store.add_document_event(db, push['scan_id'], doc[0], version_id,
                                                                          item['kind'], when)
        db.execute('INSERT OR REPLACE INTO collector_events(collector_id, local_event_id, event_id) VALUES(?,?,?)',
                   (cid, local_event, event_id))
        stored += 1
    return {'stored': stored, 'ignored': ignored}


def _end(db, collector: dict, push: dict, items: list, at: str) -> dict:
    if len(items) != 1:
        raise sync.SyncError('bad_batch', 'end has exactly one item')
    item = items[0]
    _need(item, ('counts', 'bytes_sent'), 'end item')
    _int(item['bytes_sent'], 'end bytes_sent')
    _discard_parts(db, push['id'])                  # a split version this push never finished
    swept = _sweep(db, collector, push, at) if item.get('complete') is True else 0
    totals = dict.fromkeys(('added', 'changed', 'restored', 'deleted', 'reparsed'), 0)
    totals.update(db.execute('SELECT kind, COUNT(*) FROM document_events WHERE scan_id = ? GROUP BY kind',
                             (push['scan_id'],)).fetchall())
    totals['failed'] = 0
    db.execute("UPDATE pushes SET status = 'done', finished_at = ?, counts_json = ? WHERE id = ?",
               (at, json.dumps({**item['counts'], 'bytes_sent': item['bytes_sent']}, sort_keys=True), push['id']))
    db.execute("UPDATE scans SET status = 'done', finished_at = ?, counts_json = ? WHERE id = ?",
               (at, json.dumps({'total': totals}, sort_keys=True), push['scan_id']))
    store.meta_set(db, 'chunk_version', chunks.CHUNK_VERSION)
    if store.fts_in_step(db):
        store.meta_set(db, 'fts_rows', str(store.fts_rows(db)))
        store.meta_set(db, 'fts_synced_at', at)
    store.bump_data_generation(db)
    hashes, missing = sync.summary_and_missing(db, push['model_key'], collector_id=collector['id'])
    return {**hashes, 'vectors_missing_total': missing, 'swept': swept}


def _sweep(db, collector: dict, push: dict, at: str) -> int:
    """After a push that sent every changed document (``end`` with ``complete``): an active document under a
    root of this push that this collector does not map (a file deleted before its store was rebuilt, whose old
    local id another file now has; or a retired collector's file the successor does not have) is the
    collector's no longer: ``deleted`` with a ``deleted`` event, its rows kept (review v2 N-1, L-e)."""
    roots = sorted(set(push['roots'].values()))
    if not roots:
        return 0
    rows = db.execute(
        f"SELECT d.id FROM documents d JOIN source_roots r ON r.id = d.root_id WHERE r.collector_id = ? "
        f"AND d.root_id IN ({','.join('?' * len(roots))}) AND d.status = 'active' AND NOT EXISTS("
        "SELECT 1 FROM collector_documents m WHERE m.document_id = d.id AND m.collector_id = ?) ORDER BY d.id",
        (collector['id'], *roots, collector['id'])).fetchall()
    for (doc_id,) in rows:
        db.execute("UPDATE documents SET status = 'deleted', deleted_at = ? WHERE id = ?", (at, doc_id))
        store.add_document_event(db, push['scan_id'], doc_id, None, 'deleted', at)
        refresh_chunks(db, doc_id)
    return len(rows)


HANDLERS = {'documents': _documents, 'forget': _forget, 'units': _units, 'vectors': _vectors, 'events': _events,
            'end': _end}


# ---- one request -------------------------------------------------------------------------------------------

def dispatch(db, collector: dict, method: str, name: str, query: dict, data: bytes | None, *,
             max_bytes: int = sync.BATCH_MAX_BYTES, now: str | None = None) -> tuple[int, dict]:
    """One push request after the token check: ``(HTTP status, answer envelope)``. ``name`` is the
    ``sync.ENDPOINTS`` key (``state``, ``vectors``, ``batch``), ``query`` the query string as a dict of
    strings, ``data`` the gzip request body of a batch. GET answers are ``sync.ok(state | page)``."""
    try:
        if name == 'batch' and method == 'POST':
            answer = apply(db, collector, sync.ungzip_json(data or b'', max_bytes), raw_bytes=len(data or b''), now=now)
        elif name == 'state' and method == 'GET':
            if query.get('full') == '1':
                answer = sync.ok(state_page(db, collector, after=int(query.get('after') or 0)))
            else:
                answer = sync.ok(state(db, collector, model_key=query.get('model_key') or None))
        elif name == 'vectors' and method == 'GET':
            answer = sync.ok(vectors_page(db, collector, model_key=query.get('model_key') or '',
                                          after=query.get('after') or ''))
        else:
            answer = sync.error('bad_batch', f'no {method} {name}')
    except sync.SyncError as exc:
        answer = sync.error(exc.code, exc.message)
    except ValueError:
        answer = sync.error('bad_batch', 'a query value is not a number')
    status = 200 if answer['ok'] else sync.ERROR_HTTP_STATUS[answer['error']['code']]
    return status, answer

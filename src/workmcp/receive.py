"""Operating server: apply one push batch to the store and answer the collector's state requests (design M5
remote §3.2, §3.3, appendix A).

Pure functions over a SQLite connection the caller owns (the server's ``push_db``, autocommit): a parsed body
in, the answer envelope out (``sync.ok`` / ``sync.error``). No starlette, no network, no file access;
``remote.py`` checks the token and the request size and calls ``dispatch`` (or the functions below it).

Rules: one batch is one ``BEGIN IMMEDIATE`` transaction and nothing is written for a refused batch (a
document whose versions disagree with what the server holds is refused alone, before anything of it is
written); a ``batch_id`` seen before answers its stored response and applies nothing; ids are the server's own
(``collector_documents``/``collector_versions`` map the collector's); unit sets are checked against their
hashes; chunks and their FTS rows are rebuilt here with the same ``CHUNK_VERSION`` (``index._chunk_version``,
the collector never sends chunk text) and only for eligible versions, as ``index`` does on the collector;
vectors are content addressed ``(model_key, text_sha256)`` and checked (dimension, finite, unit norm); a
deleted document's versions and units are kept. Messages name counts, ids and hashes, never unit text.

Push-run working state (the model key and the collector's root ids of the running push) lives in
``pushes.note`` as JSON; nothing else is added to the v5 schema.
"""
from __future__ import annotations

import hashlib
import json

from . import chunks, index, store, sync

# The collector a push token belongs to (``remote.lookup_token`` -> ``collectors`` row).
COLLECTOR_KEYS = ('id', 'name', 'retired_at')
VERSION_COLUMNS = ('seq', 'sha256', 'size', 'mtime_ns', 'observed_at', 'superseded_at', 'parser', 'parser_version',
                   'encoding', 'parse_status', 'parse_error', 'unit_count', 'text_chars')
# Columns a mirrored version may change (the v1 trigger's rule): mtime_ns and the parser columns always,
# superseded_at NULL -> value once, sha256/size of a 'locked' placeholder.
MUTABLE_VERSION_COLUMNS = ('mtime_ns', 'parser', 'parser_version', 'encoding', 'parse_status', 'parse_error',
                           'unit_count', 'text_chars')
DOCUMENT_STATUSES = ('active', 'deleted', 'skipped')
PARSE_STATUSES = ('ok', 'failed', 'locked', 'unsupported', 'too_large', 'encrypted', 'pending')
COPY_CANDIDATES = 5
LOCKED_SHA256 = '0' * 64


def _now(now: str | None) -> str:
    return now or store.now()


def _need(item, keys, where: str) -> None:
    missing = [key for key in keys if key not in item]
    if missing:
        raise sync.SyncError('bad_batch', f'{where} lacks {missing[0]!r}')


def _int(value, where: str, *, low: int = 0) -> int:
    if type(value) is not int or value < low:
        raise sync.SyncError('bad_batch', f'{where} is not an integer >= {low}')
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


# ---- state requests --------------------------------------------------------------------------------------

def _check_collector(collector: dict) -> None:
    if collector.get('retired_at'):
        raise sync.SyncError('collector_retired', f'collector {collector["name"]} is retired')


def state(db, collector: dict, *, model_key: str | None) -> dict:
    """``GET push/state[?model_key=K]``: ``sync.STATE_KEYS`` for this collector (its summary hashes from
    ``sync.summary`` for the collector's model key, the server's schema and chunk version, the model keys it
    holds vectors for, ``events_after``/``events_count`` of the collector's documents)."""
    _check_collector(collector)
    _sha(model_key, 'model_key', optional=True)
    hashes = sync.summary(db, model_key, collector_id=collector['id'])
    keys = [row[0] for row in db.execute('SELECT DISTINCT model_key FROM chunk_vectors ORDER BY 1')]
    return {'collector': collector['name'], 'schema_version': store.SCHEMA_VERSION,
            'chunk_version': chunks.CHUNK_VERSION, 'model_keys': keys, **hashes,
            'events_after': sync.events_after(db, collector['id']),
            'events_count': sync.event_count(db, collector_id=collector['id'])}


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


def _missing_total(db, collector_id: int, model_key: str | None) -> int:
    if model_key is None:
        return 0
    sql, args = _missing_sql(collector_id)
    return db.execute(f'SELECT COUNT(*) FROM ({sql})', [*args, model_key]).fetchone()[0]


# ---- batches ---------------------------------------------------------------------------------------------

def apply(db, collector: dict, body: dict, *, raw_bytes: int, now: str | None = None) -> dict:
    """Apply one checked-size, decompressed batch ``body`` of ``collector`` (``COLLECTOR_KEYS``) and return
    ``sync.ok(result)`` with ``sync.RESULT_KEYS[kind]`` or ``sync.error`` (``sync.ERROR_CODES``).
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
        return sync.error(exc.code, exc.message)
    except store.Busy:
        return sync.error('store_busy', 'the store is busy; retry')
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    return answer


def _running_push(db, collector: dict, push_id) -> dict:
    if type(push_id) is not int:
        raise sync.SyncError('push_not_running', 'no push_id')
    row = db.execute("SELECT id, scan_id, note FROM pushes WHERE id = ? AND collector_id = ? AND status = 'running'",
                     (push_id, collector['id'])).fetchone()
    if row is None:
        raise sync.SyncError('push_not_running', f'push {push_id} is not running for this collector')
    work = json.loads(row[2] or '{}')
    return {'id': row[0], 'scan_id': row[1], 'model_key': work.get('model_key'), 'roots': work.get('roots', {})}


def _begin(db, collector: dict, body: dict, at: str) -> dict:
    if len(body['items']) != 1:
        raise sync.SyncError('bad_batch', 'begin has exactly one item')
    item = body['items'][0]
    _need(item, ('roots', 'started_at'), 'begin item')
    model_key = _sha(item.get('model_key'), 'begin model_key', optional=True)
    _text(item['started_at'], 'begin started_at')
    stale = [row[0] for row in db.execute("SELECT id FROM pushes WHERE collector_id = ? AND status = 'running'",
                                          (collector['id'],))]
    for push_id in stale:      # a push that never ended (collect.scan closes a stale running scan the same way)
        db.execute("UPDATE pushes SET status = 'aborted', finished_at = ? WHERE id = ?", (at, push_id))
        db.execute("UPDATE scans SET status = 'aborted', finished_at = ?, note = 'push left running' "
                   "WHERE id = (SELECT scan_id FROM pushes WHERE id = ?) AND status = 'running'", (at, push_id))
        db.execute('DELETE FROM push_parts WHERE push_id = ?', (push_id,))
    roots, paths = {}, []
    for number, root in enumerate(item['roots']):
        where = f'roots[{number}]'
        _need(root, ('local_root_id', 'path', 'enabled'), where)
        local = _int(root['local_root_id'], f'{where}.local_root_id', low=1)
        path = _text(root['path'], f'{where}.path')
        enabled = 1 if root['enabled'] else 0
        found = db.execute('SELECT id, collector_id FROM source_roots WHERE path = ?', (path,)).fetchone()
        if found is None:
            server_id = db.execute('INSERT INTO source_roots(path, enabled, added_at, collector_id) VALUES(?,?,?,?)',
                                   (path, enabled, at, collector['id'])).lastrowid
        elif found[1] != collector['id']:
            raise sync.SyncError('root_owned_by_other_collector', f'root {local} belongs to another collector')
        else:
            server_id = found[0]
            db.execute('UPDATE source_roots SET enabled = ? WHERE id = ?', (enabled, server_id))
        roots[_key(local)] = server_id
        paths.append(path)
    scan_id = db.execute("INSERT INTO scans(started_at, status, partial, roots_json, note) VALUES(?, 'running', 1, ?, ?)",
                         (at, json.dumps(paths, ensure_ascii=False), f'push:{collector["name"]}')).lastrowid
    push_id = db.execute("INSERT INTO pushes(collector_id, scan_id, started_at, status, client_version, note) "
                         "VALUES(?, ?, ?, 'running', ?, ?)",
                         (collector['id'], scan_id, at, body['client_version'],
                          json.dumps({'model_key': model_key, 'roots': roots}, sort_keys=True))).lastrowid
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
    return fields


def _identity_problem(old: dict, new: dict) -> str | None:
    """Why the server's version row ``old`` cannot become ``new`` (the v1 ``versions_update_guard`` rule)."""
    if old['seq'] != new['seq'] or old['observed_at'] != new['observed_at']:
        return 'seq or observed_at differs'
    if (old['sha256'], old['size']) != (new['sha256'], new['size']) and old['parse_status'] != 'locked':
        return 'sha256 or size differs'
    if new['parse_status'] == 'locked' and old['parse_status'] != 'locked':
        return 'a read version cannot become locked'
    if old['superseded_at'] is not None and new['superseded_at'] != old['superseded_at']:
        return 'superseded_at is set once'
    return None


def _server_version(db, version_id: int) -> dict:
    row = db.execute(f'SELECT {", ".join(VERSION_COLUMNS)} FROM versions WHERE id = ?', (version_id,)).fetchone()
    return dict(zip(VERSION_COLUMNS, row))


def _drop_units(db, version_id: int) -> None:
    store.drop_chunks(db, version_id)
    db.execute('DELETE FROM units WHERE version_id = ?', (version_id,))


def _copy_source(db, version_id: int, fields: dict) -> int | None:
    """Another server version of the same content and parser version holding exactly this unit set."""
    for (candidate,) in db.execute(
            "SELECT v.id FROM versions v WHERE v.sha256 = ? AND v.parser_version IS ? AND v.parse_status = 'ok' "
            'AND v.id <> ? AND EXISTS(SELECT 1 FROM units u WHERE u.version_id = v.id) ORDER BY v.id DESC LIMIT ?',
            (fields['sha256'], fields['parser_version'], version_id, COPY_CANDIDATES)).fetchall():
        if sync.version_units_sha256(db, candidate) == fields['units_sha256']:
            return candidate
    return None


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
    if holds and wanted is not None and sync.version_units_sha256(db, version_id) == wanted:
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


def refresh_chunks(db, document_id: int) -> int:
    """The chunks of one document as ``index`` leaves them on the collector: built (or copied) under
    ``CHUNK_VERSION`` for an eligible version that holds its units, dropped for every other version. Returns
    the number of chunk rows stored."""
    stored = 0
    eligible = {row[0] for row in db.execute(f'SELECT v.id FROM {store.eligible_versions_sql()} AND d.id = ?',
                                             (document_id,))}
    for version_id, sha256, parser_version, unit_count in db.execute(
            'SELECT id, sha256, parser_version, unit_count FROM versions WHERE document_id = ? ORDER BY seq',
            (document_id,)).fetchall():
        held = db.execute('SELECT COUNT(*) FROM units WHERE version_id = ?', (version_id,)).fetchone()[0]
        if version_id not in eligible or held != unit_count:
            store.drop_chunks(db, version_id)
            continue
        store.drop_chunks(db, version_id, keep=chunks.CHUNK_VERSION)
        if db.execute('SELECT 1 FROM chunks WHERE version_id = ? AND chunk_version = ? LIMIT 1',
                      (version_id, chunks.CHUNK_VERSION)).fetchone() is None:
            stored += index._chunk_version(db, version_id, sha256, parser_version)[0]
    return stored


def _documents(db, collector: dict, push: dict, items: list, at: str) -> dict:
    if len(items) > sync.DOCUMENTS_PER_BATCH:
        raise sync.SyncError('bad_batch', f'over {sync.DOCUMENTS_PER_BATCH} documents in one batch')
    documents, versions, refused = {}, {}, []
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
        wanted = sorted((_version_fields(v, f'{where}.versions[{n}]') for n, v in enumerate(item['versions'])),
                        key=lambda fields: fields['seq'])
        if len({fields['seq'] for fields in wanted}) != len(wanted):
            raise sync.SyncError('bad_batch', f'{where}: two versions with one seq')
        mapped = db.execute('SELECT document_id FROM collector_documents WHERE collector_id = ? AND '
                            'local_document_id = ?', (collector['id'], local_doc)).fetchone()
        doc_id = mapped[0] if mapped else None
        if doc_id is None and db.execute('SELECT 1 FROM documents WHERE path_key = ?',
                                         (item['path_key'],)).fetchone():
            refused.append({'local_document_id': local_doc, 'code': 'root_owned_by_other_collector'})
            continue
        # every version is judged before anything of this document is written
        plan, problem = [], None
        for fields in wanted:
            row = db.execute('SELECT version_id FROM collector_versions WHERE collector_id = ? AND local_version_id = ?',
                             (collector['id'], fields['local_version_id'])).fetchone()
            if row is not None:
                old = _server_version(db, row[0])
                owner = db.execute('SELECT document_id FROM versions WHERE id = ?', (row[0],)).fetchone()[0]
                problem = 'another document' if owner != doc_id else _identity_problem(old, fields)
            elif doc_id is not None and db.execute('SELECT 1 FROM versions WHERE document_id = ? AND seq = ?',
                                                   (doc_id, fields['seq'])).fetchone():
                problem = 'seq taken by an unmapped version'
            if problem:
                break
            plan.append((fields, row[0] if row else None))
        if problem:
            refused.append({'local_document_id': local_doc, 'code': 'version_identity_conflict'})
            continue
        if doc_id is None:
            doc_id = db.execute(
                'INSERT INTO documents(root_id, path, path_key, ext, status, skip_reason, first_seen_at, last_seen_at, '
                'deleted_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (root_id, item['path'], item['path_key'], item['ext'], item['status'], item['skip_reason'],
                 item['first_seen_at'], item['last_seen_at'], item['deleted_at'])).lastrowid
            db.execute('INSERT INTO collector_documents(collector_id, local_document_id, document_id) VALUES(?,?,?)',
                       (collector['id'], local_doc, doc_id))
        else:
            db.execute('UPDATE documents SET root_id = ?, path = ?, ext = ?, status = ?, skip_reason = ?, '
                       'last_seen_at = ?, deleted_at = ? WHERE id = ?',
                       (root_id, item['path'], item['ext'], item['status'], item['skip_reason'], item['last_seen_at'],
                        item['deleted_at'], doc_id))
        documents[_key(local_doc)] = doc_id
        for fields, version_id in plan:
            if version_id is None:
                version_id = db.execute(
                    f'INSERT INTO versions(document_id, {", ".join(VERSION_COLUMNS)}) '
                    f'VALUES(?{",?" * len(VERSION_COLUMNS)})',
                    (doc_id, *(fields[column] for column in VERSION_COLUMNS))).lastrowid
                db.execute('INSERT INTO collector_versions(collector_id, local_version_id, version_id) VALUES(?,?,?)',
                           (collector['id'], fields['local_version_id'], version_id))
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
            versions[_key(fields['local_version_id'])] = {
                'id': version_id, 'units': _units_state(db, collector['id'], version_id, fields)}
        refresh_chunks(db, doc_id)
    store.bump_data_generation(db)
    return {'documents': documents, 'versions': versions, 'refused': refused}


# -- units ---------------------------------------------------------------------------------------------------

def _unit_rows(units, where: str) -> list[dict]:
    if not isinstance(units, list):
        raise sync.SyncError('bad_batch', f'{where} is not a list')
    rows = []
    for number, unit in enumerate(units):
        at = f'{where}[{number}]'
        _need(unit, ('ordinal', 'kind', 'locator_json', 'text', 'text_sha256'), at)
        rows.append({'ordinal': _int(unit['ordinal'], f'{at}.ordinal', low=1), 'kind': _text(unit['kind'], f'{at}.kind'),
                     'locator_json': _text(unit['locator_json'], f'{at}.locator_json'),
                     'text': _text(unit['text'], f'{at}.text'), 'text_sha256': _sha(unit['text_sha256'], f'{at}.text_sha256')})
    return rows


def _check_units(rows: list[dict], fields: dict, units_sha256: str) -> None:
    """Every text hash, ordinals 1..n, the unit count and character count of the version, the set hash."""
    if [row['ordinal'] for row in rows] != list(range(1, len(rows) + 1)):
        raise sync.SyncError('bad_units_hash', 'ordinals are not 1..n')
    for row in rows:
        if hashlib.sha256(row['text'].encode('utf-8')).hexdigest() != row['text_sha256']:
            raise sync.SyncError('bad_units_hash', f'unit {row["ordinal"]}: text_sha256 is not the hash of its text')
    if len(rows) != fields['unit_count'] or sum(len(row['text']) for row in rows) != (fields['text_chars'] or 0):
        raise sync.SyncError('bad_units_hash', 'unit or character count differs from the version')
    if sync.units_sha256(rows) != units_sha256:
        raise sync.SyncError('bad_units_hash', 'units_sha256 is not the hash of the units')


def _vectors_missing(db, version_id: int, model_key: str | None) -> list[str]:
    if model_key is None:
        return []
    return [row[0] for row in db.execute(
        'SELECT DISTINCT c.text_sha256 FROM chunks c WHERE c.version_id = ? AND c.chunk_version = ? AND NOT EXISTS('
        'SELECT 1 FROM chunk_vectors x WHERE x.model_key = ? AND x.text_sha256 = c.text_sha256) ORDER BY 1',
        (version_id, chunks.CHUNK_VERSION, model_key))]


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
        if parts > 1:
            db.execute('INSERT OR REPLACE INTO push_parts(push_id, local_version_id, part, parts, body_json, '
                       'received_at) VALUES(?,?,?,?,?,?)',
                       (push['id'], local, part, parts, sync.canonical(rows).decode('utf-8'), at))
            stored = db.execute('SELECT part, parts, body_json FROM push_parts WHERE push_id = ? AND local_version_id = ? '
                                'ORDER BY part', (push['id'], local)).fetchall()
            if any(row[1] != parts for row in stored):
                raise sync.SyncError('bad_batch', f'version {local}: parts disagree on their number')
            if len(stored) < parts:
                results.append({'local_version_id': local, 'received': len(stored), 'of': parts})
                continue
            rows = [unit for _, _, body in stored for unit in json.loads(body)]
            db.execute('DELETE FROM push_parts WHERE push_id = ? AND local_version_id = ?', (push['id'], local))
        rows = sync.join_pieces(rows)
        fields = _server_version(db, version_id)
        _check_units(rows, fields, units_sha256)
        if held != units_sha256 or sync.version_units_sha256(db, version_id) != units_sha256:
            _drop_units(db, version_id)
            db.executemany('INSERT INTO units(version_id, ordinal, kind, locator_json, text, text_sha256) '
                           'VALUES(?,?,?,?,?,?)', [(version_id, row['ordinal'], row['kind'], row['locator_json'],
                                                    row['text'], row['text_sha256']) for row in rows])
            db.execute('UPDATE collector_versions SET units_sha256 = ? WHERE collector_id = ? AND version_id = ?',
                       (units_sha256, collector['id'], version_id))
            document_id = db.execute('SELECT document_id FROM versions WHERE id = ?', (version_id,)).fetchone()[0]
            refresh_chunks(db, document_id)
        count = db.execute('SELECT COUNT(*) FROM chunks WHERE version_id = ? AND chunk_version = ?',
                           (version_id, chunks.CHUNK_VERSION)).fetchone()[0]
        results.append({'local_version_id': local, 'version_id': version_id, 'units': len(rows), 'chunks': count})
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
    if len(items) > sync.EVENTS_PER_BATCH:
        raise sync.SyncError('bad_batch', f'over {sync.EVENTS_PER_BATCH} events in one batch')
    stored = ignored = 0
    for number, item in enumerate(items):
        where = f'items[{number}]'
        _need(item, ('local_document_id', 'local_version_id', 'kind', 'at'), where)
        if item['kind'] not in store.EVENT_KINDS:
            raise sync.SyncError('bad_batch', f'{where}.kind is unknown')
        when = _text(item['at'], f'{where}.at')
        doc = db.execute('SELECT document_id FROM collector_documents WHERE collector_id = ? AND local_document_id = ?',
                         (collector['id'], _int(item['local_document_id'], f'{where}.local_document_id', low=1))).fetchone()
        version = None
        if item['local_version_id'] is not None:
            version = db.execute('SELECT version_id FROM collector_versions WHERE collector_id = ? AND '
                                 'local_version_id = ?', (collector['id'], _int(item['local_version_id'],
                                                                                f'{where}.local_version_id', low=1))
                                 ).fetchone()
        if doc is None or (item['local_version_id'] is not None and version is None):
            ignored += 1                             # its document was not mirrored (refused): nothing to attach
            continue
        version_id = version[0] if version else None
        if db.execute('SELECT 1 FROM document_events WHERE document_id = ? AND version_id IS ? AND kind = ? AND at = ?',
                      (doc[0], version_id, item['kind'], when)).fetchone():
            ignored += 1
            continue
        store.add_document_event(db, push['scan_id'], doc[0], version_id, item['kind'], when)
        stored += 1
    return {'stored': stored, 'ignored': ignored}


def _end(db, collector: dict, push: dict, items: list, at: str) -> dict:
    if len(items) != 1:
        raise sync.SyncError('bad_batch', 'end has exactly one item')
    item = items[0]
    _need(item, ('counts', 'bytes_sent'), 'end item')
    _int(item['bytes_sent'], 'end bytes_sent')
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
    return {**sync.summary(db, push['model_key'], collector_id=collector['id']),
            'vectors_missing_total': _missing_total(db, collector['id'], push['model_key'])}


HANDLERS = {'documents': _documents, 'units': _units, 'vectors': _vectors, 'events': _events, 'end': _end}


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

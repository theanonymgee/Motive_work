"""The operating server's side of a push (design M5 remote §3.3, §3.5, §5.2, appendix A): ``receive.apply`` and
the state answers, called directly with hand-made batch bodies, and ``binding.verify(..., stored_only=True)``.
Synthetic data only."""
import hashlib
import json

import pytest

from fixtures import LINES_KO, SENTINEL
from test_collect import scan
from test_push import Collector, Server, TOKEN
from workmcp import binding, chunks, index, parsers, push, receive, store, sync

H = '0' * 64


def sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def body(collector, kind, items, push_id=None):
    return sync.envelope(kind, items, collector=collector['name'], push_id=push_id, client_version='0.0.1')


def snapshot(db) -> dict:
    """Every table's rows (the store's whole content), to show a refused batch wrote nothing."""
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE "
                                           "'chunks_fts%' AND name <> 'sqlite_sequence' ORDER BY 1")]
    return {table: db.execute(f'SELECT * FROM {table}').fetchall() for table in tables}


@pytest.fixture
def server(tmp_path):
    srv = Server(tmp_path)
    yield srv
    srv.close()


UNITS = [{'ordinal': n, 'kind': 'line', 'locator_json': parsers.unit_json({'kind': 'line', 'line': n}), 'text': text,
          'text_sha256': sha(text)} for n, text in enumerate(LINES_KO, start=1)]


def version(local=11, seq=1, digest='a' * 64, units=UNITS, **change):
    row = {'local_version_id': local, 'seq': seq, 'sha256': digest, 'size': 100, 'mtime_ns': 1, 'observed_at':
           '2026-10-26T09:00:00+09:00', 'superseded_at': None, 'parser': 'text', 'parser_version': 'text-3',
           'encoding': 'utf-8', 'parse_status': 'ok', 'parse_error': None, 'unit_count': len(units),
           'text_chars': sum(len(u['text']) for u in units), 'units_sha256': sync.units_sha256(units)}
    return {**row, **change}


def document(local=1, versions=None, path='C:\\synthetic\\a.txt', **change):
    row = {'local_document_id': local, 'local_root_id': 1, 'path': path, 'path_key': path.lower(), 'ext': '.txt',
           'status': 'active', 'skip_reason': None, 'first_seen_at': '2026-10-26T09:00:00+09:00',
           'last_seen_at': '2026-10-26T09:00:00+09:00', 'deleted_at': None,
           'versions': [version()] if versions is None else versions}
    return {**row, **change}


def units_item(local=11, units=UNITS, part=1, parts=1, units_sha256=None):
    return {'local_version_id': local, 'units_sha256': units_sha256 or sync.units_sha256(UNITS), 'part': part,
            'parts': parts, 'units': [dict(unit) for unit in units]}


def begun(server, collector, root='C:\\synthetic', model_key=H):
    answer = receive.apply(server.db, collector, body(collector, 'begin', [{
        'roots': [{'local_root_id': 1, 'path': root, 'enabled': 1}], 'counts_expected': {},
        'started_at': store.now(), 'model_key': model_key}]), raw_bytes=10)
    assert answer['ok'], answer
    return answer['result']['push_id']


def ok(server, collector, kind, items, push_id):
    answer = receive.apply(server.db, collector, body(collector, kind, items, push_id), raw_bytes=10)
    assert answer['ok'], answer
    assert set(answer['result']) == set(sync.RESULT_KEYS[kind])
    return answer['result']


def refused(server, collector, kind, items, push_id, code):
    before = snapshot(server.db)
    answer = receive.apply(server.db, collector, body(collector, kind, items, push_id), raw_bytes=10)
    assert answer == sync.error(code, answer['error']['message']), answer
    assert snapshot(server.db) == before                      # one transaction: nothing written
    assert SENTINEL not in json.dumps(answer, ensure_ascii=False)
    return answer


def test_one_push_by_hand_mirrors_units_and_rebuilds_chunks(server):
    collector = server.collector('합성')
    push_id = begun(server, collector)
    documents = ok(server, collector, 'documents', [document()], push_id)
    assert documents['versions'] == {'11': {'id': 1, 'units': 'need'}} and documents['refused'] == []
    result = ok(server, collector, 'units', [units_item()], push_id)
    assert result['versions'] == [{'local_version_id': 11, 'version_id': 1, 'units': 5, 'chunks': 1}]
    rows = server.db.execute('SELECT ordinal, kind, locator_json, text, text_sha256 FROM units ORDER BY ordinal')
    assert [dict(zip(('ordinal', 'kind', 'locator_json', 'text', 'text_sha256'), row)) for row in rows] == UNITS
    chunk = server.db.execute('SELECT text, chunk_version FROM chunks').fetchone()
    assert chunk == ('\n'.join(LINES_KO), chunks.CHUNK_VERSION)       # rebuilt here, same rules
    assert store.fts_rows(server.db) == 1
    assert result['vectors_missing'] == [sha(chunk[0])]
    vector = index.to_blob([1.0] + [0.0] * 1023)
    stored = ok(server, collector, 'vectors', [{'model_key': H, 'dims': 1024, 'items': [
        {'text_sha256': sha(chunk[0]), 'vector_b64': sync.vector_to_b64(vector)},
        {'text_sha256': 'b' * 64, 'vector_b64': sync.vector_to_b64(vector)},                 # no such chunk
        {'text_sha256': 'c' * 64, 'vector_b64': sync.vector_to_b64(index.to_blob([2.0] + [0.0] * 1023))},  # norm
        {'text_sha256': 'd' * 64, 'vector_b64': 'not base64!'}]}], push_id)
    assert stored == {'stored': 1, 'ignored': 1, 'invalid': 2}
    assert store.meta_get(server.db, 'index_generation') == '1'
    events = ok(server, collector, 'events', [
        {'local_event_id': 1, 'local_document_id': 1, 'local_version_id': 11, 'kind': 'added',
         'at': '2026-10-26T09:00:00+09:00'},
        {'local_event_id': 1, 'local_document_id': 1, 'local_version_id': 11, 'kind': 'added',
         'at': '2026-10-26T09:00:00+09:00'},
        {'local_event_id': 2, 'local_document_id': 9, 'local_version_id': None, 'kind': 'deleted',
         'at': '2026-10-26T09:00:00+09:00'}],
        push_id)
    assert events == {'stored': 1, 'ignored': 2}
    end = ok(server, collector, 'end', [{'counts': {'documents': 1, 'units': 5, 'vectors': 1}, 'bytes_sent': 50}],
             push_id)
    assert end['vectors_missing_total'] == 0 and all(sync.is_sha256(end[k]) for k in sync.SUMMARY_KEYS)
    assert store.last_push(server.db) == {'collector': '합성', 'at': store.last_push(server.db)['at'], 'documents': 1,
                                          'units': 5, 'vectors': 1}
    assert store.last_scan(server.db)['added'] == 1
    assert server.db.execute('SELECT batches, bytes_in, status FROM pushes').fetchone() == (6, 60, 'done')
    assert store.fts_status(server.db)['consistent']
    state = receive.state(server.db, collector, model_key=H)
    assert tuple(state) == sync.STATE_KEYS and state['events_max'] == 1 and state['model_keys'] == [H]
    assert {k: state[k] for k in sync.SUMMARY_KEYS} == {k: end[k] for k in sync.SUMMARY_KEYS}
    page = receive.state_page(server.db, collector)
    assert tuple(page) == sync.STATE_PAGE_KEYS and page['next_after'] is None
    assert page['documents'][0]['units_sha256'] == sync.units_sha256(UNITS)
    assert receive.vectors_page(server.db, collector, model_key=H) == {'missing': [], 'next_after': None}
    assert binding.verify(server.db, stored_only=True)['ok']


def test_batch_is_one_transaction_and_refuses_bad_hashes(server):
    collector = server.collector('합성')
    push_id = begun(server, collector)
    ok(server, collector, 'documents', [document()], push_id)
    bad_text = [dict(UNITS[0], text_sha256=H), *UNITS[1:]]
    refused(server, collector, 'units', [units_item(units=bad_text)], push_id, 'bad_units_hash')
    gap = [UNITS[0], *UNITS[2:]]
    refused(server, collector, 'units', [units_item(units=gap, units_sha256=sync.units_sha256(gap))], push_id,
            'bad_units_hash')
    refused(server, collector, 'units', [units_item(units_sha256='e' * 64)], push_id, 'bad_units_hash')
    other = [dict(UNITS[0], kind='paragraph'), *UNITS[1:]]
    refused(server, collector, 'units', [units_item(units=other)], push_id, 'bad_batch')     # kind != locator's
    refused(server, collector, 'units', [units_item(local=99)], push_id, 'version_not_awaiting_units')
    # a good item and a bad one in the same batch: neither is applied
    refused(server, collector, 'units', [units_item(), units_item(local=99)], push_id, 'version_not_awaiting_units')
    assert server.count('units') == 0
    wrong = body(collector, 'units', [units_item()], push_id)
    wrong['chunk_version'] = 'units-0-v0'
    wrong['batch_id'] = sync.batch_id(wrong)
    assert receive.apply(server.db, collector, wrong, raw_bytes=1)['error']['code'] == 'chunk_version_mismatch'
    forged = body(collector, 'units', [units_item()], push_id)
    forged['items'][0]['units'][0]['text'] = '바뀐 본문'                # body changed after its batch id
    assert receive.apply(server.db, collector, forged, raw_bytes=1)['error']['code'] == 'bad_batch'
    other_name = sync.envelope('units', [units_item()], collector='다른', push_id=push_id, client_version='0')
    assert receive.apply(server.db, collector, other_name, raw_bytes=1)['error']['code'] == 'bad_batch'
    refused(server, collector, 'documents', [document(versions=[version(), version(local=12)])], push_id,
            'bad_batch')                                                 # two versions with one seq
    refused(server, collector, 'documents', [document(local_root_id=7)], push_id, 'bad_batch')
    refused(server, collector, 'end', [{'counts': {}, 'bytes_sent': 0}], push_id + 5, 'push_not_running')
    status, answer = receive.dispatch(server.db, collector, 'POST', 'batch', {},
                                      sync.gzip_json(body(collector, 'units', [units_item()], push_id)), max_bytes=100)
    assert (status, answer['error']['code']) == (413, 'batch_too_large')
    status, answer = receive.dispatch(server.db, collector, 'POST', 'batch', {}, b'not gzip')
    assert (status, answer['error']['code']) == (400, 'bad_batch')
    assert receive.dispatch(server.db, collector, 'GET', 'state', {'model_key': 'x'}, None)[0] == 400
    assert receive.dispatch(server.db, collector, 'GET', 'state', {'full': '1', 'after': 'x'}, None)[0] == 400
    assert receive.dispatch(server.db, collector, 'DELETE', 'state', {}, None)[0] == 400
    # and the good batch still applies afterwards
    assert ok(server, collector, 'units', [units_item()], push_id)['versions'][0]['units'] == 5


def test_a_batch_sent_again_answers_the_stored_response_and_applies_nothing(server):
    collector = server.collector('합성')
    first = body(collector, 'begin', [{'roots': [{'local_root_id': 1, 'path': 'C:\\synthetic', 'enabled': 1}],
                                       'counts_expected': {}, 'started_at': '2026-10-26T09:00:00.000001+09:00'}])
    answer = receive.apply(server.db, collector, first, raw_bytes=5)
    again = receive.apply(server.db, collector, first, raw_bytes=5)
    assert answer == again and answer['ok'] and server.count('pushes') == 1
    push_id = answer['result']['push_id']
    docs = body(collector, 'documents', [document()], push_id)
    assert receive.apply(server.db, collector, docs, raw_bytes=5) == receive.apply(server.db, collector, docs,
                                                                                   raw_bytes=5)
    assert server.count('versions') == 1 and server.count('push_batches') == 2
    assert server.db.execute('SELECT batches, bytes_in FROM pushes').fetchone() == (2, 10)
    # another run's begin has another started_at, so another batch id and a new push (the old one is aborted)
    second = body(collector, 'begin', [{'roots': [{'local_root_id': 1, 'path': 'C:\\synthetic', 'enabled': 1}],
                                        'counts_expected': {}, 'started_at': '2026-10-26T09:00:00.000002+09:00'}])
    assert receive.apply(server.db, collector, second, raw_bytes=5)['result']['push_id'] == push_id + 1
    assert server.db.execute('SELECT status FROM pushes ORDER BY id').fetchall() == [('aborted',), ('running',)]
    assert server.db.execute("SELECT status FROM scans ORDER BY id").fetchall() == [('aborted',), ('running',)]


def server_versions(server, doc_id):
    return server.db.execute('SELECT seq, sha256, superseded_at IS NOT NULL FROM versions WHERE document_id = ? '
                             'ORDER BY seq', (doc_id,)).fetchall()


def test_versions_are_matched_by_content_and_the_server_keeps_its_history(server):
    """Review M5 v1 M-1: a version is matched within its document by content, never refused for an observation
    the server already holds; what the collector no longer has stays on the server as superseded history."""
    collector = server.collector('합성')
    push_id = begun(server, collector)
    ok(server, collector, 'documents', [document(), document(local=2, path='C:\\synthetic\\b.txt',
                                                             versions=[version(local=21)])], push_id)
    # the same local version id now holds other content (a rebuilt store): a new server version, the old kept
    result = ok(server, collector, 'documents', [document(versions=[version(digest='f' * 64)]),
                                                 document(local=2, path='C:\\synthetic\\b.txt', status='deleted',
                                                          deleted_at=store.now(), versions=[version(local=21)])],
                push_id)
    assert result['refused'] == [] and result['versions']['11'] == {'id': 3, 'units': 'need'}
    assert server_versions(server, 1) == [(1, 'a' * 64, 1), (2, 'f' * 64, 0)]
    assert server.db.execute('SELECT status FROM documents WHERE id=2').fetchone() == ('deleted',)
    summary = sync.document_summaries(server.db, collector_id=collector['id'])[0]
    assert (summary['sha256'], summary['superseded']) == ('f' * 64, 0)    # only mapped versions count
    # an older store (restored) whose current version the server had superseded: a new version, the newer
    # server version becomes history
    ok(server, collector, 'documents', [document(local=4, path='C:\\synthetic\\d.txt', versions=[
        version(local=41, digest='d' * 64, superseded_at='2026-10-26T10:00:00+09:00'),
        version(local=42, seq=2, digest='e' * 64)])], push_id)
    doc4 = server.db.execute("SELECT id FROM documents WHERE path_key LIKE '%d.txt'").fetchone()[0]
    ok(server, collector, 'documents', [document(local=4, path='C:\\synthetic\\d.txt',
                                                 versions=[version(local=41, digest='d' * 64)])], push_id)
    assert server_versions(server, doc4) == [(1, 'd' * 64, 1), (2, 'e' * 64, 1), (3, 'd' * 64, 0)]
    assert server.db.execute('SELECT COUNT(*) FROM collector_versions m JOIN versions v ON v.id = m.version_id '
                             'WHERE v.document_id = ?', (doc4,)).fetchone()[0] == 1
    # a locked placeholder gets its real sha256 when it is read (the collector's fill in place)
    locked = version(local=31, digest='0' * 64, parse_status='locked', parser=None, parser_version=None,
                     encoding=None, unit_count=None, text_chars=None, units_sha256=None)
    ok(server, collector, 'documents', [document(local=3, path='C:\\synthetic\\c.txt', versions=[locked])], push_id)
    filled = ok(server, collector, 'documents', [document(local=3, path='C:\\synthetic\\c.txt',
                                                          versions=[version(local=31, digest='c' * 64)])], push_id)
    assert filled['refused'] == [] and filled['versions']['31']['units'] == 'need'
    doc3 = filled['documents']['3']
    assert server_versions(server, doc3) == [(1, 'c' * 64, 0)]


def test_documents_outside_their_root_or_over_the_size_limit_are_refused_alone(server):
    collector = server.collector('합성')
    push_id = begun(server, collector)
    big = version(local=51, unit_count=sync.VERSION_MAX_UNITS + 1)
    huge = version(local=52, text_chars=sync.VERSION_MAX_CHARS + 1)
    result = ok(server, collector, 'documents', [
        document(local=5, path='C:\\other\\a.txt'),                                # outside its root
        document(local=6, path='C:\\synthetic-x\\a.txt'),                          # a prefix, not a folder
        document(local=7, path='C:\\synthetic\\big.txt', versions=[big]),
        document(local=8, path='C:\\synthetic\\huge.txt', versions=[huge]),
        document(local=9, path='C:\\synthetic\\fine.txt', versions=[version(local=91)])], push_id)
    assert result['refused'] == [{'local_document_id': 5, 'code': 'path_outside_root'},
                                 {'local_document_id': 6, 'code': 'path_outside_root'},
                                 {'local_document_id': 7, 'code': 'version_too_large'},
                                 {'local_document_id': 8, 'code': 'version_too_large'}]
    assert set(result['documents']) == {'9'} and server.count('documents') == 1
    assert sync.VERSION_MAX_UNITS == parsers.Limits().max_units
    assert sync.VERSION_MAX_CHARS == parsers.Limits().max_text_chars
    assert sync.MAX_PARTS >= -(-sync.VERSION_MAX_UNITS // sync.UNITS_PER_BATCH)
    assert sync.MAX_PARTS >= -(-sync.VERSION_MAX_CHARS // sync.UNIT_CHARS_PER_BATCH)


@pytest.mark.parametrize('change', [
    lambda b: b['items'][0]['versions'][0].update(mtime_ns=10**30),
    lambda b: b['items'][0].update(local_document_id=2**70),
    lambda b: b['items'][0]['versions'][0].update(size=-1),
    lambda b: b['items'][0].update(versions='not a list'),
])
def test_malformed_values_are_bad_batch_not_an_exception(server, change):
    collector = server.collector('합성')
    push_id = begun(server, collector)
    bad = body(collector, 'documents', [document()], push_id)
    change(bad)
    bad['batch_id'] = sync.batch_id(bad)
    before = snapshot(server.db)
    assert receive.apply(server.db, collector, bad, raw_bytes=1)['error']['code'] == 'bad_batch'
    assert snapshot(server.db) == before


def test_deep_nesting_and_bad_locators_are_bad_batch(server):
    collector = server.collector('합성')
    push_id = begun(server, collector)
    deep = '{"a":' * 100_000 + '1' + '}' * 100_000
    status, answer = receive.dispatch(server.db, collector, 'POST', 'batch', {}, __import__('gzip').compress(deep.encode()))
    assert (status, answer['error']['code']) == (400, 'bad_batch')
    nested = body(collector, 'events', [], push_id)
    value = []
    for _ in range(50_000):
        value = [value]
    nested['items'] = [{'local_event_id': 1, 'local_document_id': 1, 'local_version_id': None, 'kind': 'added',
                        'at': value}]
    assert receive.apply(server.db, collector, nested, raw_bytes=1)['error']['code'] == 'bad_batch'
    ok(server, collector, 'documents', [document()], push_id)
    for locator_json in ('not json', '{"line":1,"kind":"line"}', parsers.unit_json({'kind': 'page', 'page': 1}),
                         parsers.unit_json({'kind': 'line', 'line': 0}), parsers.unit_json({'kind': 'line'})):
        units = [dict(UNITS[0], locator_json=locator_json), *UNITS[1:]]
        refused(server, collector, 'units', [units_item(units=units, units_sha256=sync.units_sha256(units))], push_id,
                'bad_batch')
    assert receive.unit_json({'kind': 'line', 'line': 3, 'b': '한'}) == parsers.unit_json({'kind': 'line', 'line': 3,
                                                                                            'b': '한'})


def test_two_collectors_share_vectors_and_keep_roots_apart(tmp_path):
    server = Server(tmp_path)
    first = server.collector('합성-하나')
    second = server.collector('합성-둘')
    a = Collector(tmp_path, 'pc-a')
    b = Collector(tmp_path, 'pc-b')
    for col in (a, b):
        (col.src/'notes.txt').write_text('\n'.join(LINES_KO), encoding='utf-8')
        (col.src/'other.txt').write_text(f'{col.name} 합성 메모\n', encoding='utf-8')
        col.collect()
    try:
        assert a.push(server, first)['converged']
        report = b.push(server, second)
        assert report['exit_code'] == 0 and report['converged']
        assert report['sent']['copied_versions'] == 1 and report['sent']['units'] == 1     # only other.txt's line
        assert report['sent']['vectors'] == 1                                               # notes' vector is shared
        for col, owner in ((a, first), (b, second)):
            assert sync.summary(server.db, col.key, collector_id=owner['id']) == sync.summary(col.db, col.key)
            assert sync.document_summaries(server.db, collector_id=owner['id']) == sync.document_summaries(col.db)
        assert server.count('documents') == 4 and server.count('chunk_vectors') == 3
        assert [row['name'] for row in store.collector_status(server.db)] == ['합성-하나', '합성-둘']
        # the same root path from another collector is refused, and nothing is written
        path = a.db.execute('SELECT path FROM source_roots').fetchone()[0]
        before = snapshot(server.db)
        answer = receive.apply(server.db, second, body(second, 'begin', [{
            'roots': [{'local_root_id': 1, 'path': path, 'enabled': 1}], 'counts_expected': {},
            'started_at': store.now()}]), raw_bytes=1)
        assert answer['error']['code'] == 'root_owned_by_other_collector' and snapshot(server.db) == before
        # a retired collector is refused (remote checks the token; receive checks again)
        retired = {**second, 'retired_at': store.now()}
        assert receive.apply(server.db, retired, body(retired, 'end', [], 1), raw_bytes=1)['error']['code'] == \
            'collector_retired'
        with pytest.raises(sync.SyncError):
            receive.state(server.db, retired, model_key=None)
    finally:
        a.close()
        b.close()
        server.close()


def test_store_busy_writes_nothing_and_is_retryable(server, tmp_path):
    import sqlite3
    collector = server.collector('합성')
    other = sqlite3.connect(server.home/'db'/'work.sqlite3', isolation_level=None)
    server.db.execute('PRAGMA busy_timeout=50')
    other.execute('BEGIN IMMEDIATE')
    try:
        answer = receive.apply(server.db, collector, body(collector, 'begin', [{
            'roots': [], 'counts_expected': {}, 'started_at': store.now()}]), raw_bytes=1)
    finally:
        other.execute('ROLLBACK')
        other.close()
    assert answer['error']['code'] == 'store_busy' and answer['error']['retryable'] is True
    assert server.count('pushes') == 0


# ---- verify --stored (design §5.2) -----------------------------------------------------------------------

def test_stored_only_verify_needs_no_file_and_finds_damage(tmp_path):
    server = Server(tmp_path)
    collector = server.collector('합성')
    col = Collector(tmp_path)
    (col.src/'notes.txt').write_text('\n'.join(LINES_KO), encoding='utf-8')
    (col.src/'more.txt').write_text('\n'.join(reversed(LINES_KO)), encoding='utf-8')
    col.collect()
    try:
        assert col.push(server, collector)['converged']
        for path in col.src.iterdir():
            path.unlink()                                  # no original anywhere: the server has none either
        report = binding.verify(server.db, stored_only=True)
        assert report['ok'] and report['scope'] == binding.SCOPE_STORED
        assert report['versions']['checked'] == 2 and report['units']['bound'] == report['units']['total'] == 10
        assert sum(report['versions']['unverifiable'].values()) == 0 and report['chunks_checked'] == 2
        assert binding.verify(col.db)['versions']['unverifiable']['missing'] == 2     # the collector needs files
        # damage: a unit text no longer matching its hash, a chunk no longer the slice, a units set not the one sent
        server.db.execute("UPDATE units SET text = text || '!' WHERE version_id = 1 AND ordinal = 2")
        server.db.execute("UPDATE chunks SET text_sha256 = ? WHERE version_id = 2", (H,))
        server.db.execute('UPDATE collector_versions SET units_sha256 = ? WHERE version_id = 2', (H,))
        damaged = binding.verify(server.db, stored_only=True)
        problems = {f['problem'] for f in damaged['failures']}
        assert not damaged['ok'] and damaged['versions']['failed'] == 2
        assert {'unit_text_sha256_mismatch', 'chunk_text_mismatch', 'chunk_text_sha256_mismatch',
                'units_sha256_mismatch'} <= problems
        assert all(SENTINEL not in json.dumps(f, ensure_ascii=False) for f in damaged['failures'])
    finally:
        col.close()
        server.close()


def test_report_lines_name_what_left_the_pc():
    report = push.new_report('https://srv.example.invalid/workmcp')
    report.update(push_id=3, collector='합성', state={**report['state'], 'documents_changed': 2, 'added': 2},
                  sent={**report['sent'], 'documents': 2, 'versions': 2, 'units': 1234, 'chars': 56789,
                        'vectors': 4, 'vector_bytes': 4 * 4096, 'events': 2},
                  batches=5, bytes_sent=12345, elapsed=0.5, log='C:\\synthetic\\push.log',
                  server={'documents_sha256': 'a' * 64, 'vectors_sha256': 'b' * 64, 'vectors_missing': 0},
                  converged=True)
    lines = push.report_lines(report)
    assert lines[0].startswith('push #3 to https://srv.example.invalid/workmcp as 합성  started ')
    assert lines[1] == ('  state: documents changed 2 (added 2 changed 0 deleted 0 restored 0 reparsed 0)  '
                        'vectors missing on server 0')
    assert lines[2] == ('  sent: documents 2  versions 2  units 1,234 (56,789 chars)  vectors 4 (16 KB)  events 2  '
                        'copied on the server 0')
    assert lines[3].startswith('  batches 5  bytes sent 12,345 (gzip)  refused 0  retries 0  0.5s')
    assert lines[4] == '  server now: documents_sha256 aaaa…  vectors_sha256 bbbb…  vectors still missing 0'
    assert TOKEN not in '\n'.join(lines)


def test_chunk_stream_resumed_between_parts_equals_build_chunks(tmp_path):
    """The server's part-by-part chunk stream (state kept as JSON between batches) gives exactly
    ``chunks.build_chunks`` of the whole version, for every version of the bundle and long units."""
    from fixtures import bundle as synthetic_bundle
    col = Collector(tmp_path)
    synthetic_bundle.write_bundle(col.src)
    (col.src/'long.txt').write_text('\n'.join(['짧은 줄'] * 30 + ['긴 문장입니다. ' * 400] + ['끝 줄'] * 5),
                                    encoding='utf-8')
    scan(col.db, col.root)
    try:
        versions = [row[0] for row in col.db.execute("SELECT id FROM versions WHERE parse_status = 'ok' AND unit_count > 0")]
        assert len(versions) > 20
        for version_id in versions:
            units = [(o, json.loads(u), t) for o, u, t in col.db.execute(
                'SELECT ordinal, locator_json, text FROM units WHERE version_id = ? ORDER BY ordinal', (version_id,))]
            expected = chunks.build_chunks(units)
            for every in (1, 2, 7, len(units)):
                out, state = [], None
                for at in range(0, len(units), every):
                    stream = receive.ChunkStream(state)
                    for unit in units[at:at + every]:
                        out += stream.feed(*unit)
                    state = json.loads(json.dumps(stream.state))
                out += receive.ChunkStream(state).finish()
                assert out == expected, (version_id, every)
    finally:
        col.close()

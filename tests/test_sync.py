"""Wire schema of ``workmcp push`` (design M5 remote §3, ``sync.py``): constants, batch ids, envelope checks, gzip
bodies, vector blobs, the summary hashes and the URL rules. Stage 0 skeleton; stage 1 adds the cross-side
summary test after a real push (``test_summary_hashes_match_across_sides``). Synthetic data only."""
import base64
import gzip
import hashlib
import json

import pytest

from fixtures import bundle
from workmcp import chunks, embedder, index, paths, store, sync

H = '0' * 64


def test_constants_cover_every_kind_and_code():
    assert set(sync.WIRE_KEYS) == set(sync.RESULT_KEYS) == set(sync.KINDS)
    assert {kind for kind, _ in sync.NESTED_KEYS} <= set(sync.KINDS)
    assert all(key in sync.WIRE_KEYS[kind] for kind, key in sync.NESTED_KEYS)
    assert all(key in sync.WIRE_KEYS[kind] for kind, key in sync.COUNT_FIELDS)
    assert len(set(sync.ERROR_CODES)) == len(sync.ERROR_CODES) and set(sync.ERROR_HTTP_STATUS) == set(sync.ERROR_CODES)
    assert sync.RETRYABLE <= set(sync.ERROR_CODES)
    # the twelve codes of design §11.1 are all there
    assert {'invalid_token', 'wrong_token_kind', 'collector_retired', 'schema_version_mismatch',
            'chunk_version_mismatch', 'batch_too_large', 'bad_units_hash', 'version_identity_conflict',
            'root_owned_by_other_collector', 'push_not_running', 'version_not_awaiting_units',
            'invalid_vector'} <= set(sync.ERROR_CODES)
    assert sync.VECTOR_B64_CHARS == 5464 == len(base64.b64encode(bytes(4 * sync.VECTOR_DIMS)))
    assert sync.UNIT_STATES == ('have', 'copied', 'need', 'none')
    # no wire key carries file bytes: text travels only as unit text (design §6.4)
    flat = set().union(*sync.WIRE_KEYS.values(), *sync.NESTED_KEYS.values())
    assert not flat & {'content', 'bytes', 'data', 'file', 'blob', 'raw'}


def test_error_envelopes_and_sync_error():
    assert sync.ok({'a': 1}) == {'ok': True, 'result': {'a': 1}}
    assert sync.error('store_busy', 'x') == {'ok': False, 'error': {'code': 'store_busy', 'message': 'x',
                                                                    'retryable': True}}
    assert sync.error('bad_batch')['error']['retryable'] is False
    assert sync.SyncError('invalid_vector').retryable is False
    with pytest.raises(ValueError):
        sync.SyncError('no_such_code')
    with pytest.raises(ValueError):
        sync.error('no_such_code')


def body(kind='events', items=None, push_id=3):
    return sync.envelope(kind, items if items is not None else
                         [{'local_document_id': 1, 'local_version_id': None, 'kind': 'added', 'at': '2026-10-26'}],
                         collector='합성-수집기', push_id=push_id, client_version='0.0.1')


def test_envelope_has_a_batch_id_of_its_own_body():
    first = body()
    assert set(first) == sync.ENVELOPE_KEYS
    assert first['batch_id'] == sync.batch_id(first) == sync.batch_id({**first, 'batch_id': 'ignored'})
    assert first['schema_version'] == store.SCHEMA_VERSION and first['chunk_version'] == chunks.CHUNK_VERSION
    assert body()['batch_id'] == first['batch_id']                       # same batch, same id (idempotent)
    assert body(push_id=4)['batch_id'] != first['batch_id']
    sync.check_body(first)
    with pytest.raises(ValueError):
        sync.envelope('other', [], collector='c', push_id=None, client_version='0')


@pytest.mark.parametrize('change, code', [
    (lambda b: b.update(extra=1), 'bad_batch'),
    (lambda b: b['items'][0].update(text='본문'), 'bad_batch'),
    (lambda b: b.pop('collector'), 'bad_batch'),
    (lambda b: b.update(kind='other'), 'bad_batch'),
    (lambda b: b.update(items={}), 'bad_batch'),
    (lambda b: b.update(protocol=2), 'bad_batch'),
    (lambda b: b.update(schema_version=4), 'schema_version_mismatch'),
    (lambda b: b.update(chunk_version='other'), 'chunk_version_mismatch'),
    (lambda b: b['items'][0].update(kind='deleted'), 'bad_batch'),       # batch id no longer the hash
])
def test_check_body_refuses(change, code):
    refused = body()
    change(refused)
    with pytest.raises(sync.SyncError) as caught:
        sync.check_body(refused)
    assert caught.value.code == code


def test_nested_keys_are_checked():
    units = {'local_version_id': 1, 'units_sha256': H, 'part': 1, 'parts': 1,
             'units': [{'ordinal': 1, 'kind': 'line', 'locator_json': '{}', 'text': '합성', 'text_sha256': H}]}
    assert sync.body_problems(body('units', [units])) == []
    units['units'][0]['path'] = 'C:\\x'
    assert sync.body_problems(body('units', [units])) == ["items[0].units[0] has unknown key 'path'"]
    begin = {'roots': [{'local_root_id': 1, 'path': 'C:\\synthetic', 'enabled': 1}], 'counts_expected': {'documents': 2}}
    assert sync.body_problems(body('begin', [begin])) == []
    begin['counts_expected'] = {'documents': '2'}
    assert sync.body_problems(body('begin', [begin])) == ['items[0].counts_expected is not a map of integers']


def test_gzip_bodies_round_trip_with_a_size_limit():
    value = body()
    data = sync.gzip_json(value)
    assert data == sync.gzip_json(value) and sync.ungzip_json(data) == value
    assert json.loads(gzip.decompress(data)) == value
    with pytest.raises(sync.SyncError) as caught:
        sync.ungzip_json(gzip.compress(b'"' + b'x' * 2000 + b'"'), max_bytes=1000)
    assert caught.value.code == 'batch_too_large'
    for bad in (b'not gzip', gzip.compress(b'{not json'), gzip.compress('"합성"'.encode('cp949'))):
        with pytest.raises(sync.SyncError) as caught:
            sync.ungzip_json(bad)
        assert caught.value.code == 'bad_batch'


def test_vector_blobs_round_trip_and_bad_ones_are_refused():
    blob = index.to_blob([1.0] + [0.0] * (sync.VECTOR_DIMS - 1))
    text = sync.vector_to_b64(blob)
    assert len(text) == sync.VECTOR_B64_CHARS and sync.vector_from_b64(text) == blob
    for bad in (text[:-4], 'not base64!', base64.b64encode(b'x' * 12).decode(), 123):
        with pytest.raises(sync.SyncError) as caught:
            sync.vector_from_b64(bad)
        assert caught.value.code == 'invalid_vector'


def test_units_sha256_hashes_ordered_lines_of_text_hashes():
    rows = [(1, 'line', '{"line":1}', 'a' * 64), (2, 'line', '{"line":2}', 'b' * 64)]
    expected = hashlib.sha256('1|line|{"line":1}|'.encode() + b'a' * 64 + b'\n2|line|{"line":2}|' + b'b' * 64)
    assert sync.units_sha256(rows) == expected.hexdigest()
    as_dicts = [dict(zip(('ordinal', 'kind', 'locator_json', 'text_sha256'), row), text='본문') for row in rows]
    assert sync.units_sha256(as_dicts) == expected.hexdigest()
    assert sync.units_sha256(reversed(rows)) != expected.hexdigest()
    assert sync.units_sha256([]) == hashlib.sha256(b'').hexdigest()


@pytest.fixture
def filled(tmp_path):
    """The synthetic bundle scanned and indexed (HashEmbedder 1024) into a fresh home."""
    from test_collect import add_root, scan
    src = tmp_path/'src'
    bundle.write_bundle(src)
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    db = store.open_home(home)
    scan(db, add_root(db, src))
    vectors = embedder.HashEmbedder(1024)
    result = index.run_index(db, home, embedder=vectors)
    yield db, result['model_key']
    db.close()


def test_summary_is_the_same_through_an_identity_collector_map(filled):
    """Server mode reads the same rows through collector_documents/collector_versions: with every id mapped to
    itself, the server-side summary equals the collector-side one (the cross-side test after a push is stage 1)."""
    db, key = filled
    mine = sync.summary(db, key)
    assert set(mine) == set(sync.SUMMARY_KEYS) and all(sync.is_sha256(v) for v in mine.values())
    assert sync.summary(db, key) == mine
    rows = sync.document_summaries(db)
    assert rows and all(tuple(row) == sync.DOCUMENT_SUMMARY_KEYS for row in rows)
    assert sync.vector_shas(db, key) and sync.vector_shas(db, None) == []
    collector = db.execute("INSERT INTO collectors(name, created_at) VALUES('합성', '2026-10-26')").lastrowid
    db.execute('INSERT INTO collector_documents SELECT ?, id, id FROM documents', (collector,))
    db.execute('INSERT INTO collector_versions(collector_id, local_version_id, version_id) SELECT ?, id, id '
               'FROM versions', (collector,))
    assert sync.summary(db, key, collector_id=collector) == mine
    assert sync.document_summaries(db, collector_id=collector, after=rows[0]['local_document_id'], limit=2) == rows[1:3]
    assert sync.summary(db, key, collector_id=collector + 1) != mine            # another collector: nothing
    # a vector removed on one side changes vectors_sha256 only
    db.execute('DELETE FROM chunk_vectors WHERE text_sha256 = ?', (sync.vector_shas(db, key)[0],))
    changed = sync.summary(db, key)
    assert changed['documents_sha256'] == mine['documents_sha256']
    assert changed['vectors_sha256'] != mine['vectors_sha256']


@pytest.mark.parametrize('url, parts', [
    ('https://srv.example.invalid/workmcp', ('https', 'srv.example.invalid', 443, '/workmcp')),
    ('https://SRV.example.invalid:8443/workmcp/', ('https', 'srv.example.invalid', 8443, '/workmcp')),
    ('http://127.0.0.1:48765/workmcp', ('http', '127.0.0.1', 48765, '/workmcp')),
    ('http://[::1]:48765', ('http', '::1', 48765, '')),
])
def test_parse_url(url, parts):
    assert sync.parse_url(url) == sync.UrlParts(*parts)


@pytest.mark.parametrize('url', ['', 'ftp://x', 'srv.example.invalid', 'https://', 'https://u:p@x/', 'https://x/?a=1',
                                 'https://x/#f', 'https://x:0/', 'https://x:99999/', 'https://x:ab/', 'https://::1/',
                                 'https://[::1/', 'https://x y/', 'https://x/\n'])
def test_parse_url_refuses(url):
    with pytest.raises(ValueError):
        sync.parse_url(url)


def test_url_rules_and_endpoints():
    assert sync.url_problem('https://srv.example.invalid/workmcp') is None
    assert 'loopback' in sync.url_problem('http://srv.example.invalid/workmcp', allow_insecure_loopback=True)
    assert 'allow-insecure-loopback' in sync.url_problem('http://127.0.0.1:1/workmcp')
    assert sync.url_problem('http://127.0.0.1:1/workmcp', allow_insecure_loopback=True) is None
    base = 'https://srv.example.invalid/workmcp/'
    assert sync.endpoint(base, 'state') == 'https://srv.example.invalid/workmcp/push/state'
    assert sync.endpoint(base, 'state', {'full': 1, 'after': 500}) == \
        'https://srv.example.invalid/workmcp/push/state?full=1&after=500'
    assert sync.endpoint(base, 'vectors', {'model_key': H}).endswith(f'push/state/vectors?model_key={H}')
    assert sync.endpoint(base, 'batch').endswith('/workmcp/push/batch')
    for name, query in (('other', None), ('state', {'after': '1&x=2'}), ('state', {'a b': 1})):
        with pytest.raises(ValueError):
            sync.endpoint(base, name, query)


def test_windows_normcase_matches_ntpath():
    import ntpath
    for path in ('C:/Synthetic/A.TXT', 'c:\\합성\\Ä.md', '\\\\server\\Share/x'):
        assert sync.windows_normcase(path) == ntpath.normcase(path)

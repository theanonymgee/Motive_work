"""Binding check (design M1 §6): stored units and quotes re-derive from the current files. Synthetic files only."""
import hashlib
import json
import os

import pytest

from fixtures import LINES_KO, SENTINEL, make_text
from test_collect import add_root, document, rewrite, scan, versions
from workmcp import binding, locator, parsers, paths, store


@pytest.fixture
def db(workmcp_home):
    home = paths.ensure_home(paths.resolve_home())
    connection = store.open_home(home)
    yield connection
    connection.close()


@pytest.fixture
def bundle(db, tmp_path):
    root = add_root(db, tmp_path/'src')
    expected = make_text.write(tmp_path/'src')
    scan(db, root)
    return root, expected


def unverifiable(report):
    return {key: n for key, n in report['versions']['unverifiable'].items() if n}


def test_fresh_bundle_binds_100_percent(db, bundle):
    _, expected = bundle
    report = binding.verify(db)
    oks = sum(item.status == 'ok' for item in expected)
    assert report['ok'] and report['failures'] == [] and report['notice'] == []
    assert report['versions'] | {'unverifiable': None} == {'total': len(expected), 'checked': oks, 'ok': oks,
                                                            'failed': 0, 'unverifiable': None}
    assert unverifiable(report) == {'skipped_not_ok': len(expected) - oks}  # the two failed edge files
    assert report['units']['bound'] == report['units']['total'] == sum(item.unit_count for item in expected) > 0
    assert report['units']['rate'] == 1.0
    assert report['scope'] == binding.SCOPE and report['parser_versions']['.txt'] == 'text-1'


def test_modified_file_is_stale_not_failed_and_notice_says_scan(db, bundle, tmp_path):
    path = tmp_path/'src'/'docs'/'notes_utf8.txt'
    rewrite(path, path.read_bytes() + b'\nsynthetic extra line')
    report = binding.verify(db)
    assert report['ok'] and unverifiable(report)['stale'] == 1
    assert len(report['notice']) == 1 and 'workmcp scan' in report['notice'][0]
    single = binding.verify_version(db, versions(db, document(db, path)[0])[0][0], {})
    assert single['result'] == 'stale' and 'run scan' in single['note']


def test_same_size_change_is_stale_too(db, bundle, tmp_path):
    path = tmp_path/'src'/'docs'/'ascii.txt'
    data = path.read_bytes()
    rewrite(path, data.replace(b'plain', b'PLAIN'))
    assert unverifiable(binding.verify(db))['stale'] == 1


def first_unit(db, rel, tmp_path):
    version = versions(db, document(db, tmp_path/'src'/rel)[0])[-1][0]
    return version, db.execute('SELECT id, ordinal FROM units WHERE version_id=? ORDER BY ordinal LIMIT 1',
                               (version,)).fetchone()


def test_tampered_unit_text_is_detected(db, bundle, tmp_path):
    version, (unit_id, ordinal) = first_unit(db, 'docs/notes_utf8.txt', tmp_path)
    db.execute("UPDATE units SET text = text || ' 변조' WHERE id=?", (unit_id,))
    report = binding.verify(db)
    assert not report['ok'] and report['versions']['failed'] == 1
    assert report['units']['bound'] == report['units']['total'] - 1
    [failure] = report['failures']
    assert (failure['version_id'], failure['ordinal'], failure['problem']) == (version, ordinal, 'unit_text_mismatch')
    assert failure['unit_label'] == '줄 1' and SENTINEL not in json.dumps(report, ensure_ascii=False)


def test_tampered_unit_hash_is_detected(db, bundle, tmp_path):
    _, (unit_id, _) = first_unit(db, 'docs/notes_utf8.txt', tmp_path)
    db.execute("UPDATE units SET text_sha256 = ? WHERE id=?", ('f' * 64, unit_id))
    assert [f['problem'] for f in binding.verify(db)['failures']] == ['unit_text_sha256_mismatch']


def test_tampered_unit_locator_is_detected(db, bundle, tmp_path):
    version, (unit_id, ordinal) = first_unit(db, 'docs/table_utf8.csv', tmp_path)
    db.execute("""UPDATE units SET locator_json = '{"columns": [1, 2, 3], "kind": "cell", "row": 99}' WHERE id=?""",
               (unit_id,))
    report = binding.verify(db)
    [failure] = report['failures']
    assert (failure['ordinal'], failure['problem'], failure['unit_label']) == (ordinal, 'unit_locator_mismatch',
                                                                              '행 99 열 1,2,3')


def test_missing_and_extra_units_are_detected(db, bundle, tmp_path):
    version, (unit_id, _) = first_unit(db, 'docs/notes_cp949.txt', tmp_path)
    db.execute('DELETE FROM units WHERE id=?', (unit_id,))
    problems = {f['problem'] for f in binding.verify(db)['failures']}
    assert 'unit_count_mismatch' in problems


def test_parser_version_mismatch_is_unverifiable_until_reparse(db, bundle, tmp_path):
    root, _ = bundle
    doc = document(db, tmp_path/'src'/'docs'/'notes_utf8.txt')[0]
    db.execute("UPDATE versions SET parser_version='text-0' WHERE document_id=?", (doc,))
    report = binding.verify(db)
    assert report['ok'] and unverifiable(report)['parser_version_mismatch'] == 1
    assert scan(db, root)['total']['reparsed'] == 0  # without --reparse nothing changes
    result = scan(db, root, reparse=True)
    assert result['total']['reparsed'] == 1 and result['total']['parsed_ok'] == 1
    report = binding.verify(db)
    assert report['ok'] and 'parser_version_mismatch' not in unverifiable(report)
    assert versions(db, doc)[0][1] == 1  # re-parsed in place, no new version


def test_missing_file_is_reported(db, bundle, tmp_path):
    (tmp_path/'src'/'docs'/'ascii.txt').unlink()
    report = binding.verify(db)
    assert report['ok'] and unverifiable(report)['missing'] == 1


def test_version_history_is_checked_with_one_read_per_document(db, tmp_path, monkeypatch):
    root = add_root(db, tmp_path/'src')
    path = tmp_path/'src'/'a.txt'
    original = '\n'.join(LINES_KO).encode('utf-8')
    path.write_bytes(original)
    scan(db, root)
    rewrite(path, b'synthetic B\n')
    scan(db, root)
    rewrite(path, original)
    scan(db, root)
    reads, parses = [], []
    real_read, real_parse = binding.collect.read_source, parsers.parse
    monkeypatch.setattr(binding.collect, 'read_source', lambda p: reads.append(p) or real_read(p))
    monkeypatch.setattr(parsers, 'parse', lambda *a: parses.append(a) or real_parse(*a))
    report = binding.verify(db)
    assert report['versions']['ok'] == 2 and unverifiable(report) == {'stale': 1}  # A ok, B stale, A ok
    assert report['notice'] == []  # the latest version is not stale
    assert len(reads) == 1 and len(parses) == 1  # A parsed once for both A versions


def test_scope_by_root_and_document(db, tmp_path):
    one, two = add_root(db, tmp_path/'one'), add_root(db, tmp_path/'two')
    for name in ('one', 'two'):
        (tmp_path/name/'a.txt').write_bytes(f'synthetic {name}\n'.encode('utf-8'))
    scan(db, one, two)
    assert binding.verify(db, root_ids=[one[0]])['versions']['total'] == 1
    doc = document(db, tmp_path/'two'/'a.txt')[0]
    assert binding.verify(db, document_ids=[doc])['versions']['ok'] == 1
    assert binding.verify(db, root_ids=[one[0]], document_ids=[doc])['versions']['total'] == 0


def add_evidence(db, version_id, quote, *, tamper=None):
    doc, sha = db.execute('SELECT document_id, sha256 FROM versions WHERE id=?', (version_id,)).fetchone()
    path = db.execute('SELECT path FROM documents WHERE id=?', (doc,)).fetchone()[0]
    locator_json, text = db.execute('SELECT locator_json, text FROM units WHERE version_id=? ORDER BY ordinal LIMIT 1',
                                    (version_id,)).fetchone()
    obj = locator.make(doc, path, sha, json.loads(locator_json), text, locator.quote_range(text, quote))
    if tamper:
        obj = tamper(obj)
    assertion = db.execute("INSERT INTO assertions(kind, statement, recorded_from, status, source_kind, recorded_by) "
                           "VALUES('fact', '합성 사실', '2026-10-23T09:00:00+09:00', 'active', 'evidence', 'test')"
                           ).lastrowid
    db.execute('INSERT INTO evidence(assertion_id, version_id, locator_json, quote, quote_sha256) VALUES(?,?,?,?,?)',
               (assertion, version_id, json.dumps(obj, ensure_ascii=False), quote,
                hashlib.sha256(quote.encode('utf-8')).hexdigest()))
    return obj


def test_evidence_quote_is_rechecked(db, bundle, tmp_path):
    version = versions(db, document(db, tmp_path/'src'/'docs'/'notes_utf8.txt')[0])[0][0]
    add_evidence(db, version, '주간 회의록')
    report = binding.verify(db)
    assert report['ok'] and report['evidence_checked'] == 1
    add_evidence(db, version, '주간 회의록', tamper=lambda obj: {**obj, 'chars': [0, 3], 'text_sha256': 'a' * 64})
    add_evidence(db, version, '합성', tamper=lambda obj: {**obj, 'unit': {'kind': 'line', 'line': 999}})
    report = binding.verify(db)
    assert not report['ok'] and report['evidence_checked'] == 3
    assert [f['problem'] for f in report['failures']] == ['evidence:locator_text_mismatch', 'evidence:unit_not_found']


def test_validate_locator_against_store(db, bundle, tmp_path):
    version_id, doc, sha = db.execute(
        'SELECT v.id, v.document_id, v.sha256 FROM versions v JOIN documents d ON d.id = v.document_id '
        "WHERE d.path LIKE '%notes_utf8.txt'").fetchone()
    path = str(tmp_path/'src'/'docs'/'notes_utf8.txt')
    unit_id, locator_json, text = db.execute('SELECT id, locator_json, text FROM units WHERE version_id=? AND ordinal=2',
                                             (version_id,)).fetchone()
    obj = locator.make(doc, path, sha, json.loads(locator_json), text, locator.quote_range(text, '가상 거래처 A'))
    assert binding.validate_locator(db, obj) == {'version_id': version_id, 'unit_id': unit_id, 'text': '가상 거래처 A'}
    for change, code in [({'sha256': 'b' * 64}, 'version_not_found'), ({'unit': {'kind': 'line', 'line': 77}},
                                                                       'unit_not_found'),
                         ({'chars': [0, 2]}, 'locator_text_mismatch'), ({'v': 2}, 'locator_version_unsupported')]:
        with pytest.raises(locator.LocatorError) as info:
            binding.validate_locator(db, {**obj, **change})
        assert info.value.code == code


def test_binding_reads_sources_only_through_read_source():
    source = open(binding.__file__, encoding='utf-8').read()
    assert 'open(' not in source.replace('collect.read_source', '') and 'collect.read_source(path)' in source
    assert os.path.basename(binding.__file__) == 'binding.py'

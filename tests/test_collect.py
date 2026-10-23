"""Incremental collector (design M1 §2.4, §3). Synthetic TXT/CSV files in temporary folders only.

The bundle tests use the full synthetic bundle (``fixtures.bundle``: nine parsers, edge cases and
noise); the single-behaviour tests use one or two small text files.
"""
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fixtures import LINES_KO, SENTINEL, bundle, make_ooxml
from workmcp import cli, collect, config, parsers, paths, store

SETTINGS = config.ScanSettings()
CHANGE_KEYS = ('added', 'changed', 'touched', 'restored', 'deleted', 'unchanged', 'unstable', 'reparsed')


@pytest.fixture
def home(workmcp_home):
    return paths.ensure_home(paths.resolve_home())


@pytest.fixture
def db(home):
    connection = store.open_home(home)
    yield connection
    connection.close()


@pytest.fixture
def calls(monkeypatch):
    """Counts original reads and parser calls made by the collector."""
    counts = {'read': 0, 'stream': 0, 'parse': 0}
    real_read, real_stream, real_parse = collect.read_source, collect.stream_sha256, parsers.parse

    def read(path):
        counts['read'] += 1
        return real_read(path)

    def stream(path):
        counts['stream'] += 1
        return real_stream(path)

    def parse(data, ext, limits):
        counts['parse'] += 1
        return real_parse(data, ext, limits)

    monkeypatch.setattr(collect, 'read_source', read)
    monkeypatch.setattr(collect, 'stream_sha256', stream)
    monkeypatch.setattr(parsers, 'parse', parse)
    return counts


def add_root(db, path: Path, exclude=()):
    path.mkdir(parents=True, exist_ok=True)
    spec = config.RootSpec(path.resolve(), str(path), tuple(exclude))
    return config.sync_roots(db, [spec], from_config=False)[spec.key], spec


def scan(db, *roots, settings=SETTINGS, **options):
    events = []
    result = collect.scan(db, list(roots), settings, on_event=events.append, **options)
    result['events'] = events
    return result


def changes(result) -> dict:
    return {key: result['total'][key] for key in CHANGE_KEYS if result['total'][key]}


def document(db, path: Path):
    return db.execute('SELECT id, status, skip_reason, deleted_at FROM documents WHERE path_key=?',
                      (os.path.normcase(str(path.resolve())),)).fetchone()


def versions(db, doc_id):
    return db.execute('SELECT id, seq, sha256, size, mtime_ns, superseded_at, parse_status, parse_error, unit_count '
                      'FROM versions WHERE document_id=? ORDER BY seq', (doc_id,)).fetchall()


def units(db, version_id):
    return db.execute('SELECT ordinal, kind, locator_json, text, text_sha256 FROM units WHERE version_id=? '
                      'ORDER BY ordinal', (version_id,)).fetchall()


def rewrite(path: Path, data: bytes, seconds=2):
    """New content with an mtime that surely differs from the previous one."""
    before = path.stat().st_mtime_ns if path.exists() else 0
    path.write_bytes(data)
    stamp = max(path.stat().st_mtime_ns, before + seconds * 10**9)
    os.utime(path, ns=(stamp, stamp))


def text_root(db, tmp_path, lines=LINES_KO):
    root = add_root(db, tmp_path/'src')
    (tmp_path/'src'/'a.txt').write_bytes('\n'.join(lines).encode('utf-8'))
    return root


def bundle_root(db, tmp_path):
    """The full synthetic bundle (all nine parsers, edge cases and noise) as one source root."""
    root = add_root(db, tmp_path/'src')
    bundle.write_bundle(tmp_path/'src')
    return root, bundle.documents()


def stored_by_parser(db) -> dict:
    """``{parser: {'files', 'units', 'statuses'}}`` of the latest versions, as ``bundle.totals`` counts."""
    out = {}
    for parser, key, files, units_ in db.execute(
            "SELECT v.parser, v.parse_status || COALESCE('/' || v.parse_error, ''), COUNT(*), SUM(v.unit_count) "
            'FROM versions v WHERE v.superseded_at IS NULL GROUP BY 1, 2'):
        entry = out.setdefault(parser, {'files': 0, 'units': 0, 'statuses': {}})
        entry['files'] += files
        entry['units'] += units_
        entry['statuses'][key] = files
    return dict(sorted(out.items()))


def check_document(db, src: Path, item, data: bytes, seq=1):
    """The latest version of one bundle file matches its ``Expected`` exactly."""
    doc = document(db, src/item.rel_path)
    assert doc is not None and doc[1:3] == ('active', None), item.rel_path
    version = versions(db, doc[0])[-1]
    assert version[1:4] == (seq, collect.hashlib.sha256(data).hexdigest(), len(data)), item.rel_path
    assert version[6:9] == (item.status, item.error, item.unit_count), item.rel_path
    parser, encoding = db.execute('SELECT parser, encoding FROM versions WHERE id=?', (version[0],)).fetchone()
    assert parser == item.parser and (item.status != 'ok' or encoding == item.encoding), item.rel_path
    stored = units(db, version[0])
    assert len(stored) == item.unit_count, item.rel_path
    for ordinal, (kind, unit, text) in item.units_sample.items():
        assert stored[ordinal - 1][:4] == (ordinal, kind, parsers.unit_json(unit), text), (item.rel_path, ordinal)
    assert [row[0] for row in stored] == list(range(1, len(stored) + 1))
    assert all(row[4] == collect.hashlib.sha256(row[3].encode('utf-8')).hexdigest() for row in stored)
    return version


TOTAL_KEYS = ('parsed_ok', 'reused', 'failed', 'unsupported', 'encrypted', 'too_large', 'units')


def test_first_scan_adds_every_supported_file(db, tmp_path):
    root, pairs = bundle_root(db, tmp_path)
    want = bundle.totals(pairs)
    result = scan(db, root)
    total = result['total']
    assert changes(result) == {'added': want['files']}
    assert total['seen'] == total['new_versions'] == want['files'] == 51
    assert {key: total[key] for key in TOTAL_KEYS} == {key: want[key] for key in TOTAL_KEYS}
    assert want['reused'] == 1 and want['units'] > 0 and len(want['by_parser']) == 9
    noise = bundle.noise_counts()
    assert (total['excluded'], total['ignored']) == (noise['excluded'], noise['ignored'])
    assert (total['links_skipped'], total['errors'], total['locked'], total['skipped']) == (0, 0, 0, 0)
    assert stored_by_parser(db) == want['by_parser']  # per format: parse_status/code counts and unit totals
    for item, data in pairs:
        check_document(db, tmp_path/'src', item, data)
    assert db.execute('SELECT COUNT(*) FROM documents').fetchone()[0] == want['files']  # noise has no row
    row = db.execute('SELECT status, partial, finished_at, counts_json FROM scans').fetchone()
    assert row[:2] == ('done', 0) and row[2] and json.loads(row[3])['total'] == total


def test_unchanged_files_are_skipped_by_size_and_mtime(db, tmp_path, calls):
    root, pairs = bundle_root(db, tmp_path)
    scan(db, root)
    calls.update(read=0, parse=0)
    result = scan(db, root)
    assert changes(result) == {'unchanged': len(pairs)}
    assert calls == {'read': 0, 'stream': 0, 'parse': 0}
    assert result['total']['new_versions'] == 0 and result['total']['units'] == 0
    assert db.execute('SELECT COUNT(*) FROM versions').fetchone()[0] == len(pairs)


CHANGED = 'docs/report.docx'
DELETED = 'mail/plain_utf8.eml'
CHANGED_TEXT = f'{SENTINEL} 바뀐 보고서 한 문단'


def other_docx() -> bytes:
    return make_ooxml.docx_package(make_ooxml.w_p(make_ooxml.w_run(CHANGED_TEXT)))


def test_bundle_change_delete_restore_and_revert(db, tmp_path, calls):
    """Whole bundle: A->B is changed (seq 2), a removed file is deleted, B->A is a third version with the
    first one's units and the removed file comes back as restored; everything else stays unchanged."""
    src = tmp_path/'src'
    root, pairs = bundle_root(db, tmp_path)
    data = {item.rel_path: payload for item, payload in pairs}
    n = len(pairs)
    scan(db, root)
    assert changes(scan(db, root)) == {'unchanged': n}

    rewrite(src/CHANGED, other_docx())
    (src/DELETED).unlink()
    calls.update(read=0, parse=0)
    result = scan(db, root)
    assert changes(result) == {'changed': 1, 'deleted': 1, 'unchanged': n - 2}
    assert calls['read'] == 1 and calls['parse'] == 1 and result['total']['new_versions'] == 1
    first, second = versions(db, document(db, src/CHANGED)[0])
    assert (first[1], second[1]) == (1, 2) and first[5] and second[5] is None
    assert [u[3] for u in units(db, second[0])] == [CHANGED_TEXT]
    doc = document(db, src/DELETED)
    assert doc[1] == 'deleted' and doc[3] is not None and len(versions(db, doc[0])) == 1

    rewrite(src/CHANGED, data[CHANGED])
    (src/DELETED).write_bytes(data[DELETED])
    calls.update(read=0, parse=0)
    result = scan(db, root)
    assert changes(result) == {'changed': 1, 'restored': 1, 'unchanged': n - 2}
    assert result['total']['reused'] == 2 and calls == {'read': 2, 'stream': 0, 'parse': 0}  # units copied
    rows = versions(db, document(db, src/CHANGED)[0])
    assert [r[1] for r in rows] == [1, 2, 3] and rows[0][2] == rows[2][2] != rows[1][2]
    assert [u[:4] for u in units(db, rows[2][0])] == [u[:4] for u in units(db, rows[0][0])]
    assert rows[1][5] and rows[2][5] is None
    restored = document(db, src/DELETED)
    assert restored[1:] == ('active', None, None)
    rows = versions(db, restored[0])
    assert [r[1] for r in rows] == [1, 2] and rows[0][2] == rows[1][2] and rows[0][5] and rows[1][5] is None
    by_path = {item.rel_path: item for item, _ in pairs}
    check_document(db, src, by_path[CHANGED], data[CHANGED], seq=3)
    check_document(db, src, by_path[DELETED], data[DELETED], seq=2)
    assert stored_by_parser(db) == bundle.totals(pairs)['by_parser']
    assert changes(scan(db, root)) == {'unchanged': n}


def test_touched_file_with_same_hash_updates_mtime_only(db, tmp_path, calls):
    root = text_root(db, tmp_path)
    scan(db, root)
    path = tmp_path/'src'/'a.txt'
    stamp = path.stat().st_mtime_ns + 5 * 10**9
    os.utime(path, ns=(stamp, stamp))
    calls.update(parse=0)
    assert changes(scan(db, root)) == {'touched': 1}
    [version] = versions(db, document(db, path)[0])
    assert version[4] == stamp and calls['parse'] == 0
    assert changes(scan(db, root)) == {'unchanged': 1}


def test_changed_file_creates_new_version_and_supersedes_old(db, tmp_path):
    root = text_root(db, tmp_path)
    scan(db, root)
    path = tmp_path/'src'/'a.txt'
    rewrite(path, '\n'.join(LINES_KO[:2]).encode('utf-8'))
    assert changes(scan(db, root)) == {'changed': 1}
    first, second = versions(db, document(db, path)[0])
    assert (first[1], second[1]) == (1, 2)
    assert first[5] is not None and second[5] is None
    assert [u[3] for u in units(db, second[0])] == list(LINES_KO[:2])
    assert len(units(db, first[0])) == len(LINES_KO)  # history kept


def test_content_returning_to_an_earlier_hash_is_a_third_version_with_reused_units(db, tmp_path, calls):
    root = text_root(db, tmp_path)
    path = tmp_path/'src'/'a.txt'
    original = path.read_bytes()
    scan(db, root)
    rewrite(path, b'synthetic B version\n')
    scan(db, root)
    rewrite(path, original)
    calls.update(parse=0)
    result = scan(db, root)
    assert changes(result) == {'changed': 1}
    assert result['total']['reused'] == 1 and calls['parse'] == 0
    rows = versions(db, document(db, path)[0])
    assert [r[1] for r in rows] == [1, 2, 3]
    assert rows[0][2] == rows[2][2] != rows[1][2]
    assert [u[:4] for u in units(db, rows[2][0])] == [u[:4] for u in units(db, rows[0][0])]
    assert rows[0][5] and rows[1][5] and rows[2][5] is None


def test_missing_file_is_marked_deleted_and_history_is_kept(db, tmp_path):
    root = text_root(db, tmp_path)
    scan(db, root)
    path = tmp_path/'src'/'a.txt'
    counts = [db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('versions', 'units')]
    path.unlink()
    result = scan(db, root)
    assert changes(result) == {'deleted': 1}
    assert [e['event'] for e in result['events']] == ['deleted']
    doc = document(db, path)
    assert doc[1] == 'deleted' and doc[3] is not None
    assert [db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('versions', 'units')] == counts
    assert changes(scan(db, root)) == {}  # stays deleted, judged once


def test_reappearing_file_is_restored_with_a_new_version(db, tmp_path, calls):
    root = text_root(db, tmp_path)
    path = tmp_path/'src'/'a.txt'
    original = path.read_bytes()
    scan(db, root)
    path.unlink()
    scan(db, root)
    path.write_bytes(original)
    calls.update(parse=0)
    result = scan(db, root)
    assert changes(result) == {'restored': 1}
    assert result['total']['reused'] == 1 and calls['parse'] == 0
    doc = document(db, path)
    assert doc[1:] == ('active', None, None)
    rows = versions(db, doc[0])
    assert [r[1] for r in rows] == [1, 2] and rows[0][2] == rows[1][2]


def test_rename_is_delete_plus_add_with_unit_reuse(db, tmp_path, calls):
    root = text_root(db, tmp_path)
    old = tmp_path/'src'/'a.txt'
    scan(db, root)
    new = tmp_path/'src'/'moved'/'b.txt'
    new.parent.mkdir()
    old.replace(new)
    calls.update(parse=0)
    result = scan(db, root)
    assert changes(result) == {'added': 1, 'deleted': 1}
    assert result['total']['reused'] == 1 and calls['parse'] == 0
    assert document(db, old)[1] == 'deleted'
    [moved] = versions(db, document(db, new)[0])
    [before] = versions(db, document(db, old)[0])
    assert moved[1] == 1 and moved[2] == before[2]
    assert [u[:4] for u in units(db, moved[0])] == [u[:4] for u in units(db, before[0])]


def test_same_content_in_another_document_reuses_units(db, tmp_path, calls):
    root = text_root(db, tmp_path)
    (tmp_path/'src'/'copy.md').write_bytes((tmp_path/'src'/'a.txt').read_bytes())  # .txt and .md: same parser
    (tmp_path/'src'/'copy.csv').write_bytes((tmp_path/'src'/'a.txt').read_bytes())  # csv: another parser
    result = scan(db, root)
    assert result['total']['reused'] == 1 and result['total']['parsed_ok'] == 2 and calls['parse'] == 2


def test_partial_root_scan_only_deletes_inside_that_root(db, tmp_path):
    first, second = add_root(db, tmp_path/'one'), add_root(db, tmp_path/'two')
    for name in ('one', 'two'):
        for file in ('a.txt', 'b.txt'):
            (tmp_path/name/file).write_bytes(f'{SENTINEL} {name} {file}\n'.encode('utf-8'))
    scan(db, first, second)
    (tmp_path/'one'/'a.txt').unlink()
    (tmp_path/'two'/'a.txt').unlink()
    result = scan(db, first, partial=True)
    assert changes(result) == {'deleted': 1, 'unchanged': 1}
    assert document(db, tmp_path/'one'/'a.txt')[1] == 'deleted'
    assert document(db, tmp_path/'two'/'a.txt')[1] == 'active'
    assert db.execute('SELECT partial FROM scans ORDER BY id DESC').fetchone() == (1,)
    assert result['mode'] == 'partial'


def test_locked_file_is_recorded_as_locked_and_retried(db, tmp_path, monkeypatch):
    root = text_root(db, tmp_path)
    path = tmp_path/'src'/'a.txt'
    real = collect.read_source

    def locked_once(source):
        monkeypatch.setattr(collect, 'read_source', real)
        raise collect.SourceLocked()

    monkeypatch.setattr(collect, 'read_source', locked_once)
    result = scan(db, root)
    assert changes(result) == {'added': 1} and result['total']['locked'] == 1
    doc = document(db, path)
    [version] = versions(db, doc[0])
    assert version[1:4] == (1, collect.LOCKED_SHA256, path.stat().st_size) and version[6] == 'locked'
    result = scan(db, root)  # same size and mtime, but a locked version is always retried
    assert changes(result) == {'reparsed': 1} and result['total']['parsed_ok'] == 1
    [version] = versions(db, doc[0])
    assert version[1:3] == (1, collect.hashlib.sha256(path.read_bytes()).hexdigest())
    assert version[6:9] == ('ok', None, len(LINES_KO)) and len(units(db, version[0])) == len(LINES_KO)
    assert changes(scan(db, root)) == {'unchanged': 1}


def test_locked_known_file_keeps_its_version(db, tmp_path, monkeypatch):
    root = text_root(db, tmp_path)
    path = tmp_path/'src'/'a.txt'
    scan(db, root)
    rewrite(path, b'synthetic new content\n')
    monkeypatch.setattr(collect, 'read_source', lambda source: (_ for _ in ()).throw(collect.SourceLocked()))
    result = scan(db, root)
    assert changes(result) == {} and result['total']['locked'] == 1
    assert len(versions(db, document(db, path)[0])) == 1 and document(db, path)[1] == 'active'  # not deleted


def test_os_sharing_violation_is_reported_as_locked(tmp_path):
    """A real share-deny lock (Windows) reaches the collector as SourceLocked."""
    if os.name != 'nt':
        pytest.skip('Windows share modes')
    import _winapi
    path = tmp_path/'held.txt'
    path.write_bytes(b'synthetic')
    handle = _winapi.CreateFile(str(path), _winapi.GENERIC_READ, 0, 0, _winapi.OPEN_EXISTING, 0, 0)  # no sharing
    try:
        with pytest.raises(collect.SourceLocked):
            collect.read_source(str(path))
        with pytest.raises(collect.SourceLocked):
            collect.stream_sha256(str(path))
    finally:
        _winapi.CloseHandle(handle)


def test_file_changing_during_read_is_skipped_this_round(db, tmp_path, monkeypatch):
    root = text_root(db, tmp_path)
    path = tmp_path/'src'/'a.txt'
    real = collect.read_source

    def changing(source):
        data, sha, after = real(source)
        stamp = after.st_mtime_ns + 3 * 10**9
        os.utime(source, ns=(stamp, stamp))  # a writer touched it while we read
        return data, sha, os.stat(source)

    monkeypatch.setattr(collect, 'read_source', changing)
    result = scan(db, root)
    assert changes(result) == {'unstable': 1}
    assert document(db, path) is None and db.execute('SELECT COUNT(*) FROM versions').fetchone()[0] == 0
    monkeypatch.setattr(collect, 'read_source', real)
    assert changes(scan(db, root)) == {'added': 1}


def test_unstable_known_file_is_not_deleted(db, tmp_path, monkeypatch):
    root = text_root(db, tmp_path)
    path = tmp_path/'src'/'a.txt'
    scan(db, root)
    rewrite(path, b'synthetic being written\n')
    real = collect.read_source

    def changing(source):
        data, sha, after = real(source)
        os.utime(source, ns=(after.st_mtime_ns + 10**9,) * 2)
        return data, sha, os.stat(source)

    monkeypatch.setattr(collect, 'read_source', changing)
    assert changes(scan(db, root)) == {'unstable': 1}
    assert document(db, path)[1] == 'active' and len(versions(db, document(db, path)[0])) == 1


def set_hidden(path: Path):
    if os.name != 'nt':
        pytest.skip('Windows file attributes')
    import subprocess
    subprocess.run(['attrib', '+H', str(path)], check=True, capture_output=True)


def test_excluded_folders_patterns_hidden_and_office_lock_files_are_not_collected(db, tmp_path, calls):
    src = tmp_path/'src'
    root = add_root(db, src, exclude=['drafts/*'])
    files = {'keep.txt': 1, 'node_modules/x.txt': 0, '.git/notes.txt': 0, 'sub/__pycache__/m.txt': 0,
             '~$report.docx': 0, 'tmp-file.tmp': 0, 'Thumbs.db': 0, 'old.bak': 0, 'drafts/d.txt': 0,
             'hidden.txt': 0, 'hidden-dir/inner.txt': 0, 'sub/ok.md': 1}
    for rel in files:
        (src/rel).parent.mkdir(parents=True, exist_ok=True)
        (src/rel).write_bytes(f'{SENTINEL} {rel}\n'.encode('utf-8'))
    set_hidden(src/'hidden.txt')
    set_hidden(src/'hidden-dir')
    result = scan(db, root, settings=replace(SETTINGS, exclude=('*.bak',)))
    collected = {rel for rel in files if document(db, src/rel) is not None}
    assert collected == {rel for rel, keep in files.items() if keep}
    assert result['total']['added'] == 2 and calls['read'] == 2
    # folders: node_modules, .git, __pycache__, hidden-dir; files: ~$, .tmp, Thumbs.db, .bak, drafts/*, hidden
    assert result['total']['excluded'] == 10
    assert not [e for e in result['events'] if 'node_modules' in e['path'] or '.git' in e['path']]


def test_links_are_not_followed(db, tmp_path, calls):
    src = tmp_path/'src'
    root = add_root(db, src)
    outside = tmp_path/'outside'
    outside.mkdir()
    (outside/'secret.txt').write_bytes(b'synthetic outside\n')
    made = 0
    if os.name == 'nt':
        import _winapi
        try:
            _winapi.CreateJunction(str(outside), str(src/'junction'))
            made += 1
        except OSError:
            pass
    for target, name in ((outside, 'dirlink'), (outside/'secret.txt', 'filelink.txt')):
        try:
            os.symlink(target, src/name, target_is_directory=target.is_dir())
            made += 1
        except OSError:  # Windows needs Developer Mode or admin for symlinks
            pass
    if not made:
        pytest.skip('no junction or symlink could be made here')
    (src/'real.txt').write_bytes(b'synthetic real\n')
    result = scan(db, root)
    assert result['total']['links_skipped'] == made
    assert result['total']['added'] == 1 and calls['read'] == 1
    assert db.execute("SELECT COUNT(*) FROM documents WHERE path LIKE '%secret%'").fetchone()[0] == 0


def test_cloud_placeholder_attribute_skips_the_file(db, tmp_path, monkeypatch, calls):
    root = text_root(db, tmp_path)
    path = tmp_path/'src'/'a.txt'
    real = collect.file_attributes
    online_only = {'on': True}

    def attributes(info):
        value = real(info)
        if online_only['on'] and info.st_size == path.stat().st_size:
            value |= collect.FILE_ATTRIBUTE_REPARSE_POINT | paths.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        return value

    monkeypatch.setattr(collect, 'file_attributes', attributes)
    result = scan(db, root)
    assert result['total']['skipped'] == 1 and result['total']['links_skipped'] == 0 and calls['read'] == 0
    assert document(db, path)[1:3] == ('skipped', 'cloud_placeholder')
    assert changes(scan(db, root)) == {}  # still a placeholder: seen, not deleted
    assert document(db, path)[1] == 'skipped'
    reading = scan(db, root, settings=replace(SETTINGS, read_cloud_placeholders=True))
    assert changes(reading) == {'added': 1} and calls['read'] == 1
    assert document(db, path)[1:3] == ('active', None)


def test_too_large_file_is_hashed_but_not_parsed(db, tmp_path, calls):
    root = text_root(db, tmp_path)
    path = tmp_path/'src'/'a.txt'
    result = scan(db, root, settings=replace(SETTINGS, limits=parsers.Limits(max_file_bytes=10)))
    assert changes(result) == {'added': 1} and result['total']['too_large'] == 1
    assert calls == {'read': 0, 'stream': 1, 'parse': 0}
    [version] = versions(db, document(db, path)[0])
    assert version[2] == collect.hashlib.sha256(path.read_bytes()).hexdigest()
    assert version[6:9] == ('too_large', 'file_bytes', 0) and units(db, version[0]) == []


def test_unsupported_extension_creates_no_document(db, tmp_path):
    root = add_root(db, tmp_path/'src')
    for name in ('image.png', 'old.doc', 'mail.msg', 'noext'):
        (tmp_path/'src'/name).write_bytes(b'synthetic')
    result = scan(db, root)
    assert result['total']['ignored'] == 4 and result['total']['seen'] == 0
    assert db.execute('SELECT COUNT(*) FROM documents').fetchone()[0] == 0


def test_failed_file_is_retried_only_when_asked(db, tmp_path, calls):
    root = add_root(db, tmp_path/'src')
    path = tmp_path/'src'/'binary.txt'
    path.write_bytes(b'synthetic\x00binary')
    scan(db, root)
    stamp = path.stat().st_mtime_ns + 10**9
    os.utime(path, ns=(stamp, stamp))
    calls.update(parse=0)
    assert changes(scan(db, root)) == {'touched': 1} and calls['parse'] == 0
    result = scan(db, root, retry_failed=True)
    assert changes(result) == {'reparsed': 1} and calls['parse'] == 1 and result['total']['failed'] == 1
    assert len(versions(db, document(db, path)[0])) == 1


def test_rehash_finds_content_changed_under_the_same_size_and_mtime(db, tmp_path):
    root = text_root(db, tmp_path, lines=['synthetic AAAA'])
    path = tmp_path/'src'/'a.txt'
    scan(db, root)
    info = path.stat()
    path.write_bytes(b'synthetic BBBB')
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))  # a copy tool that keeps the time
    assert changes(scan(db, root)) == {'unchanged': 1}  # documented limit (design §3.1)
    result = scan(db, root, rehash=True)
    assert changes(result) == {'changed': 1} and result['mode'] == 'full+rehash'


def test_unreadable_folder_is_counted_and_its_documents_are_not_deleted(db, tmp_path, monkeypatch):
    root = add_root(db, tmp_path/'src')
    (tmp_path/'src'/'locked-dir').mkdir()
    (tmp_path/'src'/'locked-dir'/'in.txt').write_bytes(b'synthetic inside\n')
    (tmp_path/'src'/'top.txt').write_bytes(b'synthetic top\n')
    scan(db, root)
    real = os.scandir

    def scandir(folder):
        if os.path.basename(folder) == 'locked-dir':
            raise PermissionError(13, 'synthetic denial')
        return real(folder)

    monkeypatch.setattr(collect.os, 'scandir', scandir)
    result = scan(db, root)
    assert result['total']['errors'] == 1 and changes(result) == {'unchanged': 1}
    assert document(db, tmp_path/'src'/'locked-dir'/'in.txt')[1] == 'active'


def test_stat_failure_of_a_listed_folder_or_file_deletes_nothing(db, tmp_path, monkeypatch):
    """Review M1-L6: an entry whose stat fails might be a folder or a file; nothing at or under it is deleted."""
    root = add_root(db, tmp_path/'src')
    (tmp_path/'src'/'sub').mkdir()
    (tmp_path/'src'/'sub'/'in.txt').write_bytes(b'synthetic inside\n')
    (tmp_path/'src'/'f.txt').write_bytes(b'synthetic file\n')
    (tmp_path/'src'/'ok.txt').write_bytes(b'synthetic ok\n')
    assert changes(scan(db, root)) == {'added': 3}
    failing = {'sub', 'f.txt'}
    real = os.scandir

    class Entry:
        def __init__(self, entry):
            self._entry = entry

        def __getattr__(self, name):
            return getattr(self._entry, name)

        def stat(self, **options):
            if self._entry.name in failing:
                raise PermissionError(13, 'synthetic stat denial')
            return self._entry.stat(**options)

    class Listing:
        def __init__(self, listing):
            self._listing = listing

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._listing.close()

        def __iter__(self):
            return (Entry(entry) for entry in self._listing)

    monkeypatch.setattr(collect.os, 'scandir', lambda folder: Listing(real(folder)))
    result = scan(db, root)
    assert result['total']['errors'] == 2 and changes(result) == {'unchanged': 1}
    assert [e['code'] for e in result['events'] if e['event'] == 'error'] == ['stat_failed', 'stat_failed']
    for rel in ('sub/in.txt', 'f.txt', 'ok.txt'):
        assert document(db, tmp_path/'src'/rel)[1] == 'active', rel
    failing.clear()
    assert changes(scan(db, root)) == {'unchanged': 3}
    failing.add('f.txt')
    (tmp_path/'src'/'ok.txt').unlink()  # a real deletion next to the failing entry is still judged
    assert changes(scan(db, root)) == {'unchanged': 1, 'deleted': 1}


@pytest.mark.skipif(os.name != 'nt', reason='case-insensitive names (normcase) are a Windows matter')
def test_case_only_rename_updates_the_stored_path(db, tmp_path, calls):
    """Review M1-L5 (P10): same path_key, so the same document, but documents.path takes the new spelling."""
    root = text_root(db, tmp_path)
    old, new = tmp_path/'src'/'a.txt', tmp_path/'src'/'A.TXT'
    scan(db, root)
    doc_id = document(db, old)[0]
    os.rename(old, new)
    assert changes(scan(db, root)) == {'unchanged': 1} and calls['read'] == 1
    assert db.execute('SELECT id, path FROM documents').fetchall() == [(doc_id, str(new.resolve()))]
    assert document(db, new)[0] == doc_id


def test_file_that_becomes_a_placeholder_after_listing_is_not_read(db, tmp_path, monkeypatch, calls):
    """Review M1-2 (scan side): the attribute is checked again on the stat made just before reading."""
    root = text_root(db, tmp_path)
    path = tmp_path/'src'/'a.txt'
    scan(db, root)
    rewrite(path, b'synthetic changed\n')  # would be read and become a new version
    real = collect.file_attributes
    seen = {'calls': 0}

    def attributes(info):
        seen['calls'] += 1  # 1st: the folder listing, 2nd: the stat before reading
        value = real(info)
        return value | paths.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS if seen['calls'] >= 2 else value

    monkeypatch.setattr(collect, 'file_attributes', attributes)
    calls.update(read=0)
    result = scan(db, root)
    assert calls['read'] == 0 and calls['stream'] == 0 and result['total']['skipped'] == 1
    assert changes(result) == {} and result['total']['new_versions'] == 0
    assert document(db, path)[1] == 'active' and len(versions(db, document(db, path)[0])) == 1


def test_name_that_is_not_utf8_is_an_error_not_a_crash(db, tmp_path):
    root = add_root(db, tmp_path/'src')
    try:
        (tmp_path/'src'/'bad\udc80name.txt').write_bytes(b'synthetic')
    except (OSError, UnicodeError, ValueError):
        pytest.skip('this file system refuses a lone surrogate in a name')
    (tmp_path/'src'/'good.txt').write_bytes(b'synthetic\n')
    result = scan(db, root)
    assert result['total']['errors'] == 1 and result['total']['added'] == 1


def test_commit_every_n_and_interrupted_scan_resumes(db, tmp_path, monkeypatch):
    root = add_root(db, tmp_path/'src')
    for number in range(1, 8):
        (tmp_path/'src'/f'f{number}.txt').write_bytes(f'synthetic file {number}\n'.encode('utf-8'))
    real = collect.read_source
    seen = []

    def failing(source):
        seen.append(source)
        if len(seen) == 5:
            raise RuntimeError('synthetic interruption')
        return real(source)

    monkeypatch.setattr(collect, 'read_source', failing)
    with pytest.raises(RuntimeError):
        scan(db, root, settings=replace(SETTINGS, commit_every=2))
    assert db.execute('SELECT status, note FROM scans').fetchone() == ('aborted', 'RuntimeError')
    assert db.execute('SELECT COUNT(*) FROM documents').fetchone()[0] == 4  # batches of 2 committed
    assert db.execute('SELECT COUNT(*) FROM versions v LEFT JOIN units u ON u.version_id = v.id '
                      'WHERE u.id IS NULL').fetchone()[0] == 0  # never a version without its units
    monkeypatch.setattr(collect, 'read_source', real)
    result = scan(db, root, settings=replace(SETTINGS, commit_every=2))
    assert changes(result) == {'unchanged': 4, 'added': 3}


def test_interrupted_walk_judges_no_deletion(db, tmp_path, monkeypatch):
    root = text_root(db, tmp_path)
    (tmp_path/'src'/'b.txt').write_bytes(b'synthetic b\n')
    scan(db, root)
    (tmp_path/'src'/'b.txt').unlink()
    monkeypatch.setattr(collect._Scan, 'judge_deleted', lambda self, root_id, counts: (_ for _ in ()).throw(
        KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        scan(db, root)
    assert document(db, tmp_path/'src'/'b.txt')[1] == 'active'
    assert db.execute("SELECT status FROM scans ORDER BY id DESC").fetchone() == ('aborted',)


def test_a_scan_left_running_is_marked_aborted(db, tmp_path):
    root = text_root(db, tmp_path)
    db.execute("INSERT INTO scans(started_at, status, roots_json) VALUES('2026-10-23T09:00:00+09:00', 'running', '[]')")
    result = scan(db, root)
    assert [e['code'] for e in result['events'] if e['event'] == 'warning'] == ['stale_running_scan_aborted']
    assert [r[0] for r in db.execute('SELECT status FROM scans ORDER BY id')] == ['aborted', 'done']


def test_events_carry_no_document_text(db, tmp_path):
    root, _ = bundle_root(db, tmp_path)
    result = scan(db, root)
    assert result['events'] and SENTINEL not in json.dumps(result, ensure_ascii=False)
    assert all(set(e) == {'event', 'path', 'sha12', 'status', 'code'} for e in result['events'])
    assert db.execute('SELECT COUNT(*) FROM units WHERE text LIKE ?', (f'%{SENTINEL}%',)).fetchone()[0] > 0


def test_scan_lock_refuses_a_second_scan_and_clears_stale_lock(home, tmp_path, capsys):
    (tmp_path/'src').mkdir()
    (tmp_path/'src'/'a.txt').write_bytes(b'synthetic\n')
    lock, warning = store.acquire_scan_lock(home)
    assert warning is None and json.loads(lock.read_text(encoding='utf-8'))['pid'] == os.getpid()
    assert cli.main(['scan', '--root', str(tmp_path/'src')]) == 3
    err = capsys.readouterr().err
    assert 'another scan is running' in err and str(lock) in err
    assert lock.exists()  # the refused scan does not remove someone else's lock
    old = datetime.now().astimezone() - timedelta(hours=25)
    lock.write_text(json.dumps({'pid': 1, 'started_at': old.isoformat(timespec='seconds')}), encoding='utf-8')
    assert cli.main(['scan', '--root', str(tmp_path/'src')]) == 0
    captured = capsys.readouterr()
    assert 'stale scan lock' in captured.err and 'added 1' in captured.out
    assert not lock.exists()  # released at the end
    store.release_scan_lock(lock)  # releasing twice is harmless


def test_scan_lock_is_released_when_the_scan_fails(home, tmp_path, monkeypatch, capsys):
    (tmp_path/'src').mkdir()
    (tmp_path/'src'/'a.txt').write_bytes(b'synthetic\n')
    monkeypatch.setattr(collect, 'read_source', lambda source: (_ for _ in ()).throw(RuntimeError('synthetic')))
    assert cli.main(['scan', '--root', str(tmp_path/'src')]) == 1
    assert 'scan aborted (RuntimeError)' in capsys.readouterr().err
    assert not store.scan_lock_path(home).exists()


def test_read_source_opens_read_only_and_returns_the_stat_after(tmp_path, monkeypatch):
    path = tmp_path/'a.txt'
    path.write_bytes(b'synthetic bytes')
    modes = []
    real_open = open
    monkeypatch.setattr('builtins.open', lambda file, mode='r', *a, **k: modes.append(mode) or real_open(file, mode, *a, **k))
    data, sha, after = collect.read_source(str(path))
    digest, after2 = collect.stream_sha256(str(path))
    assert data == b'synthetic bytes' and sha == digest == collect.hashlib.sha256(data).hexdigest()
    assert after.st_size == after2.st_size == len(data) and modes == ['rb', 'rb']
    with pytest.raises(FileNotFoundError):
        collect.read_source(str(tmp_path/'nope.txt'))


def test_collector_module_has_no_logging_and_reads_with_rb_only():
    source = Path(collect.__file__).read_text(encoding='utf-8')
    assert 'import logging' not in source and source.count("open(path, 'rb')") == 2
    assert sys.modules['workmcp.collect'] is collect

"""``workmcp scan``/``verify``/``sources`` output, exit codes and logs (design M1 §9); ``status.last_scan``.

Run in-process with a temporary data home (conftest) and the full synthetic bundle (``fixtures.bundle``).
"""
import json
import re
from pathlib import Path

import pytest

from fixtures import SENTINEL, bundle
from workmcp import cli, config, paths, store, tools

UNC = r'\\localhost\C$\synthetic'


@pytest.fixture
def home(workmcp_home):
    return paths.ensure_home(paths.resolve_home())


def write_config(home, *roots, extra=''):
    blocks = ''.join(f"\n[[sources]]\npath = '{root}'\n{extra}" for root in roots)
    config.config_file(home).write_text(blocks, encoding='utf-8')


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


SUMMARY = re.compile(
    r'scan #(\d+) started \S+  roots 1  mode (\S+)\n'
    r'  (.+)\n'
    r'    seen (\d+)  added (\d+)  changed (\d+)  touched (\d+)  restored (\d+)  deleted (\d+)  unchanged (\d+)  '
    r'unstable (\d+)  reparsed (\d+)\n'
    r'    parsed ok (\d+)  reused (\d+)  failed (\d+)  locked (\d+)  too_large (\d+)  unsupported (\d+)  '
    r'encrypted (\d+)  skipped (\d+)\n'
    r'    excluded (\d+)  ignored (\d+)  links_skipped (\d+)  errors (\d+)\n'
    r'total ([\d,]+) files  ([\d,]+) new versions  ([\d,]+) units  [\d.]+s   log: (.+scan-\d{8}\.log)\n$')


def test_scan_cli_summary_format_and_exit_codes(home, tmp_path, capsys):
    src = tmp_path/'src'
    expected = bundle.write_bundle(src)
    want, noise = bundle.totals(bundle.documents()), bundle.noise_counts()
    write_config(home, src)
    code, out, err = run(capsys, 'scan')
    assert (code, err) == (0, '')
    match = SUMMARY.match(out)
    assert match, out
    numbers = [int(n.replace(',', '')) for n in match.groups()[3:-1]]
    n = len(expected)
    assert match.group(2) == 'full' and Path(match.group(3)) == src.resolve()
    assert numbers[:9] == [n, n, 0, 0, 0, 0, 0, 0, 0]                 # seen added ... reparsed
    assert numbers[9:17] == [want['parsed_ok'], want['reused'], want['failed'], 0, 0, want['unsupported'],
                             want['encrypted'], 0]                     # parsed ok ... skipped
    assert numbers[17:21] == [noise['excluded'], noise['ignored'], 0, 0]
    assert numbers[21:23] == [n, n] and numbers[23] == sum(item.unit_count for item in expected)
    log = Path(match.groups()[-1])
    assert log.is_file() and log.parent == home/'logs'

    code, out, _ = run(capsys, 'scan')
    numbers = [int(x.replace(',', '')) for x in SUMMARY.match(out).groups()[3:-1]]
    assert code == 0 and numbers[0] == numbers[6] == n and numbers[22] == 0   # all unchanged, no new version

    (src/'docs'/'ascii.txt').unlink()
    code, out, _ = run(capsys, 'scan', '--root', str(src), '--rehash')
    match = SUMMARY.match(out)
    assert code == 0 and match.group(2) == 'partial+rehash' and int(match.group(9)) == 1   # deleted 1


@pytest.mark.parametrize('case, code, message', [
    ('no_sources', 4, 'no sources configured'),
    ('missing_root', 4, 'source_root_missing'),
    ('relative_root', 4, 'source_root_relative'),
    ('network_root', 4, 'source_root_network'),
    ('overlap', 4, 'source_roots_overlap'),
    ('root_in_home', 4, 'source_root_inside_home'),
    ('bad_config', 4, 'unknown [scan] key'),
    ('bad_home', 2, 'home_relative'),
    ('locked', 3, 'another scan is running'),
])
def test_scan_cli_refusals(home, tmp_path, capsys, monkeypatch, case, code, message):
    (tmp_path/'src'/'sub').mkdir(parents=True)
    argv = ['scan']
    if case == 'missing_root':
        argv += ['--root', str(tmp_path/'nope')]
    elif case == 'relative_root':
        argv += ['--root', 'relative\\x']
    elif case == 'network_root':
        argv += ['--root', UNC]
    elif case == 'overlap':
        write_config(home, tmp_path/'src', tmp_path/'src'/'sub')
    elif case == 'root_in_home':
        argv += ['--root', str(home/'logs')]
    elif case == 'bad_config':
        config.config_file(home).write_text('[scan]\nmax_file_gb = 1\n', encoding='utf-8')
    elif case == 'bad_home':
        monkeypatch.setenv('WORKMCP_HOME', 'relative-home')
    elif case == 'locked':
        store.acquire_scan_lock(home)
        argv += ['--root', str(tmp_path/'src')]
    result, out, err = run(capsys, *argv)
    assert (result, out) == (code, '')
    assert message in err
    if case != 'locked':
        assert not store.scan_lock_path(home).exists()
    if case == 'bad_home':
        assert not Path('relative-home').exists()


def test_scan_warnings_go_to_stderr(home, tmp_path, capsys, monkeypatch):
    synced = tmp_path/'OneDrive - 가상회사'/'docs'
    synced.mkdir(parents=True)
    (synced/'a.txt').write_bytes(b'synthetic\n')
    code, out, err = run(capsys, 'scan', '--root', str(synced))
    assert code == 0 and 'sync folder' in err and out.startswith('scan #1')

    share = tmp_path/'share'
    share.mkdir()
    (share/'b.txt').write_bytes(b'synthetic\n')
    monkeypatch.setattr(config, '_is_dir', lambda path: True)       # stands in for a file server: no SMB in tests
    monkeypatch.setattr(config, '_resolve', lambda path: share)
    write_config(home, UNC, extra='allow_network = true\n')
    code, out, err = run(capsys, 'scan')
    assert code == 0 and 'on the network' in err and 'added 1' in out


def test_scan_output_and_log_carry_no_document_text(home, tmp_path, capsys):
    src = tmp_path/'src'
    bundle.write_bundle(src)
    outputs = []
    for argv in (['scan', '--root', str(src)], ['scan', '--root', str(src)], ['verify'], ['sources']):
        code, out, err = run(capsys, *argv)
        assert code == 0
        outputs.append(out + err)
    logs = ''.join(p.read_text(encoding='utf-8') for p in (home/'logs').glob('*.log'))
    db = store.open_read(home)
    try:
        assert db.execute('SELECT COUNT(*) FROM units WHERE text LIKE ?', (f'%{SENTINEL}%',)).fetchone()[0] > 0
    finally:
        db.close()
    assert logs and SENTINEL not in logs and all(SENTINEL not in text for text in outputs)
    for line in logs.splitlines():
        stamp, *fields = line.split(' | ')
        assert re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}', stamp) and len(fields) == 5, line
    assert ' | added | ' in logs and ' | done | ' in logs


def test_verify_cli_output_and_exit_codes(home, tmp_path, capsys):
    assert run(capsys, 'verify')[0] == 4  # no store yet
    src = tmp_path/'src'
    expected = bundle.write_bundle(src)
    assert run(capsys, 'scan', '--root', str(src))[0] == 0
    code, out, err = run(capsys, 'verify')
    oks = sum(item.status == 'ok' for item in expected)
    units = sum(item.unit_count for item in expected)
    not_ok = len(expected) - oks
    assert (code, err) == (0, '')
    lines = out.splitlines()
    assert re.fullmatch(rf'verify \S+  versions {len(expected)} checked {oks} ok {oks} failed 0  unverifiable {not_ok} '
                        rf'\(stale 0, missing 0, locked 0, unstable 0, parser 0, not_ok {not_ok}, placeholder 0\)',
                        lines[0])
    assert lines[1:] == [f'units {units}/{units} bound (100.0%)  chunks 0  evidence 0', 'binding ok']
    assert run(capsys, 'verify', '--root', str(src))[0] == 0
    assert run(capsys, 'verify', '--root', str(tmp_path/'elsewhere'))[0] == 4

    db = store.open_home(home)
    try:
        doc = db.execute("SELECT id FROM documents WHERE path LIKE '%notes_utf8.txt'").fetchone()[0]
        db.execute('UPDATE units SET text = text || ? WHERE ordinal = 2 AND version_id = '
                   '(SELECT id FROM versions WHERE document_id=?)', (' 변조', doc))
    finally:
        db.close()
    code, out, _ = run(capsys, 'verify')
    lines = out.splitlines()
    assert code == 1 and 'binding FAILED' in lines
    failure = lines[lines.index('binding FAILED') + 1]
    assert re.fullmatch(r'.+notes_utf8\.txt @ [0-9a-f]{12} / 줄 2 : unit_text_mismatch', failure)
    assert SENTINEL not in out and '변조' not in out
    other = db_doc_id(home, 'ascii.txt')
    assert run(capsys, 'verify', '--doc', str(other))[0] == 0


def test_verify_cli_says_when_nothing_was_verified(home, tmp_path, capsys):
    """Review M1-L7: exit 0 as designed (§6.3), but the last line tells 'nothing verified' apart from a pass."""
    src = tmp_path/'src'
    bundle.write_bundle(src)
    assert run(capsys, 'scan', '--root', str(src))[0] == 0
    failed = db_doc_id(home, 'binary.txt')
    code, out, _ = run(capsys, 'verify', '--doc', str(failed))
    assert code == 0 and out.splitlines()[-1] == 'binding ok (nothing verified)' and ' checked 0 ' in out
    code, out, _ = run(capsys, 'verify', '--doc', str(db_doc_id(home, 'ascii.txt')))
    assert code == 0 and out.splitlines()[-1] == 'binding ok'


def test_verify_cli_passes_read_cloud_placeholders(home, tmp_path, capsys, monkeypatch):
    """Review M1-2: ``verify`` follows ``[scan] read_cloud_placeholders`` (default false: no read)."""
    from workmcp import binding, collect
    src = tmp_path/'src'
    (src/'docs').mkdir(parents=True)
    (src/'docs'/'a.txt').write_bytes(b'synthetic placeholder line\n')
    assert run(capsys, 'scan', '--root', str(src))[0] == 0
    real_attributes, real_read = collect.file_attributes, collect.read_source
    reads = []
    monkeypatch.setattr(collect, 'file_attributes',
                        lambda info: real_attributes(info) | paths.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    monkeypatch.setattr(collect, 'read_source', lambda p: reads.append(p) or real_read(p))
    assert binding.collect is collect
    code, out, _ = run(capsys, 'verify')
    assert code == 0 and reads == [] and 'placeholder 1)' in out
    assert out.splitlines()[-1] == 'binding ok (nothing verified)'
    config.config_file(home).write_text('[scan]\nread_cloud_placeholders = true\n', encoding='utf-8')
    code, out, _ = run(capsys, 'verify')
    assert code == 0 and len(reads) == 1 and 'placeholder 0)' in out and out.splitlines()[-1] == 'binding ok'


def db_doc_id(home, name):
    db = store.open_read(home)
    try:
        return db.execute('SELECT id FROM documents WHERE path LIKE ?', (f'%{name}',)).fetchone()[0]
    finally:
        db.close()


def test_sources_cli_lists_roots_checks_and_counts(home, tmp_path, capsys):
    (tmp_path/'a').mkdir()
    (tmp_path/'a'/'x.txt').write_bytes(b'synthetic\n')
    write_config(home, tmp_path/'a', tmp_path/'missing')
    code, out, _ = run(capsys, 'sources')
    assert code == 0 and 'not scanned yet' in out and 'source_root_missing' in out
    assert run(capsys, 'scan')[0] == 4  # one bad root stops the scan
    write_config(home, tmp_path/'a')
    assert run(capsys, 'scan')[0] == 0
    code, out, _ = run(capsys, 'sources')
    assert code == 0
    assert f'{(tmp_path/"a").resolve()}  ok  enabled 1  documents 1 (active 1, deleted 0, skipped 0)' in out


def test_doctor_reports_source_roots(home, tmp_path, capsys):
    code, out, _ = run(capsys, 'doctor')
    assert code == 0 and 'sources: none configured' in out
    (tmp_path/'a').mkdir()
    write_config(home, tmp_path/'a', 'relative')
    code, out, _ = run(capsys, 'doctor')
    assert code == 0 and f'source root {(tmp_path/"a").resolve()}: ok' in out
    assert 'source root relative: source_root_relative' in out


def test_changed_development_schema_is_explained(home, tmp_path, capsys, monkeypatch):
    store.open_home(home).close()
    monkeypatch.setattr(store, 'MIGRATIONS', [(1, 'initial_schema_v1', store.SCHEMA_V1 + '\n-- edited\n')])
    (tmp_path/'src').mkdir()
    code, out, err = run(capsys, 'scan', '--root', str(tmp_path/'src'))
    assert (code, out) == (2, '') and 'development schema change' in err
    assert not store.scan_lock_path(home).exists()
    code, out, _ = run(capsys, 'doctor')
    assert code == 0 and 'development schema change' in out


def test_status_tool_reports_last_scan(home, tmp_path, capsys):
    store.open_home(home).close()
    before = tools.status(home)
    assert before['ok'] and before['result']['last_scan'] is None and before['notice']
    src = tmp_path/'src'
    expected = bundle.write_bundle(src)
    run(capsys, 'scan', '--root', str(src))
    (src/'docs'/'ascii.txt').unlink()
    run(capsys, 'scan', '--root', str(src))
    after = tools.status(home)
    last = after['result']['last_scan']
    assert after['notice'] == [] and after['delivered_sources'] == []
    assert {k: last[k] for k in ('added', 'changed', 'deleted', 'failed')} == {
        'added': 0, 'changed': 0, 'deleted': 1, 'failed': 0}
    assert re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d', last['at'])
    assert after['result']['db']['documents'] == len(expected)
    assert SENTINEL not in json.dumps(after, ensure_ascii=False)

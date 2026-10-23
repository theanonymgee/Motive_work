"""Sources stay read-only (design M1 §4). Stage 0: the source-write judgement and its positive
controls, the ``scan`` audit mode skeleton, and the expat amplification guard.

M1 stage 1 adds ``test_scan_and_verify_never_write_to_sources`` (the collector under the hook)
and ``test_collector_and_parsers_have_no_write_api`` (static check) here.
"""
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pyexpat
import pytest

import _audited_server
from fixtures import LINES_EN, LINES_KO, SENTINEL

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = Path(__file__).resolve().parent/'_audited_server.py'
SRC = Path(r'C:\synthetic\src') if os.name == 'nt' else Path('/synthetic/src')


# Event tuples as measured on this PC (design M1 §4.3).
@pytest.mark.parametrize('event, args', [
    ('open', (str(SRC/'a.txt'), 'w', 33665)),
    ('open', (str(SRC/'a.txt'), 'a', 33161)),
    ('open', (str(SRC/'a.txt'), 'r+', 32898)),
    ('open', (str(SRC/'a.txt'), 'x', 33921)),
    ('open', (str(SRC/'a.txt'), None, 137)),           # os.open O_WRONLY | O_APPEND | O_NOINHERIT
    ('open', (str(SRC/'a.txt'), None, os.O_CREAT | os.O_WRONLY)),
    ('open', (str(SRC/'a.txt'), None, os.O_RDWR)),
    ('open', (str(SRC/'a.txt'), None, os.O_TRUNC)),
    ('os.utime', (str(SRC/'a.txt'), None, None, None)),
    ('os.remove', (str(SRC/'a.txt'), None)),
    ('os.rename', (str(SRC/'a.txt'), str(SRC/'b.txt'), None, None)),
    ('os.rename', (r'C:\elsewhere\x.txt' if os.name == 'nt' else '/elsewhere/x.txt', str(SRC/'b.txt'), None, None)),
    ('os.chmod', (str(SRC/'a.txt'), 0o444, None)),
    ('os.truncate', (str(SRC/'a.txt'), 0)),
    ('os.mkdir', (str(SRC/'new'), 0o777, None)),
    ('os.rmdir', (str(SRC), None)),
    ('os.link', (str(SRC/'a.txt'), str(SRC/'c.txt'), None, None)),
    ('shutil.copyfile', (str(SRC/'a.txt'), str(SRC/'b.txt'))),
    ('shutil.rmtree', (str(SRC), None)),
    ('open', (os.fsencode(str(SRC/'a.txt')), 'w', 33665)),      # bytes path
    ('open', (SRC/'a.txt', 'w', 33665)),                        # PathLike
    ('open', (str(SRC).upper() + os.sep + 'A.TXT', 'w', 33665) if os.name == 'nt' else (str(SRC/'a.txt'), 'w', 1)),
    ('open', (str(SRC/'sub'/'..'/'a.txt'), 'w', 33665)),         # normalised before comparing
])
def test_source_write_judge_flags_every_write_form(event, args):
    assert _audited_server.judge_source_write(event, args, str(SRC)) is not None


@pytest.mark.parametrize('event, args', [
    ('open', (str(SRC/'a.txt'), 'r', 32896)),        # 'rb' arrives as 'r'
    ('open', (str(SRC/'a.txt'), None, 128)),          # os.open O_RDONLY | O_NOINHERIT
    ('open', (str(SRC/'a.txt'), None, 0)),
    ('open', (3, 'r', 0)),                            # file descriptor
    ('open', (str(SRC) + '2' + os.sep + 'a.txt', 'w', 33665)),   # sibling folder sharing the prefix
    ('open', (str(SRC.parent/'other.txt'), 'w', 33665)),
    ('os.remove', (str(SRC.parent/'other.txt'), None)),
    ('os.utime', (5, None, None, None)),
    ('os.listdir', (str(SRC),)),
    ('os.scandir', (str(SRC),)),
    ('socket.connect', (None, ('127.0.0.1', 1))),
])
def test_source_write_judge_lets_reads_and_outside_paths_pass(event, args):
    assert _audited_server.judge_source_write(event, args, str(SRC)) is None


def test_under_compares_whole_path_components():
    assert _audited_server.under(SRC, SRC)
    assert _audited_server.under(SRC/'x'/'y.txt', SRC)
    assert not _audited_server.under(Path(str(SRC) + 'x')/'y.txt', SRC)
    assert not _audited_server.under(SRC.parent, SRC)
    assert not _audited_server.under(None, SRC)


def fingerprint(root: Path) -> dict:
    """size, mtime_ns, sha256 and attributes of every file, plus the folder listing."""
    state = {}
    for path in sorted(root.rglob('*')):
        info = path.stat()
        entry = [path.is_dir(), info.st_mode, getattr(info, 'st_file_attributes', 0)]
        if path.is_file():
            entry += [info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()]
        state[path.relative_to(root).as_posix()] = entry
    return state


def write_synthetic_sources(root: Path) -> None:
    (root/'docs').mkdir(parents=True)
    (root/'docs'/'notes.txt').write_bytes('\n'.join(LINES_KO).encode('utf-8'))
    (root/'docs'/'readme.md').write_bytes('\r\n'.join(LINES_EN).encode('utf-8'))
    (root/'docs'/'table.csv').write_bytes(f'항목,값\n{SENTINEL},1\n'.encode('cp949'))
    (root/'docs'/'paper.pdf').write_bytes(b'%PDF-1.4\n%synthetic\n')
    (root/'docs'/'renamed.docx').write_bytes(b'not a zip')
    (root/'docs'/'ignored.png').write_bytes(b'\x89PNG synthetic')


def test_scan_mode_reads_sources_without_writing(tmp_path):
    """Stage-0 skeleton: the hook sees every source read as mode 'r' and nothing else under the root."""
    source = tmp_path/'src'
    write_synthetic_sources(source)
    files = [p for p in source.rglob('*') if p.is_file()]
    for path in files:
        os.chmod(path, stat.S_IREAD)  # extra belt: any write would also fail at the OS level
    try:
        before = fingerprint(source)
        log = tmp_path/'audit-scan.json'
        env = {**os.environ, 'WORKMCP_HOME': str(tmp_path/'home'), 'PYTHONPATH': str(ROOT/'src')}
        run = subprocess.run([sys.executable, str(WRAPPER), str(log), 'scan', str(source)], env=env, cwd=tmp_path,
                             capture_output=True, text=True, encoding='utf-8', timeout=120)
        assert run.returncode == 0, run.stderr
        report = json.loads(log.read_text(encoding='utf-8'))
        assert fingerprint(source) == before
    finally:
        for path in files:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    assert report['completed'] is True
    assert report['files'] == 5  # .png is not a supported extension
    assert report['statuses'] == {'failed/not_implemented': 4, 'unsupported/bad_signature': 1}
    events = report['events']
    assert [e for e in events if e['violation']] == []
    reads = [e for e in events if e.get('source') and e['event'] == 'open']
    assert len(reads) >= 5
    assert all(", 'r', " in e['detail'] for e in reads), reads
    assert not [e for e in events if e['event'].startswith(_audited_server.PROCESS_EVENTS)]
    assert SENTINEL not in run.stdout + run.stderr


def test_expat_has_amplification_protection():
    """Entity amplification limits arrived in expat 2.4.1; the DTD byte check is the first layer (§5.4)."""
    assert pyexpat.version_info >= (2, 4, 1)

"""Sources stay read-only (design M1 §4).

Runtime: the source-write judgement with its positive controls, and the real collector, binding
check and a second (incremental) scan under the audit hook. Static: no write API in the modules
that touch sources. Plus the expat amplification guard.
"""
import ast
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
from fixtures import SENTINEL, bundle

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = Path(__file__).resolve().parent/'_audited_server.py'
SRC = Path(r'C:\synthetic\src') if os.name == 'nt' else Path('/synthetic/src')
LONG = '\\\\?\\' if os.name == 'nt' else ''   # Win32 long-path prefix
O_BINARY = getattr(os, 'O_BINARY', 0)
O_TEMPORARY = getattr(os, 'O_TEMPORARY', 0x40)      # Windows only; 0x40 is O_CREAT elsewhere
O_SHORT_LIVED = getattr(os, 'O_SHORT_LIVED', os.O_CREAT)
LARGE = 'large/synthetic_large.txt'  # over the audit run's 1 MiB max_file_mb: hashed by stream_sha256


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
    # review M1 P12: delete-on-close and short-lived opens, long-path prefixes, dir_fd names, SQLite
    ('open', (str(SRC/'a.txt'), None, os.O_RDONLY | O_BINARY | O_TEMPORARY | 128)),
    ('open', (str(SRC/'a.txt'), None, O_SHORT_LIVED)),
    ('open', (LONG + str(SRC/'a.txt'), 'w', 33665)),
    ('open', (LONG.replace('\\', '/') + str(SRC/'a.txt'), 'w', 33665)),
    ('os.remove', (LONG + str(SRC/'a.txt'), None)),
    ('os.rename', ('a.txt', 'b.txt', 3, 3)),                    # names relative to a directory descriptor
    ('os.remove', ('a.txt', 3)),
    ('os.utime', ('a.txt', None, None, 3)),
    ('sqlite3.connect', (str(SRC/'x.db'),)),
    ('sqlite3.connect', (SRC/'x.db',)),
    ('sqlite3.connect', (f'{(SRC/"x.db").as_uri()}?mode=ro',)),
    ('sqlite3.connect', (os.fsencode(str(SRC/'x.db')),)),
    ('sqlite3.connect', ((SRC/'합성 폴더'/'x.db').as_uri() + '?mode=rwc',)),  # percent-encoded name
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
    ('open', (LONG + str(SRC/'a.txt'), 'r', 32896)),
    ('open', (LONG + str(SRC.parent/'other.txt'), 'w', 33665)),
    ('os.remove', ('a.txt', None)),                               # relative to the current folder, not a dir_fd
    ('os.remove', ('a.txt', -1)),                                 # -1: the event's 'no dir_fd'
    ('os.mkdir', (str(SRC.parent/'home'/'db'), 511, -1)),
    ('os.rename', (str(SRC.parent/'x'), str(SRC.parent/'y'), 3, 3)),   # absolute names ignore dir_fd
    ('sqlite3.connect', (':memory:',)),
    ('sqlite3.connect', (str(SRC.parent/'home'/'work.sqlite3'),)),
    ('sqlite3.connect', (f'{(SRC.parent/"home"/"work.sqlite3").as_uri()}?mode=ro',)),
])
def test_source_write_judge_lets_reads_and_outside_paths_pass(event, args):
    assert _audited_server.judge_source_write(event, args, str(SRC)) is None


def test_under_compares_whole_path_components():
    assert _audited_server.under(SRC, SRC)
    assert _audited_server.under(SRC/'x'/'y.txt', SRC)
    assert not _audited_server.under(Path(str(SRC) + 'x')/'y.txt', SRC)
    assert not _audited_server.under(SRC.parent, SRC)
    assert not _audited_server.under(None, SRC)
    assert _audited_server.under(LONG + str(SRC/'x.txt'), SRC) and _audited_server.under(SRC/'x.txt', LONG + str(SRC))
    if os.name == 'nt':
        assert _audited_server.under(r'\\?\UNC\srv\share\x.txt', r'\\srv\share')
        assert _audited_server.under(r'\\.' + '\\' + str(SRC/'x.txt'), SRC)


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


def test_scan_and_verify_never_write_to_sources(tmp_path):
    """Design §4.3 (a)-(f) over the full bundle: the collector, the binding check and a second scan
    under the audit hook."""
    source = tmp_path/'src'
    expected = bundle.write_bundle(source)                                          # (a)
    want = bundle.totals(bundle.documents())
    large = source/LARGE  # made here, never committed; the bundle's files are all far below 1 MiB
    large.parent.mkdir()
    line = b'synthetic large line 0123456789\n'
    large.write_bytes(line * (2**20 // len(line) + 64))
    home = tmp_path/'home'
    home.mkdir()
    (home/'config.toml').write_text('[scan]\nmax_file_mb = 1\n', encoding='utf-8')
    files = [p for p in source.rglob('*') if p.is_file()]
    for path in files:
        os.chmod(path, stat.S_IREAD)  # extra belt: any write would also fail at the OS level
    try:
        before = fingerprint(source)                                                # (b)
        log = tmp_path/'audit-scan.json'
        env = {**os.environ, 'WORKMCP_HOME': str(home), 'PYTHONPATH': str(ROOT/'src')}
        run = subprocess.run([sys.executable, str(WRAPPER), str(log), 'scan', str(source)], env=env, cwd=tmp_path,
                             capture_output=True, text=True, encoding='utf-8', timeout=180)
        assert run.returncode == 0, run.stderr                                      # (c)
        report = json.loads(log.read_text(encoding='utf-8'))
        assert fingerprint(source) == before                                        # (e)
    finally:
        for path in files:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    assert report['completed'] is True
    supported = len(expected) + 1                                                   # the bundle and the large file
    assert supported - 1 == want['files'] and len(files) == supported + len(bundle.noise_paths())
    first, second = report['first'], report['second']
    noise = bundle.noise_counts()
    assert (first['seen'], first['added'], first['excluded'], first['ignored']) == (
        supported, supported, noise['excluded'], noise['ignored'])
    assert {key: first[key] for key in ('parsed_ok', 'reused', 'failed', 'unsupported', 'encrypted', 'units')} == {
        key: want[key] for key in ('parsed_ok', 'reused', 'failed', 'unsupported', 'encrypted', 'units')}
    assert first['too_large'] == want['too_large'] + 1                              # stream_sha256 ran under the hook
    assert second['seen'] == second['unchanged'] == supported                       # incremental: nothing re-read
    assert all(second[key] == 0 for key in ('added', 'changed', 'touched', 'restored', 'deleted', 'unstable',
                                            'reparsed', 'new_versions', 'units'))
    statuses = dict(want['statuses'])
    statuses['too_large/file_bytes'] = statuses.get('too_large/file_bytes', 0) + 1
    assert report['statuses'] == statuses
    by_parser = json.loads(json.dumps(want['by_parser']))
    text = by_parser['text']
    text['files'] += 1
    text['statuses']['too_large/file_bytes'] = text['statuses'].get('too_large/file_bytes', 0) + 1
    assert report['by_parser'] == by_parser and len(report['by_parser']) == 9
    verify = report['verify']
    assert verify['ok'] and verify['versions']['ok'] == want['ok']
    assert verify['units']['bound'] == verify['units']['total'] == want['units'] > 0
    events = report['events']
    assert [e for e in events if e['violation']] == []                              # (d) no write was attempted
    reads = [e for e in events if e.get('source') and e['event'] == 'open']
    assert len(reads) >= supported + want['ok']                                     # the hook really saw the reads
    assert all(", 'r', " in e['detail'] for e in reads), reads
    assert [e for e in reads if 'synthetic_large.txt' in e['detail']]               # the streaming read too
    noise_names = ('node_modules', '~$lock', '.git', 'hidden_note', 'image.png')
    assert not [e for e in events if any(name in e['detail'] for name in noise_names)]
    assert not [e for e in events if e['event'].startswith(_audited_server.PROCESS_EVENTS)]   # (f)
    assert not [e for e in events if e['event'].startswith(('socket.getaddrinfo', 'socket.gethostbyname'))]
    assert not [e for e in events if e['event'] in _audited_server.ADDRESS_EVENTS]  # a scan opens no socket at all
    assert SENTINEL not in run.stdout + run.stderr
    assert SENTINEL not in json.dumps(report, ensure_ascii=False)


# Static layer (design §4.2): no write API in the modules that touch sources.
READONLY_MODULES = ('collect.py', 'binding.py', 'parsers', 'knowledge.py', 'knowledge_query.py',  # M4 §8.1
                    'push.py', 'sync.py', 'receive.py')  # M5 remote §6.4: push writes only its log, via logging
WRITE_NAMES = frozenset({'write_text', 'write_bytes', 'touch', 'unlink', 'rename', 'rmdir', 'mkdir', 'makedirs',
                         'removedirs', 'chmod', 'utime', 'truncate', 'link', 'symlink', 'copyfile', 'copy2',
                         'copytree', 'move', 'rmtree', 'mkstemp', 'mkdtemp', 'NamedTemporaryFile', 'TemporaryFile',
                         'write', 'fdopen', 'FileIO', 'hardlink_to', 'symlink_to', 'link_to', 'write_mode',
                         'PdfWriter'})
OS_WRITE_CALLS = frozenset({'os.remove', 'os.replace', 'os.open', 'os.renames', 'os.lchmod', 'os.chown', 'os.lchown',
                            'os.chflags', 'os.mkfifo', 'os.mknod', 'os.ftruncate', 'os.write', 'os.startfile'})
BANNED_IMPORTS = ('shutil', 'tempfile', 'sqlite3', '_sqlite3', 'mmap')
BANNED_KEYWORDS = frozenset({'opener', 'write_mode'})
# Container libraries get the bytes, never a path (design §4.1-2): their first argument must be
# an ``io.BytesIO(...)`` call or a name bound to one in the same module.
CONTAINERS = frozenset({'ZipFile', 'OleFileIO', 'isOleFile', 'PdfReader'})
CONTAINER_KEYWORDS = ('file', 'filename', 'stream')


def _qualify(node, bound):
    if isinstance(node, ast.Name):
        return bound.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _qualify(node.value, bound)
        return f'{base}.{node.attr}' if base else None
    return None


def _open_mode(call: ast.Call):
    for keyword in call.keywords:
        if keyword.arg == 'mode':
            return keyword.value
    if isinstance(call.func, ast.Name) or len(call.args) > 1:  # open(path, mode), zip.open(name, mode)
        return call.args[1] if len(call.args) > 1 else None
    return call.args[0] if call.args else None                  # Path(p).open(mode)


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _is_bytesio(node, bound, buffers) -> bool:
    if isinstance(node, ast.Call):
        return _qualify(node.func, bound) == 'io.BytesIO'
    return isinstance(node, ast.Name) and node.id in buffers


def _bytesio_names(tree, bound) -> set[str]:
    """Names assigned from an ``io.BytesIO(...)`` call (``buf = io.BytesIO(data)``, ``with ... as buf``)."""
    names = set()
    for node in ast.walk(tree):
        pairs = []
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            pairs = [(target, node.value) for target in (node.targets if isinstance(node, ast.Assign) else [node.target])]
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            pairs = [(node.optional_vars, node.context_expr)]
        names |= {target.id for target, value in pairs
                  if isinstance(target, ast.Name) and _is_bytesio(value, bound, ())}
    return names


def _path_replace(node: ast.Call, bound) -> bool:
    """``Path(p).replace(q)``, ``Path.replace(p, q)`` or any ``x.replace(q)`` with one argument (``str`` and
    ``bytes`` take two; ``dataclasses.replace`` and ``datetime.replace`` take keywords)."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == 'replace'):
        return False
    receiver = func.value.func if isinstance(func.value, ast.Call) else func.value
    if 'Path' in (_qualify(receiver, bound) or ''):
        return True
    return len(node.args) == 1 and not node.keywords


def write_api_violations(name: str, source: str) -> list[str]:
    """``name`` is the path relative to the package (``collect.py``, ``parsers/_container.py``)."""
    tree = ast.parse(source)
    allowed_modes = {'rb', 'r'} if name == 'parsers/_container.py' else {'rb'}
    bound, found = {}, []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split('.')[0]
                bound[alias.asname or top] = alias.name if alias.asname else top
                if top in BANNED_IMPORTS:
                    found.append(f'import {alias.name}')
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound[alias.asname or alias.name] = f'{node.module}.{alias.name}'
                if node.level == 0 and (node.module.split('.')[0] in BANNED_IMPORTS or alias.name in WRITE_NAMES
                                        or f'{node.module}.{alias.name}' in OS_WRITE_CALLS):
                    found.append(f'from {node.module} import {alias.name}')
    buffers = _bytesio_names(tree, bound)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_open = (isinstance(func, ast.Name) and func.id == 'open') or (
                isinstance(func, ast.Attribute) and func.attr == 'open')
            if is_open and _qualify(func, bound) != 'os.open':  # os.open is reported as an os write call
                mode = _open_mode(node)
                if not (isinstance(mode, ast.Constant) and mode.value in allowed_modes):
                    found.append(f'open without a read-only mode at line {node.lineno}')
            if _called_name(node) in CONTAINERS:
                first = node.args[0] if node.args else next(
                    (k.value for k in node.keywords if k.arg in CONTAINER_KEYWORDS), None)
                if not _is_bytesio(first, bound, buffers):
                    found.append(f'{_called_name(node)} not given an io.BytesIO at line {node.lineno}')
            if _path_replace(node, bound):
                found.append(f'Path.replace at line {node.lineno}')
            found += [f'keyword {k.arg}= at line {node.lineno}' for k in node.keywords if k.arg in BANNED_KEYWORDS]
        elif isinstance(node, ast.Attribute):
            if node.attr in WRITE_NAMES:
                found.append(f'write API .{node.attr} at line {node.lineno}')
            elif _qualify(node, bound) in OS_WRITE_CALLS:
                found.append(f'{_qualify(node, bound)} at line {node.lineno}')
        elif isinstance(node, ast.Name) and node.id in WRITE_NAMES:
            found.append(f'write API {node.id} at line {node.lineno}')
        elif isinstance(node, ast.Name) and bound.get(node.id, node.id).split('.')[0] in BANNED_IMPORTS:
            found.append(f'{node.id} (banned module) at line {node.lineno}')
    return found


def readonly_sources():
    package = ROOT/'src'/'workmcp'
    return sorted(p for p in package.rglob('*.py') if p.relative_to(package).parts[0] in READONLY_MODULES)


def test_collector_and_parsers_have_no_write_api():
    package = ROOT/'src'/'workmcp'
    names = {p.relative_to(package).as_posix() for p in readonly_sources()}
    assert {'collect.py', 'binding.py', 'parsers/__init__.py', 'parsers/_container.py', 'parsers/text.py',
            'parsers/pdf.py', 'parsers/hwp.py', 'knowledge.py', 'knowledge_query.py', 'push.py', 'sync.py',
            'receive.py'} <= names
    offenders = {name: v for p in readonly_sources()
                 if (v := write_api_violations(name := p.relative_to(package).as_posix(), p.read_text(encoding='utf-8')))}
    assert offenders == {}


@pytest.mark.parametrize('snippet', [
    'open(p)', "open(p, 'r')", "open(p, 'rb+')", "Path(p).write_bytes(b'')", 'os.remove(p)', 'os.replace(a, b)',
    'os.open(p, os.O_RDONLY)', 'import shutil',
    "open(p, mode='w')", "io.open(p, 'wb')", "Path(p).open('a')", 'Path(p).open()', 'p.unlink()', 'p.touch()',
    'os.utime(p)', 'os.chmod(p, 0o444)', 'import tempfile', 'from os import remove', 'from shutil import copyfile',
    'handle.write(b"x")', "z.open(name, 'w')", 'mode = "rb"\nopen(p, mode)',
    # review M1 P11: containers given a path, write modes, other write APIs
    "zipfile.ZipFile(path, 'a')", 'zipfile.ZipFile(path)', 'zipfile.ZipFile(file=path)',
    'from zipfile import ZipFile\nZipFile(path)', 'olefile.OleFileIO(path, write_mode=True)',
    'olefile.OleFileIO(io.BytesIO(data), write_mode=True)', 'olefile.isOleFile(path)', 'PdfReader(path)',
    'pypdf.PdfReader(stream=path)', 'buf = open_stream(path)\nPdfReader(buf)', 'zipfile.ZipFile(io.StringIO(s))',
    'Path(p).replace(q)', 'p.replace(q)', 'Path.replace(p, q)', 'Path(p).hardlink_to(q)', 'Path(p).symlink_to(q)',
    'Path(p).link_to(q)', 'sqlite3.connect(path)', 'import sqlite3', 'from sqlite3 import connect',
    "open(p, 'rb', opener=my_opener)", 'import mmap\nmmap.mmap(f.fileno(), 0)', 'mmap.mmap(-1, 10)',
    'from pypdf import PdfWriter', 'import pypdf\npypdf.PdfWriter()'])
def test_write_api_check_flags_every_form(snippet):
    assert write_api_violations('collect.py', 'import io\nimport os\nfrom pathlib import Path\n' + snippet)


def test_container_check_accepts_bytes_in_memory():
    clean = ('import io\nimport zipfile\nimport olefile\nimport dataclasses\nfrom io import BytesIO\n'
             'from pypdf import PdfReader\n'
             'zipfile.ZipFile(io.BytesIO(data))\nolefile.OleFileIO(io.BytesIO(data))\n'
             'buf = io.BytesIO(data)\nolefile.OleFileIO(buf)\nPdfReader(BytesIO(data))\n'
             'with io.BytesIO(data) as stream:\n    PdfReader(stream)\n'
             "def f(ole: olefile.OleFileIO) -> zipfile.ZipFile:\n    pass\n"
             "text.replace('a', 'b')\nraw.replace(b'a', b'b', 1)\ndataclasses.replace(obj, x=1)\n"
             'when.replace(tzinfo=None)\n')
    assert write_api_violations('parsers/pdf.py', clean) == []


def test_write_api_check_accepts_read_only_forms():
    clean = ("import os\nwith open(p, 'rb') as f:\n    f.read()\nos.stat(p)\nos.scandir(p)\n"
             "'a'.replace('a', 'b')\n[1].remove(1)\nos.path.join(a, b)\n")
    assert write_api_violations('collect.py', clean) == []
    assert write_api_violations('parsers/_container.py', "z.open(info, 'r')\n") == []
    assert write_api_violations('parsers/docx.py', "z.open(info, 'r')\n")  # 'r' only inside _container


def test_expat_has_amplification_protection():
    """Entity amplification limits arrived in expat 2.4.1; the DTD byte check is the first layer (§5.4)."""
    assert pyexpat.version_info >= (2, 4, 1)

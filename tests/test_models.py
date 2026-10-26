"""Model manifest, verification and the offline import (design M2 §3.3, §10). Synthetic files only: a tiny
fake manifest entry is built from files written at test time; the real model is used only when present.
"""
import ast
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import _real_model
from workmcp import cli, embedder, models, paths

PACKAGE = Path(models.__file__).resolve().parent
CONTENT = {'tiny.onnx': b'A' * 3000, 'tiny.onnx_data': bytes(range(256)) * 40, 'tiny.tokenizer.json': b'{"t": 1}'}
TREE_PATH = {name: f'onnx/{name}' for name in CONTENT}
ALL_OK = {name: 'ok' for name in CONTENT}
ALL_VERIFIED = {name: 'already_verified' for name in CONTENT}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def fake(monkeypatch):
    spec = {'repo': 'test/none', 'revision': 'rev1', 'base_url': 'https://example.invalid', 'kind': 'embedding',
            'license': 'test', 'source': 'test',
            'precision': 'fp32', 'pooling': 'cls', 'query_prefix': '', 'passage_prefix': '', 'dims': 4,
            'max_tokens': 8, 'model_file': 'tiny.onnx', 'tokenizer_file': 'tiny.tokenizer.json',
            'files': [{'path': TREE_PATH[n], 'size': len(c), 'sha256': sha(c)} for n, c in CONTENT.items()]}
    monkeypatch.setitem(models.MANIFEST, 'tiny', spec)
    monkeypatch.setattr(models, 'CHUNK_BYTES', 64)  # many blocks per file
    return spec


@pytest.fixture
def home(workmcp_home):
    return paths.ensure_home(workmcp_home)


def make_source(folder: Path, layout: str = 'flat') -> Path:
    for name, data in CONTENT.items():
        target = folder/(name if layout == 'flat' else TREE_PATH[name])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return folder


def snapshot(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): (sha(p.read_bytes()), p.stat().st_mtime_ns)
            for p in sorted(folder.rglob('*')) if p.is_file()}


def partials(home: Path) -> list[Path]:
    return sorted((home/'tmp').glob('*.partial'))


def quiet(_line):
    pass


def test_import_from_copies_verifies_and_replaces_atomically(fake, home, tmp_path, monkeypatch):
    source = make_source(tmp_path/'already-downloaded')
    before = snapshot(source)
    replaced = []
    real_replace = os.replace

    def spy(a, b):
        replaced.append((Path(a), Path(b)))
        real_replace(a, b)

    monkeypatch.setattr(models.os, 'replace', spy)
    lines = []
    assert models.import_from('tiny', source, home, log=lines.append) == ALL_OK
    assert len(lines) == len(CONTENT) and all(line.startswith('copy tiny/') for line in lines)
    assert [(a.parent, a.suffix) for a, _ in replaced] == [(home/'tmp', '.partial')]*len(CONTENT)
    assert {b for _, b in replaced} == {models.file_path('tiny', n, home) for n in CONTENT}
    assert models.verify('tiny', home) == ALL_OK and partials(home) == []
    assert snapshot(source) == before  # the source folder is only read


def test_import_from_reads_flat_or_tree_layout(fake, home, tmp_path):
    assert models.import_from('tiny', make_source(tmp_path/'tree', 'tree'), home, log=quiet) == ALL_OK
    mixed = tmp_path/'mixed'
    make_source(mixed, 'tree')
    (mixed/'tiny.onnx').write_bytes(CONTENT['tiny.onnx'])  # the flat name is found first
    assert models.import_from('tiny', mixed, paths.ensure_home(tmp_path/'other-home'), log=quiet) == ALL_OK


def test_import_from_skips_already_verified(fake, home, tmp_path):
    source = make_source(tmp_path/'src')
    models.import_from('tiny', source, home, log=quiet)
    for path in source.iterdir():  # the source may even be gone: nothing is read from it
        path.unlink()
    lines = []
    assert models.import_from('tiny', source, home, log=lines.append) == ALL_VERIFIED
    assert lines == []


def test_import_from_rejects_size_and_hash_mismatch_without_touching_target(fake, home, tmp_path):
    source = make_source(tmp_path/'src')
    (source/'tiny.onnx').write_bytes(CONTENT['tiny.onnx'] + b'x')                    # wrong size
    (source/'tiny.onnx_data').write_bytes(b'B'*len(CONTENT['tiny.onnx_data']))       # right size, wrong bytes
    (source/'tiny.tokenizer.json').unlink()                                          # absent
    keep = models.file_path('tiny', 'tiny.onnx_data', home)
    keep.parent.mkdir(parents=True)
    keep.write_bytes(b'old target')                                                  # must stay as it is
    before = snapshot(source)
    report = models.import_from('tiny', source, home, log=quiet)
    assert report == {'tiny.onnx': 'size_mismatch_in_source', 'tiny.onnx_data': 'sha256_mismatch_in_source',
                      'tiny.tokenizer.json': 'missing_in_source'}
    assert keep.read_bytes() == b'old target' and not models.file_path('tiny', 'tiny.onnx', home).exists()
    assert partials(home) == [] and snapshot(source) == before


def test_corrupt_target_is_replaced_only_by_a_verified_copy(fake, home, tmp_path):
    source = make_source(tmp_path/'src')
    bad = models.file_path('tiny', 'tiny.onnx', home)
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b'Z'*len(CONTENT['tiny.onnx']))  # right size, wrong hash
    assert models.verify('tiny', home)['tiny.onnx'] == 'sha256_mismatch'
    assert models.import_from('tiny', source, home, log=quiet)['tiny.onnx'] == 'ok'
    assert bad.read_bytes() == CONTENT['tiny.onnx']


def test_interrupt_mid_copy_leaves_no_partial_and_no_target_and_rerun_completes(fake, home, tmp_path, monkeypatch):
    source = make_source(tmp_path/'src')
    calls = []

    class Interrupting:
        def __init__(self):
            self.inner = hashlib.sha256()

        def update(self, block):
            calls.append(len(block))
            if len(calls) == 3:
                raise KeyboardInterrupt
            self.inner.update(block)

        def hexdigest(self):
            return self.inner.hexdigest()

    with monkeypatch.context() as patch:
        patch.setattr(models, 'hashlib', SimpleNamespace(sha256=Interrupting))
        with pytest.raises(KeyboardInterrupt):
            models.import_from('tiny', source, home, log=quiet)
    assert len(calls) == 3 and partials(home) == []
    assert not any(models.model_dir('tiny', home).iterdir())
    assert models.import_from('tiny', source, home, log=quiet) == ALL_OK


def test_stale_partial_from_a_killed_run_is_overwritten(fake, home, tmp_path):
    source = make_source(tmp_path/'src')
    stale = home/'tmp'/'tiny-tiny.onnx.partial'
    stale.write_bytes(b'half a file from a killed process'*100)
    assert models.import_from('tiny', source, home, log=quiet) == ALL_OK
    assert not stale.exists()


def test_copy_error_is_reported_per_file_and_the_other_files_still_go_on(fake, home, tmp_path, monkeypatch):
    source = make_source(tmp_path/'src')
    real_open = open

    def flaky(path, mode='r', *args, **kwargs):
        if 'w' in mode and str(path).endswith('tiny-tiny.onnx.partial'):
            raise PermissionError('denied')
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(models, 'open', flaky, raising=False)
    assert models.import_from('tiny', source, home, log=quiet) == {**ALL_OK, 'tiny.onnx': 'copy_failed:PermissionError'}
    assert partials(home) == []


def test_second_import_is_idempotent(fake, home, tmp_path):
    source = make_source(tmp_path/'src')
    assert models.import_from('tiny', source, home, log=quiet) == ALL_OK
    assert models.import_from('tiny', source, home, log=quiet) == ALL_VERIFIED


def test_verify_statuses_missing_size_sha256(fake, home):
    folder = models.model_dir('tiny', home)
    folder.mkdir(parents=True)
    assert set(models.verify('tiny', home).values()) == {'missing'} and not models.available('tiny', home)
    (folder/'tiny.onnx').write_bytes(CONTENT['tiny.onnx'])
    (folder/'tiny.onnx_data').write_bytes(b'short')
    (folder/'tiny.tokenizer.json').write_bytes(b'#'*len(CONTENT['tiny.tokenizer.json']))
    assert models.verify('tiny', home) == {'tiny.onnx': 'ok', 'tiny.onnx_data': 'size_mismatch',
                                           'tiny.tokenizer.json': 'sha256_mismatch'}
    assert 'size mismatch' in models.quick_status('tiny', home)['message']
    with pytest.raises(models.ModelFileError):
        models.require_verified('tiny', home)


def test_manifest_entries_are_pinned_and_layout_is_flat():
    for name, item in models.MANIFEST.items():
        assert len(item['revision']) == 40 and set(item['revision']) <= set('0123456789abcdef')
        assert all(len(f['sha256']) == 64 and f['size'] > 0 for f in item['files'])
        assert any(models.local_name(f) == item['model_file'] for f in item['files'])
        assert models.model_dir(name, Path('H')) == Path('H')/'models'/name/item['revision']
        assert models.identity(name)['dims'] == item['dims'] and models.identity(name)['real_model'] is True


def test_download_urls_name_the_pinned_revision(fake):
    """M5 design §12: the model is downloaded from the pinned commit, never a branch (string only, no network)."""
    for name, item in models.MANIFEST.items():
        for spec in item['files']:
            url = models.file_url(name, spec)
            assert url == f"{item['base_url']}/{item['repo']}/resolve/{item['revision']}/{spec['path']}"
            assert url.startswith('https://') and '/resolve/main/' not in url
    item = models.MANIFEST['bge-m3']
    assert item['base_url'] == models.BASE_URL == 'https://huggingface.co' and item['source'] == 'huggingface'
    assert models.file_url('tiny', fake['files'][0]) == 'https://example.invalid/test/none/resolve/rev1/onnx/tiny.onnx'


@pytest.mark.parametrize('header, start', [('bytes 100-199/200', 100), ('bytes 0-9/10', 0), (None, None),
                                           ('bytes */200', None), ('items 1-2/3', None), ('', None)])
def test_content_range_start(header, start):
    assert models._range_start(header) == start


def test_fetch_model_cli_requires_from_and_verify_model_reports(fake, home, tmp_path, capsys):
    assert cli.main(['fetch-model', 'tiny']) == 4
    assert '--from' in capsys.readouterr().err
    assert cli.main(['fetch-model', 'tiny', '--from', str(tmp_path/'nope')]) == 4
    assert cli.main(['fetch-model', 'unknown-model', '--from', str(tmp_path)]) == 4
    capsys.readouterr()
    assert cli.main(['verify-model', 'tiny']) == 1
    assert capsys.readouterr().out.splitlines() == [f'tiny/{n}: missing' for n in CONTENT]

    source = make_source(tmp_path/'src')
    assert cli.main(['fetch-model', 'tiny', '--from', str(source)]) == 0
    out = capsys.readouterr().out
    assert [line for line in out.splitlines() if not line.startswith('copy ')] == [f'tiny/{n}: ok' for n in CONTENT]
    assert cli.main(['fetch-model', 'tiny', '--from', str(source)]) == 0  # again: already verified
    assert capsys.readouterr().out.splitlines() == [f'tiny/{n}: already_verified' for n in CONTENT]
    assert cli.main(['verify-model', 'tiny']) == 0
    assert capsys.readouterr().out.splitlines() == [f'tiny/{n}: ok' for n in CONTENT]

    (source/'tiny.onnx_data').write_bytes(b'C'*len(CONTENT['tiny.onnx_data']))
    models.file_path('tiny', 'tiny.onnx_data', home).unlink()
    assert cli.main(['fetch-model', 'tiny', '--from', str(source)]) == 1
    assert 'tiny/tiny.onnx_data: sha256_mismatch_in_source' in capsys.readouterr().out


IMPORT_PATH = ('import_from', 'cli_fetch_model', '_copy_hashed', '_source_file')
DOWNLOAD_PATH = {'download_file', 'fetch', 'main', 'download'}
NETWORK_NAMES = {'urlopen', 'urllib', 'socket', 'subprocess', 'http'}


def test_import_from_is_not_a_network_function():
    tree = ast.parse((PACKAGE/'models.py').read_text(encoding='utf-8'))
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in IMPORT_PATH:
        nodes = list(ast.walk(functions[name]))
        names = {n.id for n in nodes if isinstance(n, ast.Name)} | {n.attr for n in nodes if isinstance(n, ast.Attribute)}
        assert not names & (DOWNLOAD_PATH | NETWORK_NAMES), (name, names & (DOWNLOAD_PATH | NETWORK_NAMES))
        assert not [n for n in nodes if isinstance(n, (ast.Import, ast.ImportFrom))], name
    for module in ('cli.py', 'embedder.py'):  # neither reaches the download path through ``models``
        used = {n.attr for n in ast.walk(ast.parse((PACKAGE/module).read_text(encoding='utf-8')))
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'models'}
        assert not used & DOWNLOAD_PATH, (module, used & DOWNLOAD_PATH)


@pytest.mark.parametrize('name', ['bge-m3'])
def test_real_model_verifies_and_loads(name):
    home = _real_model.real_model_home(name)
    if home is None:
        pytest.skip(_real_model.SKIP_REASON)
    assert models.verify(name, home) == {models.local_name(f): 'ok' for f in models.entry(name)['files']}
    assert models.require_verified(name, home) == models.model_dir(name, home)
    loaded = embedder.OnnxEmbedder(name, home)
    vector = loaded.embed(['합성 문장', 'A synthetic sentence'], query=False)
    assert vector.shape == (2, models.entry(name)['dims']) and loaded.providers == ['CPUExecutionProvider']
    assert abs(float((vector[0]**2).sum()) - 1.0) < 1e-4


class FakeResponse:
    """What ``download_file`` reads from an opened URL: ``status``, ``headers``, ``read`` (no socket)."""

    def __init__(self, status, body, content_range=None):
        self.status, self.body, self.headers = status, body, {'Content-Range': content_range} if content_range else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        block, self.body = self.body[:n], self.body[n:]
        return block


def fake_opener(data: bytes, mode: str, seen: list):
    """``mode``: ``range`` honours Range, ``ignore`` answers 200 with everything, ``shifted`` a 206 from byte 0."""
    def opener(request, timeout):
        assert request.full_url.startswith('https://example.invalid/') and timeout == 60
        seen.append(request.get_header('Range'))
        have = int(request.get_header('Range')[len('bytes='):-1]) if request.get_header('Range') else 0
        if not have or mode == 'ignore':
            return FakeResponse(200, data)
        start = have if mode == 'range' else 0
        return FakeResponse(206, data[start:], f'bytes {start}-{len(data) - 1}/{len(data)}')
    return opener


@pytest.mark.parametrize('mode, ranges, note', [
    ('range', ['bytes=1000-'], None),
    ('ignore', ['bytes=1000-'], 'server ignored Range'),
    ('shifted', ['bytes=1000-', None], 'unexpected Content-Range'),  # partial deleted, then a fresh request
])
def test_download_file_resumes_a_partial_and_replaces_atomically(fake, home, mode, ranges, note):
    """Offline (fake opener): a fresh file, then a resumed partial; a wrong answer restarts the file."""
    data = CONTENT['tiny.onnx_data']
    target, partial = models.file_path('tiny', 'tiny.onnx_data', home), home/'tmp'/'tiny-tiny.onnx_data.partial'
    url, seen, lines = models.file_url('tiny', fake['files'][1]), [], []
    models.download_file(url, target, partial, len(data), sha(data), log=quiet, opener=fake_opener(data, mode, seen))
    assert target.read_bytes() == data and not partial.exists()
    target.unlink()
    partial.write_bytes(data[:1000])  # left by an interrupted run
    models.download_file(url, target, partial, len(data), sha(data), log=lines.append,
                         opener=fake_opener(data, mode, seen))
    assert target.read_bytes() == data and not partial.exists() and seen == [None, *ranges]
    assert note is None or any(note in line for line in lines)


def test_download_file_deletes_a_partial_with_the_wrong_hash(fake, home):
    data = CONTENT['tiny.onnx_data']
    target, partial = models.file_path('tiny', 'tiny.onnx_data', home), home/'tmp'/'x.partial'
    with pytest.raises(models.ModelFileError, match='SHA-256 mismatch'):
        models.download_file('https://example.invalid/x', target, partial, len(data), sha(b'other'), log=quiet,
                             opener=fake_opener(data, 'range', []))
    assert not partial.exists() and not target.exists()

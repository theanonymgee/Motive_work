"""``tools/make_bundle.py`` (design M6 §3): argument checks, the refusal of an output folder inside the repository,
``SHA256SUMS`` writing / verification / tamper detection on fake files, the lock parser, the verified model copy and
the assembly of a bundle with pip replaced by fakes. No network, no pip, no real bundle."""
import hashlib
import importlib.util
import io
import sys
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from workmcp import models

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO/'tools'/'make_bundle.py'


def _load():
    spec = importlib.util.spec_from_file_location('make_bundle_under_test', TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module                  # dataclasses looks its module up here
    spec.loader.exec_module(module)
    return module


mb = _load()


def run(argv, script='make_bundle.py'):
    out, err = io.StringIO(), io.StringIO()
    saved = sys.argv
    sys.argv = [script]
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = mb.main(argv)
            except SystemExit as exc:
                code = exc.code
    finally:
        sys.argv = saved
    return code, out.getvalue(), err.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fake_folder(root: Path) -> Path:
    folder = root/'bundle'
    (folder/'wheels').mkdir(parents=True)
    (folder/'wheels'/'a-1.0-py3-none-any.whl').write_bytes(b'wheel a')
    (folder/'wheels'/'b-2.0-py3-none-any.whl').write_bytes(b'wheel b')
    (folder/'requirements.lock.txt').write_bytes(b'a==1.0\n')
    (folder/'INSTALL.txt').write_text('instructions', encoding='utf-8')
    mb.write_sums(folder)
    return folder


# ---- arguments ---------------------------------------------------------------------------------------------------

def test_help_lists_both_commands():
    code, out, _ = run(['--help'])
    assert code == 0 and 'make' in out and 'verify' in out
    code, out, _ = run(['make', '--help'])
    assert code == 0 and '--out' in out and '--model-from' in out and '--dry-run' in out


def test_make_needs_out_and_a_command():
    assert run(['make'])[0] == 2
    assert run([])[0] == 2


def test_relative_out_is_refused(tmp_path):
    code, _, err = run(['make', '--out', 'bundles'])
    assert code == 4 and 'absolute' in err


@pytest.mark.parametrize('relative', ['bundles', 'tools/x', '.'])
def test_out_inside_the_repository_is_refused_before_anything_is_created(relative):
    target = REPO/relative
    existed = target.exists()
    code, out, err = run(['make', '--out', str(target)])
    assert code == 4 and 'inside the repository' in err and out == ''
    assert target.exists() == existed


def test_out_problem_accepts_a_sibling_and_rejects_nested_paths(tmp_path):
    assert mb.out_problem(tmp_path/'bundles', REPO) is None
    assert mb.out_problem(REPO/'a'/'b', REPO)
    assert mb.out_problem(REPO.parent/(REPO.name + '-bundles'), REPO) is None      # a name that merely starts alike


def test_model_from_must_be_a_folder(tmp_path):
    code, _, err = run(['make', '--out', str(tmp_path/'o'), '--model-from', str(tmp_path/'nope')])
    assert code == 4 and 'not a folder' in err


def test_host_check_only_lets_windows_amd64_python_312_through(monkeypatch):
    monkeypatch.setattr(mb.sys, 'platform', 'linux')
    assert mb.host_problem() and 'Windows AMD64 Python 3.12' in mb.host_problem()


# ---- SHA256SUMS ---------------------------------------------------------------------------------------------------

def test_sums_are_written_in_sha256sum_format_without_install_and_itself(tmp_path):
    folder = fake_folder(tmp_path)
    lines = (folder/'SHA256SUMS').read_text(encoding='utf-8').splitlines()
    assert lines == [f'{sha(b"a==1.0\n")}  requirements.lock.txt', f'{sha(b"wheel a")}  wheels/a-1.0-py3-none-any.whl',
                     f'{sha(b"wheel b")}  wheels/b-2.0-py3-none-any.whl']
    assert not (folder/'SHA256SUMS').read_bytes().count(b'\r')


def test_verify_passes_on_an_untouched_bundle(tmp_path):
    folder = fake_folder(tmp_path)
    code, out, _ = run(['verify', str(folder)])
    assert code == 0 and out.strip().endswith('ok 3 mismatch 0 missing 0 unlisted 0')


def test_verify_detects_a_changed_a_missing_and_an_added_file(tmp_path):
    folder = fake_folder(tmp_path)
    (folder/'wheels'/'a-1.0-py3-none-any.whl').write_bytes(b'tampered')
    (folder/'wheels'/'b-2.0-py3-none-any.whl').unlink()
    (folder/'wheels'/'workmcp-0.0.1-py3-none-any.whl').write_bytes(b'planted')
    code, out, _ = run(['verify', str(folder)])
    assert code == 1
    assert 'mismatch: wheels/a-1.0-py3-none-any.whl' in out and 'missing: wheels/b-2.0-py3-none-any.whl' in out
    assert 'unlisted: wheels/workmcp-0.0.1-py3-none-any.whl' in out
    assert out.strip().endswith('ok 1 mismatch 1 missing 1 unlisted 1')


def test_verify_detects_a_changed_sums_file_through_its_own_hash(tmp_path):
    folder = fake_folder(tmp_path)
    before = mb.sha256_file(folder/'SHA256SUMS')
    lines = (folder/'SHA256SUMS').read_text(encoding='utf-8').splitlines()
    (folder/'SHA256SUMS').write_text('\n'.join(lines[:-1]) + '\n', encoding='utf-8')
    assert mb.sha256_file(folder/'SHA256SUMS') != before                      # what INSTALL.txt tells the user to compare
    assert run(['verify', str(folder)])[0] == 1                               # and the dropped file shows up as unlisted


def test_verify_without_sums_or_with_a_bad_line_fails(tmp_path):
    empty = tmp_path/'e'
    empty.mkdir()
    assert run(['verify', str(empty)])[0] == 1
    (empty/'SHA256SUMS').write_text('not a sum line\n', encoding='utf-8')
    code, _, err = run(['verify', str(empty)])
    assert code == 1 and 'line 1' in err


@pytest.mark.parametrize('name', ['../outside.txt', '/etc/passwd', 'C:/Windows/x', 'a/../../b', '\\\\host\\share'])
def test_a_sums_path_may_not_leave_the_bundle_folder(tmp_path, name):
    folder = tmp_path/'b'
    folder.mkdir()
    (tmp_path/'outside.txt').write_bytes(b'x')
    (folder/'SHA256SUMS').write_text(f'{sha(b"x")}  {name}\n', encoding='utf-8')
    code, _, err = run(['verify', str(folder)])
    assert code == 1 and 'leaves the bundle folder' in err


def test_verify_bundle_py_takes_the_folder_without_a_subcommand(tmp_path):
    folder = fake_folder(tmp_path)
    code, out, _ = run([str(folder)], script='verify_bundle.py')
    assert code == 0 and 'ok 3' in out
    code, out, _ = run(['verify', str(folder)], script='verify_bundle.py')
    assert code == 0 and 'ok 3' in out


# ---- the lock file ------------------------------------------------------------------------------------------------

LOCK_TEXT = ('# comment\nanyio==4.15.1 \\\n    --hash=sha256:' + 'a' * 64 + ' \\\n    --hash=sha256:' + 'b' * 64 +
             '\n    # via mcp\npywin32==312 ; sys_platform == \'win32\' \\\n    --hash=sha256:' + 'c' * 64 +
             '\nhttpx2-jsfetch==1.0 ; sys_platform == \'emscripten\' \\\n    --hash=sha256:' + 'd' * 64 + '\n')


def test_parse_lock_reads_pins_markers_and_hashes():
    pins = mb.parse_lock(LOCK_TEXT)
    assert set(pins) == {'anyio', 'pywin32', 'httpx2-jsfetch'}
    assert pins['anyio'].version == '4.15.1' and pins['anyio'].marker is None and len(pins['anyio'].hashes) == 2
    assert pins['pywin32'].marker == "sys_platform == 'win32'" and pins['pywin32'].hashes == {'c' * 64}


def test_parse_lock_on_the_real_lock_file():
    pins = mb.parse_lock((REPO/'requirements.lock.txt').read_text(encoding='utf-8'))
    assert len(pins) >= 40 and all(pin.hashes for pin in pins.values())
    assert {'mcp', 'numpy', 'onnxruntime', 'tokenizers', 'pypdf'} <= set(pins)


def test_wheel_pin_names():
    assert mb.wheel_pin('typing_extensions-4.1.0-py3-none-any.whl') == ('typing-extensions', '4.1.0')
    assert mb.wheel_pin('numpy-2.5.4-cp312-cp312-win_amd64.whl') == ('numpy', '2.5.4')
    with pytest.raises(mb.Problem):
        mb.wheel_pin('numpy-2.5.4.tar.gz')


def test_check_wheels_accepts_lock_wheels_and_reports_absent_marker_pins(tmp_path):
    wheels = tmp_path/'w'
    wheels.mkdir()
    (wheels/'anyio-4.15.1-py3-none-any.whl').write_bytes(b'one')
    (wheels/'pywin32-312-cp312-cp312-win_amd64.whl').write_bytes(b'two')
    lock = ('anyio==4.15.1 \\\n    --hash=sha256:' + sha(b'one') + '\npywin32==312 ; sys_platform == \'win32\' \\\n'
            '    --hash=sha256:' + sha(b'two') + '\nhttpx2-jsfetch==1.0 ; sys_platform == \'emscripten\' \\\n'
            '    --hash=sha256:' + 'd' * 64 + '\n')
    assert mb.check_wheels(wheels, mb.parse_lock(lock)) == ['httpx2-jsfetch']


def test_check_wheels_rejects_a_foreign_a_wrong_version_a_wrong_hash_and_a_missing_wheel(tmp_path):
    wheels = tmp_path/'w'
    wheels.mkdir()
    lock = mb.parse_lock('anyio==4.15.1 \\\n    --hash=sha256:' + sha(b'one') + '\n')
    with pytest.raises(mb.Problem, match='no wheel for: anyio'):
        mb.check_wheels(wheels, lock)
    (wheels/'anyio-4.15.1-py3-none-any.whl').write_bytes(b'other')
    with pytest.raises(mb.Problem, match='hash not in the lock'):
        mb.check_wheels(wheels, lock)
    (wheels/'anyio-4.15.1-py3-none-any.whl').write_bytes(b'one')
    assert mb.check_wheels(wheels, lock) == []
    (wheels/'extra-1.0-py3-none-any.whl').write_bytes(b'x')
    with pytest.raises(mb.Problem, match='not in the lock'):
        mb.check_wheels(wheels, lock)
    (wheels/'extra-1.0-py3-none-any.whl').unlink()
    (wheels/'anyio-4.15.1-py3-none-any.whl').rename(wheels/'anyio-9.0-py3-none-any.whl')
    with pytest.raises(mb.Problem, match='not in the lock'):
        mb.check_wheels(wheels, lock)


# ---- model --------------------------------------------------------------------------------------------------------

def test_model_constants_equal_the_package_manifest():
    entry = models.MANIFEST[mb.MODEL_NAME]
    assert entry['revision'] == mb.MODEL_REVISION
    assert {models.local_name(spec): (spec['size'], spec['sha256']) for spec in entry['files']} == mb.MODEL_FILES


@pytest.fixture
def small_model(monkeypatch):
    files = {'model.onnx': b'one', 'model.onnx_data': b'two', 'Constant_7_attr__value': b'three', 'tokenizer.json': b'4'}
    monkeypatch.setattr(mb, 'MODEL_FILES', {name: (len(data), sha(data)) for name, data in files.items()})
    return files


def test_model_copy_accepts_flat_and_onnx_layout_and_writes_sums(tmp_path, small_model):
    source = tmp_path/'src'
    (source/'onnx').mkdir(parents=True)
    for index, (name, data) in enumerate(small_model.items()):
        (source/('onnx' if index % 2 else '.')/name).write_bytes(data)
    target = mb.build_model(source, tmp_path/'out')
    assert target.name == f'workmcp-model-bge-m3-{mb.MODEL_REVISION[:8]}'
    assert sorted(p.name for p in target.iterdir()) == sorted([*small_model, 'SHA256SUMS'])
    assert mb.verify_folder(target).clean


def test_model_copy_refuses_a_changed_file_and_leaves_no_folder_behind(tmp_path, small_model):
    source = tmp_path/'src'
    source.mkdir()
    for name, data in small_model.items():
        (source/name).write_bytes(data)
    (source/'model.onnx_data').write_bytes(b'XXX')
    with pytest.raises(mb.Problem, match='differs from the manifest'):
        mb.build_model(source, tmp_path/'out')
    target = tmp_path/'out'/f'workmcp-model-bge-m3-{mb.MODEL_REVISION[:8]}'
    assert not target.exists()


def test_model_copy_names_a_missing_file(tmp_path, small_model):
    (tmp_path/'src').mkdir()
    with pytest.raises(mb.Problem, match='not found'):
        mb.build_model(tmp_path/'src', tmp_path/'out')


# ---- make with pip and git replaced -------------------------------------------------------------------------------

@pytest.fixture
def faked(monkeypatch, tmp_path):
    lock = ('anyio==4.15.1 \\\n    --hash=sha256:' + sha(b'anyio wheel') + '\n')
    pyproject = '[project]\nname = "workmcp"\nversion = "9.9.9"\n'
    calls = []
    monkeypatch.setattr(mb, 'resolve_commit', lambda rev: 'abcdef0123456789' * 2 + 'abcdef01')
    monkeypatch.setattr(mb, 'dirty_problem', lambda: None)
    monkeypatch.setattr(mb, 'host_problem', lambda: None)
    monkeypatch.setattr(mb, 'pip_problem', lambda: None)
    monkeypatch.setattr(mb, 'git', lambda *a: lock if a[1].endswith(mb.LOCK) else pyproject)

    def export(commit, folder):
        archive = folder/'src.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr(mb.LOCK, lock)
            z.writestr('pyproject.toml', pyproject)
        calls.append('export')
        return archive

    def download(lock_file, wheels):
        assert lock_file.name == mb.LOCK
        (wheels/'anyio-4.15.1-py3-none-any.whl').write_bytes(b'anyio wheel')
        calls.append('download')

    def build(source, wheels):
        assert (source/'pyproject.toml').is_file()
        (wheels/'workmcp-9.9.9-py3-none-any.whl').write_bytes(b'project wheel')
        calls.append('build')

    monkeypatch.setattr(mb, 'export_commit', export)
    monkeypatch.setattr(mb, 'pip_download', download)
    monkeypatch.setattr(mb, 'build_wheel', build)
    return calls


def test_make_assembles_a_bundle_that_verifies(faked, tmp_path):
    out = tmp_path/'bundles'
    code, stdout, err = run(['make', '--out', str(out), '--zip'])
    assert code == 0, err
    folder, = [p for p in out.iterdir() if p.is_dir()]
    assert folder.name.startswith('workmcp-bundle-9.9.9-') and folder.name.endswith('-abcdef01')
    assert {p.name for p in folder.iterdir()} == {'INSTALL.txt', 'SHA256SUMS', 'src.zip', 'verify_bundle.py',
                                                  'requirements.lock.txt', 'wheels'}
    assert {p.name for p in (folder/'wheels').iterdir()} == {'anyio-4.15.1-py3-none-any.whl',
                                                             'workmcp-9.9.9-py3-none-any.whl'}
    assert (folder/'verify_bundle.py').read_bytes() == TOOL.read_bytes()
    assert faked == ['export', 'download', 'build']
    install = (folder/'INSTALL.txt').read_text(encoding='utf-8')
    assert mb.sha256_file(folder/'SHA256SUMS') in install and 'workmcp==9.9.9' in install and '--require-hashes' in install
    assert '--no-index' in install and 'abcdef01' in install
    assert mb.verify_folder(folder).clean
    assert (out/(folder.name + '.zip')).is_file() and 'zip:' in stdout
    assert run(['make', '--out', str(out)])[0] == 4                      # the same bundle folder is never overwritten


def test_make_dry_run_checks_and_writes_nothing(faked, tmp_path):
    out = tmp_path/'bundles'
    code, stdout, _ = run(['make', '--out', str(out), '--dry-run'])
    assert code == 0 and 'dry run' in stdout and 'would write' in stdout
    assert not out.exists() and faked == []


def test_make_stops_when_a_wheel_is_not_in_the_lock(faked, monkeypatch, tmp_path):
    def download(lock_file, wheels):
        (wheels/'anyio-4.15.1-py3-none-any.whl').write_bytes(b'not the pinned file')
    monkeypatch.setattr(mb, 'pip_download', download)
    code, _, err = run(['make', '--out', str(tmp_path/'bundles')])
    assert code == 1 and 'hash not in the lock' in err


def test_make_refuses_a_dirty_tree_unless_allowed(faked, monkeypatch, tmp_path):
    monkeypatch.setattr(mb, 'dirty_problem', lambda: 'the working tree has uncommitted changes')
    code, _, err = run(['make', '--out', str(tmp_path/'o')])
    assert code == 4 and 'uncommitted' in err
    assert run(['make', '--out', str(tmp_path/'o'), '--allow-dirty'])[0] == 0

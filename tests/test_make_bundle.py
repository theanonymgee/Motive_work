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


FAKE_FILES = {'INSTALL.txt': b'instructions', 'requirements.lock.txt': b'a==1.0\n', 'src.zip': b'zip',
              'verify_bundle.py': b'tool', 'wheels/a-1.0-py3-none-any.whl': b'wheel a',
              'wheels/workmcp-0.1.0-py3-none-any.whl': b'wheel workmcp'}


def fake_folder(root: Path) -> Path:
    """A code bundle with every piece a bundle has to list (fake contents)."""
    folder = root/'bundle'
    (folder/'wheels').mkdir(parents=True)
    for name, data in FAKE_FILES.items():
        (folder/name).write_bytes(data)
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

def test_sums_are_written_in_sha256sum_format_with_install_and_without_itself(tmp_path):
    folder = fake_folder(tmp_path)
    lines = (folder/'SHA256SUMS').read_text(encoding='utf-8').splitlines()
    assert lines == [f'{sha(data)}  {name}' for name, data in sorted(FAKE_FILES.items())]
    assert lines[0].endswith('  INSTALL.txt')                                 # review M6 v1 M6-10: listed
    assert not (folder/'SHA256SUMS').read_bytes().count(b'\r')


def test_verify_passes_on_an_untouched_bundle(tmp_path):
    folder = fake_folder(tmp_path)
    code, out, _ = run(['verify', str(folder)])
    assert code == 0 and out.strip().endswith('ok 6 mismatch 0 missing 0 unlisted 0')


def test_verify_detects_a_changed_a_missing_and_an_added_file(tmp_path):
    folder = fake_folder(tmp_path)
    (folder/'wheels'/'a-1.0-py3-none-any.whl').write_bytes(b'tampered')
    (folder/'src.zip').unlink()
    (folder/'wheels'/'c-3.0-py3-none-any.whl').write_bytes(b'planted')
    code, out, _ = run(['verify', str(folder)])
    assert code == 1
    assert 'mismatch: wheels/a-1.0-py3-none-any.whl' in out and 'missing: src.zip' in out
    assert 'unlisted: wheels/c-3.0-py3-none-any.whl' in out
    assert out.strip().endswith('ok 4 mismatch 1 missing 1 unlisted 1')


def test_verify_detects_a_changed_install_txt(tmp_path):
    """Review M6 v1 M6-10 (probe VB): changing the install commands fails the verification."""
    folder = fake_folder(tmp_path)
    (folder/'INSTALL.txt').write_text('pip install --index-url https://example.invalid workmcp', encoding='utf-8')
    code, out, _ = run(['verify', str(folder)])
    assert code == 1 and 'mismatch: INSTALL.txt' in out


@pytest.mark.parametrize('dropped', ['INSTALL.txt', 'requirements.lock.txt', 'src.zip', 'verify_bundle.py',
                                     'wheels/workmcp-0.1.0-py3-none-any.whl', 'wheels/a-1.0-py3-none-any.whl'])
def test_verify_fails_when_a_required_piece_is_not_listed(tmp_path, dropped):
    """Review M6 v1 M6-10: a bundle that does not list one of its pieces fails, the file removed with its line."""
    folder = fake_folder(tmp_path)
    (folder/dropped).unlink()
    mb.write_sums(folder)
    code, out, _ = run(['verify', str(folder)])
    assert code == 1 and '(required, not listed in SHA256SUMS)' in out and 'missing 1' in out


def test_verify_fails_on_an_empty_sums_file(tmp_path):
    """Review M6 v1 M6-10 (probe VB): an empty ``SHA256SUMS`` was ``ok 0 ...`` and exit 0."""
    folder = tmp_path/'empty'
    folder.mkdir()
    (folder/'SHA256SUMS').write_text('', encoding='utf-8')
    code, out, _ = run(['verify', str(folder)])
    assert code == 1 and out.strip().startswith('missing: INSTALL.txt (required') and 'ok 0' in out


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
    assert code == 0 and 'ok 6' in out
    code, out, _ = run(['verify', str(folder)], script='verify_bundle.py')
    assert code == 0 and 'ok 6' in out


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


def test_marker_pins_are_judged_for_the_target_platform(tmp_path):
    """Review M6 v1 M6-11: a pin whose marker holds on Windows AMD64 CPython 3.12 needs its wheel; one for another
    platform does not; a marker this tool cannot read counts as holding."""
    assert mb.marker_applies("sys_platform == 'win32'") is True
    assert mb.marker_applies("sys_platform == 'emscripten'") is False
    assert mb.marker_applies("sys_platform != 'emscripten'") is True
    assert mb.marker_applies("implementation_name != 'PyPy' and platform_python_implementation != 'PyPy'") is True
    assert mb.marker_applies("platform_machine == 'arm64' or platform_machine == 'AMD64'") is True
    assert mb.marker_applies("python_version >= '3.8'") is None
    assert mb.marker_applies("(sys_platform == 'win32')") is None
    wheels = tmp_path/'w'
    wheels.mkdir()
    (wheels/'anyio-4.15.1-py3-none-any.whl').write_bytes(b'one')
    lock = ('anyio==4.15.1 \\\n    --hash=sha256:' + sha(b'one') + '\npywin32==312 ; sys_platform == \'win32\' \\\n'
            '    --hash=sha256:' + sha(b'two') + '\n')
    with pytest.raises(mb.Problem, match='no wheel for: pywin32'):
        mb.check_wheels(wheels, mb.parse_lock(lock))


def test_every_marker_of_the_real_lock_can_be_judged():
    pins = mb.parse_lock((REPO/'requirements.lock.txt').read_text(encoding='utf-8'))
    judged = {pin.name: mb.marker_applies(pin.marker) for pin in pins.values() if pin.marker}
    assert judged and None not in judged.values(), judged
    assert judged['pywin32'] is True and judged['httpx2-jsfetch'] is False


def test_the_release_number_is_one_value():
    """Review M6 v1 M6-1: ``pyproject.toml``, ``workmcp.__version__`` and the push client version are the same release
    number (design M6 §5.1: raised with every release, so an offline update never meets an equal installed version)."""
    import tomllib
    from workmcp import __version__, push
    project = tomllib.loads((REPO/'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    assert project == __version__ == push.CLIENT_VERSION and project != '0.0.1'


def test_the_build_lock_pins_the_build_backend_of_pyproject_with_a_hash():
    """Review M6 v2 M6-9: ``requirements-build.lock.txt`` pins exactly ``[build-system] requires``, each with a hash."""
    import tomllib
    requires = tomllib.loads((REPO/'pyproject.toml').read_text(encoding='utf-8'))['build-system']['requires']
    pins = mb.parse_lock((REPO/mb.BUILD_LOCK).read_text(encoding='utf-8'))
    assert sorted(f'{pin.name}=={pin.version}' for pin in pins.values()) == sorted(requires) == ['setuptools==84.2.1']
    assert pins['setuptools'].hashes == {'ca140b45d5325a9478f880e351484f62fef15f336462e449ca0c03c873d589c2'}


FETCHING_PIP = frozenset({'install', 'download', 'wheel'})


def unchecked_fetch(command: list[str]) -> bool:
    """A pip command that may fetch a package without checking its hash: ``install``/``download``/``wheel`` with
    neither ``--require-hashes`` nor ``--no-index``, or a ``wheel`` that keeps build isolation (whose build
    environment pip fills from the index by version only)."""
    if 'pip' not in command or not FETCHING_PIP & set(command):
        return False
    if 'wheel' in command and '--no-build-isolation' not in command:
        return True
    return '--require-hashes' not in command and '--no-index' not in command


def test_unchecked_fetch_flags_the_old_build_command():
    old = ['py', '-I', '-m', 'pip', 'wheel', '--no-deps', '--no-input', '-w', 'wheels', 'src']
    assert unchecked_fetch(old)
    assert unchecked_fetch(['py', '-m', 'pip', 'install', 'setuptools==84.2.1'])
    assert unchecked_fetch(['py', '-m', 'pip', 'wheel', '--no-build-isolation', 'src'])
    assert not unchecked_fetch(['py', '-m', 'pip', '--version'])


def test_no_pip_command_of_make_fetches_a_build_dependency_without_a_hash(tmp_path, monkeypatch):
    """Review M6 v2 M6-9, without network: the commands ``make`` runs (recorded, nothing runs) download the lock with
    ``--require-hashes``, install the build backend into a fresh venv with ``--require-hashes`` and build there with
    ``--no-build-isolation --no-index``; and no pip command in the tool's source is an unchecked fetch."""
    import ast
    commands = []
    monkeypatch.setattr(mb, 'run_step', lambda label, command: commands.append(command))
    source = tmp_path/'src'
    source.mkdir()
    (source/mb.BUILD_LOCK).write_text((REPO/mb.BUILD_LOCK).read_text(encoding='utf-8'), encoding='utf-8')
    mb.pip_download(source/mb.LOCK, tmp_path/'wheels')
    mb.build_wheel(source, tmp_path/'wheels', tmp_path)
    assert [command[3:5] for command in commands] == [['pip', 'download'], ['venv', str(tmp_path/'build-env')],
                                                      ['pip', 'install'], ['pip', 'wheel']]
    assert not [command for command in commands if unchecked_fetch(command)], commands
    install, wheel = commands[2], commands[3]
    assert install[-2:] == ['-r', str(source/mb.BUILD_LOCK)] and '--require-hashes' in install
    assert install[0] == wheel[0] and 'build-env' in install[0]          # the build runs in that venv, not here
    assert {'--no-build-isolation', '--no-index', '--no-deps'} <= set(wheel)
    tree = ast.parse(TOOL.read_text(encoding='utf-8'))
    lists = [[e.value for e in node.elts if isinstance(e, ast.Constant)] for node in ast.walk(tree)
             if isinstance(node, ast.List)]
    pip_lists = [items for items in lists if 'pip' in items and FETCHING_PIP & set(items)]
    assert len(pip_lists) == 3 and not [items for items in pip_lists if unchecked_fetch(items)], pip_lists


@pytest.mark.parametrize('text', [None, '', 'setuptools==84.2.1\n'])
def test_build_wheel_needs_a_build_lock_with_hashes(tmp_path, monkeypatch, text):
    commands = []
    monkeypatch.setattr(mb, 'run_step', lambda label, command: commands.append(command))
    if text is not None:
        (tmp_path/mb.BUILD_LOCK).write_text(text, encoding='utf-8')
    with pytest.raises(mb.Problem, match='missing or has a pin without a hash'):
        mb.build_wheel(tmp_path, tmp_path/'wheels', tmp_path)
    assert commands == []


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

    def build(source, wheels, work):
        assert (source/'pyproject.toml').is_file() and work.is_dir()
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
    sums_sha256 = mb.sha256_file(folder/'SHA256SUMS')
    # review M6 v1 M6-10: INSTALL.txt is listed, so it never holds the SUMS hash; make prints it to write down
    assert sums_sha256 not in install and '  INSTALL.txt' in (folder/'SHA256SUMS').read_text(encoding='utf-8')
    assert f'SHA256SUMS sha256 {sums_sha256}' in stdout and 'write this value down' in stdout
    assert install.index('certutil') < install.index('verify_bundle.py')       # compare first, then run its tool
    assert 'py -3.12 -I verify_bundle.py .' in install                         # M6-15 b: no bare python
    # review M6 v1 M6-1: the same version number still replaces the code
    assert '--force-reinstall --no-deps workmcp==9.9.9' in install and '--require-hashes' in install
    assert '--no-index' in install and 'abcdef01' in install and '--upgrade' in install
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

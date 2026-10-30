"""Data home rules (design §2.2, §8.4 A). Cases call ``validate_home`` directly with explicit env."""
import os
import sys
import unicodedata
from pathlib import Path, PurePosixPath

import pytest

from workmcp import cli, paths, server
from workmcp.paths import DataPathError, ensure_home, validate_home

REPO_ROOT = Path(__file__).resolve().parents[1]


def code_of(raw, env=None, repo_root=REPO_ROOT):
    with pytest.raises(DataPathError) as info:
        validate_home(raw, env or {}, repo_root)
    return info.value.code


def test_repo_root_is_found_from_the_package():
    assert paths.find_repo_root() == REPO_ROOT


def deny_stat(monkeypatch, *denied: Path):
    """``os.stat`` raises EACCES for these paths, as for ``/root/.git`` under ``sudo -u workmcp`` (VPS 2026-10-30).

    Python 3.12 ``Path.exists``/``is_file``/``is_dir`` re-raise it. No real permission is changed.
    """
    real, keys = os.stat, {os.path.normcase(str(path)) for path in denied}

    def stat(path, *args, **kwargs):
        if os.path.normcase(os.fspath(path)) in keys:
            raise PermissionError(13, 'Permission denied (synthetic)', os.fspath(path))
        return real(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', stat)


def site_packages_file(tmp_path) -> Path:
    """A package file as a non-editable install has it: no ``pyproject.toml`` above it."""
    return tmp_path/'venv'/'lib'/'python3.12'/'site-packages'/'workmcp'/'paths.py'


def test_repo_root_skips_an_ancestor_it_cannot_stat(tmp_path, monkeypatch):
    sub = (tmp_path/'root'/'sub').resolve()
    sub.mkdir(parents=True)
    (sub.parent/'.git').mkdir()
    deny_stat(monkeypatch, sub.parent/'.git')
    with pytest.raises(PermissionError):
        (sub.parent/'.git').exists()                     # the 3.12 behaviour the fix guards against
    assert paths.find_repo_root(site_packages_file(tmp_path), cwd=sub) is None


def test_repo_root_finds_a_git_ancestor_of_cwd(tmp_path):
    work = (tmp_path/'clone'/'a'/'b').resolve()
    work.mkdir(parents=True)
    (tmp_path/'clone'/'.git').mkdir()
    assert paths.find_repo_root(site_packages_file(tmp_path), cwd=work) == (tmp_path/'clone').resolve()


def test_repo_root_without_a_cwd_is_none(tmp_path, monkeypatch):
    def gone():
        raise FileNotFoundError(2, 'No such file or directory (synthetic: cwd deleted)')
    monkeypatch.setattr(Path, 'cwd', staticmethod(gone))
    assert paths.find_repo_root(site_packages_file(tmp_path)) is None
    assert paths.find_repo_root() == REPO_ROOT          # the pyproject chain needs no cwd


def test_pyproject_chain_wins_over_a_denied_cwd_ancestor(tmp_path, monkeypatch):
    sub = (tmp_path/'root'/'sub').resolve()
    sub.mkdir(parents=True)
    deny_stat(monkeypatch, sub.parent/'.git')
    assert paths.find_repo_root(cwd=sub) == REPO_ROOT
    assert code_of(str(REPO_ROOT/'data'), repo_root=paths.find_repo_root(cwd=sub)) == 'home_inside_repo'


def test_home_under_an_ancestor_that_cannot_be_stated_is_judged_without_it(tmp_path, monkeypatch):
    locked = (tmp_path/'locked').resolve()
    locked.mkdir()
    deny_stat(monkeypatch, locked)
    assert validate_home(str(locked/'wk'), {}, REPO_ROOT) == locked/'wk'
    monkeypatch.undo()
    real = paths._file_attributes

    def attributes(path):                               # exists() passes, the attribute read fails
        if Path(path) == locked:
            raise PermissionError(13, 'Permission denied (synthetic)', str(path))
        return real(path)
    monkeypatch.setattr(paths, '_file_attributes', attributes)
    assert validate_home(str(locked/'wk'), {}, REPO_ROOT) == locked/'wk'


def test_token_issue_from_a_denied_cwd_with_a_site_packages_install(tmp_path, monkeypatch, capsys):
    """The VPS case end to end: non-editable install, run from a cwd whose ``.git`` cannot be stat'ed."""
    sub = (tmp_path/'root'/'sub').resolve()
    sub.mkdir(parents=True)
    monkeypatch.setattr(paths.find_repo_root, '__defaults__', (site_packages_file(tmp_path), None))
    monkeypatch.chdir(sub)
    deny_stat(monkeypatch, sub.parent/'.git')
    assert cli.main(['token', 'issue', '--kind', 'mcp', '--name', 'synthetic-client']) == 0
    out, err = capsys.readouterr()
    assert err == '' and '(shown once)' in out


def test_cli_turns_a_stray_os_error_into_one_stderr_line(monkeypatch, capsys):
    def doctor(home=None):
        raise PermissionError(13, 'Permission denied (synthetic)', '/root/.git')
    monkeypatch.setattr(cli, 'doctor', doctor)
    assert cli.main(['doctor']) == 1
    out, err = capsys.readouterr()
    assert out == '' and 'Traceback' not in err
    assert err == "workmcp: PermissionError: [Errno 13] Permission denied (synthetic): '/root/.git'\n"


def profile_env(tmp_path):
    profile = tmp_path/'profile'
    return profile, {'USERPROFILE': str(profile), 'LOCALAPPDATA': str(profile/'AppData'/'Local'),
                     'APPDATA': str(profile/'AppData'/'Roaming')}


def test_default_is_userprofile_motive_work(tmp_path):
    """M5 design §12 Q4: the default home moved out of AppData to ``%USERPROFILE%\\Motive_work``."""
    profile, env = profile_env(tmp_path)
    home = validate_home(None, env, REPO_ROOT)
    assert home == (profile/'Motive_work').resolve()
    assert not paths.under_appdata(home, env)


def test_empty_value_means_default(tmp_path):
    profile, env = profile_env(tmp_path)
    assert validate_home('', env, REPO_ROOT) == (profile/'Motive_work').resolve()


def test_missing_userprofile_is_unresolvable(tmp_path):
    assert code_of(None, {}) == 'home_unresolvable'
    # LOCALAPPDATA alone no longer gives a default (the old default lay under AppData)
    assert code_of(None, {'LOCALAPPDATA': str(tmp_path/'AppData'/'Local')}) == 'home_unresolvable'


def test_under_appdata_marks_every_appdata_form(tmp_path):
    profile, env = profile_env(tmp_path)
    for home in (profile/'AppData'/'Local'/'Motive_work', profile/'AppData'/'Roaming'/'Motive_work',
                 profile/'AppData'/'LocalLow'/'Motive_work',
                 profile/'AppData'/'Local'/'Packages'/'Fake_pkg'/'LocalCache'/'Local'/'Motive_work'):
        assert paths.under_appdata(home, env), home
    for home in (profile/'Motive_work', tmp_path/'elsewhere'/'Motive_work', profile/'AppDataX'/'Motive_work'):
        assert not paths.under_appdata(home, env), home
    # an explicit AppData home is still a valid home (doctor reports virtualization; setup refuses it)
    assert validate_home(str(profile/'AppData'/'Local'/'Motive_work'), env, REPO_ROOT)


def test_explicit_home_is_returned_resolved(tmp_path):
    assert validate_home(str(tmp_path/'wk'), {}, REPO_ROOT) == (tmp_path/'wk').resolve()


@pytest.mark.parametrize('raw', ['data', r'.\data', '~/wk', r'C:relative', 'C:', r'\x'])
def test_relative_path_is_refused(raw):
    assert code_of(raw) == 'home_relative'


@pytest.mark.parametrize('raw', [r'\\server\share\x', '//server/share/x', r'\\localhost\C$\x'])
def test_unc_path_is_refused(raw):
    assert code_of(raw) == 'home_unc'


def test_home_inside_repo_is_refused():
    assert code_of(str(REPO_ROOT/'data')) == 'home_inside_repo'
    assert code_of(str(REPO_ROOT)) == 'home_inside_repo'


def test_repo_inside_home_is_refused():
    assert code_of(str(REPO_ROOT.parent)) == 'home_inside_repo'


def test_repo_check_ignores_case_on_windows():
    if os.name != 'nt':
        pytest.skip('case-insensitive paths are a Windows property')
    assert code_of(str(REPO_ROOT/'data').upper()) == 'home_inside_repo'


@pytest.mark.parametrize('folder', ['OneDrive - 회사', 'OneDrive', 'onedrive - personal', 'Dropbox', 'Google Drive',
                                    'GoogleDrive', 'iCloudDrive', 'iCloud Drive', 'Nextcloud', 'Box',
                                    'Dropbox (회사)', 'Dropbox (팀)', 'Dropbox (Personal)', 'My Drive', 'my drive',
                                    'MyDrive', '내 드라이브', '내드라이브', 'GoogleDriveFS', 'MYBOX', 'MyBox',
                                    'Shared drives', 'shared drives', 'SharedDrives', '공유 드라이브', '공유드라이브',
                                    unicodedata.normalize('NFD', '내 드라이브'),
                                    unicodedata.normalize('NFD', '공유 드라이브')])
def test_sync_folder_component_is_refused(tmp_path, folder):
    assert code_of(str(tmp_path/folder/'x')) == 'home_sync_component'


@pytest.mark.parametrize('name', ['OneDrive', 'OneDriveConsumer', 'OneDriveCommercial', 'OneDriveCommercial2'])
def test_sync_env_folder_is_refused(tmp_path, name):
    env = {name: str(tmp_path/'od')}
    assert code_of(str(tmp_path/'od'/'x'), env) == 'home_sync_env'
    assert code_of(str(tmp_path/'od'), env) == 'home_sync_env'
    assert validate_home(str(tmp_path/'other'), env, REPO_ROOT) == (tmp_path/'other').resolve()


LONG = '\\\\?\\'  # the Windows \\?\ prefix


@pytest.mark.parametrize('make', [lambda t: str(REPO_ROOT/'data.'), lambda t: str(REPO_ROOT) + ' ' + os.sep + 'data',
                                  lambda t: str(REPO_ROOT) + '.' + os.sep + 'data', lambda t: str(t/'OneDrive.'/'wk'),
                                  lambda t: str(t/'ok '/'wk'), lambda t: str(t/'wk..'), lambda t: str(t/'wk. ')])
def test_trailing_dot_or_space_component_is_refused(tmp_path, make):
    assert code_of(make(tmp_path)) == 'home_trailing_dot_space'


def test_dot_and_dotdot_components_are_not_trailing_dots(tmp_path):
    sep = os.sep
    assert validate_home(sep.join([str(tmp_path), '.', 'a', '..', 'wk']), {}, REPO_ROOT) == (tmp_path/'wk').resolve()
    assert code_of(sep.join([str(REPO_ROOT), '..', REPO_ROOT.name, 'data'])) == 'home_inside_repo'


def test_long_path_prefix_is_judged_on_the_local_path(tmp_path):
    if os.name != 'nt':
        pytest.skip('the long-path prefix is a Windows form')
    assert validate_home(LONG + str(tmp_path/'ok'), {}, REPO_ROOT) == (tmp_path/'ok').resolve()
    assert code_of(LONG + str(REPO_ROOT/'data')) == 'home_inside_repo'
    assert code_of(LONG + str(tmp_path/'OneDrive - 회사'/'wk')) == 'home_sync_component'
    assert code_of(LONG + str(REPO_ROOT) + r' \data') == 'home_trailing_dot_space'


@pytest.mark.parametrize('raw', [LONG + r'UNC\server\share\x', r'\\.\C:\x',
                                 LONG + r'Volume{00000000-0000-0000-0000-000000000000}\x'])
def test_unc_and_device_prefixes_stay_refused(raw):
    assert code_of(raw) == 'home_unc'


def short_name(path: Path) -> str | None:
    """Windows 8.3 form of an existing path, or None when the volume has no short names (test-only ctypes)."""
    if os.name != 'nt':
        return None
    import ctypes
    buffer = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
        return None
    return buffer.value if buffer.value.casefold() != str(path).casefold() else None


def test_short_8dot3_names_are_judged_by_the_long_target(tmp_path):
    repo = short_name(REPO_ROOT)
    synced = tmp_path/'OneDrive - 회사 long folder'
    synced.mkdir()
    synced_short = short_name(synced)
    if repo is None and synced_short is None:
        pytest.skip('8.3 short names are disabled on this volume')
    if repo is not None:
        assert code_of(repo + r'\data') == 'home_inside_repo'
    if synced_short is not None:
        assert '~' in synced_short
        assert code_of(synced_short + r'\wk') == 'home_sync_component'


ON_WINDOWS_ONLY = pytest.mark.skipif(sys.platform != 'win32', reason='drive letters exist on Windows only')


@pytest.mark.parametrize('drive, system, warned', [('C:', 'C:', False), ('c:', 'C:', False),
                                                    pytest.param('G:', 'C:', True, marks=ON_WINDOWS_ONLY),
                                                    pytest.param('D:', 'C:', True, marks=ON_WINDOWS_ONLY),
                                                    ('G:', None, False)])
def test_doctor_warns_when_home_is_off_the_system_drive(drive, system, warned):
    env = {'SystemDrive': system} if system else {}
    message = paths.drive_warning(Path(drive + '\\wk'), env)
    assert (message is not None) is warned
    if warned:
        assert drive in message and 'Google Drive' in message


def test_posix_home_has_no_drive_warning():
    assert paths.drive_warning(PurePosixPath('/srv/workmcp/wk'), {'SystemDrive': 'C:'}) is None


@pytest.mark.parametrize('raw, code', [('wk', 'home_relative'), ('./wk', 'home_relative'), ('../wk', 'home_relative'),
                                       ('//server/share/wk', 'home_unc')])
def test_relative_and_slash_network_forms_are_refused(raw, code):
    assert code_of(raw) == code


def test_sync_attribute_is_refused(tmp_path, monkeypatch):
    marked = (tmp_path/'cloud').resolve()
    marked.mkdir()

    def attributes(path):
        return paths.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS if Path(path) == marked else 0

    monkeypatch.setattr(paths, '_file_attributes', attributes)
    assert code_of(str(marked/'x')) == 'home_sync_attr'
    assert validate_home(str(tmp_path/'plain'), {}, REPO_ROOT) == (tmp_path/'plain').resolve()


def make_link(link: Path, target: Path):
    """Directory symlink, else a junction (no privilege needed on Windows), else skip."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError) as exc:
        reason = exc
    try:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, OSError) as exc:
        pytest.skip(f'cannot create a symlink or junction here: {reason}; {exc}')


def test_symlink_to_sync_folder_is_judged_by_target(tmp_path):
    target = tmp_path/'OneDrive'/'x'
    target.mkdir(parents=True)
    make_link(tmp_path/'link', target)
    assert code_of(str(tmp_path/'link')) == 'home_sync_component'
    assert code_of(str(tmp_path/'link'/'deeper')) == 'home_sync_component'


def test_symlink_into_repo_is_judged_by_target(tmp_path):
    link = tmp_path/'repo-link'
    make_link(link, REPO_ROOT)
    try:
        assert code_of(str(link/'data')) == 'home_inside_repo'
    finally:
        # removes only the link (a Windows junction goes by rmdir, a POSIX symlink by unlink), so later tmp cleanup never
        # walks into the repository
        os.rmdir(link) if os.name == 'nt' else os.unlink(link)
    assert (REPO_ROOT/'pyproject.toml').is_file()


def test_ensure_home_creates_subfolders(tmp_path):
    home = ensure_home(validate_home(str(tmp_path/'ok'), {}, REPO_ROOT))
    assert sorted(p.name for p in home.iterdir()) == ['db', 'logs', 'models', 'tmp']


def test_validation_failure_creates_nothing(tmp_path):
    target = tmp_path/'Dropbox'/'wk'
    assert code_of(str(target)) == 'home_sync_component'
    assert not (tmp_path/'Dropbox').exists()


@pytest.mark.parametrize('command', ['doctor', 'serve'])
def test_cli_refuses_bad_home_with_exit_2_and_empty_stdout(tmp_path, monkeypatch, capsys, command):
    bad = tmp_path/'OneDrive - 회사'/'wk'
    monkeypatch.setenv('WORKMCP_HOME', str(bad))
    assert cli.main([command]) == 2
    out, err = capsys.readouterr()
    assert out == ''
    assert 'home_sync_component' in err
    assert not bad.exists()


def test_server_main_refuses_relative_home(monkeypatch, capsys):
    monkeypatch.setenv('WORKMCP_HOME', 'data')
    assert server.main() == 2
    out, err = capsys.readouterr()
    assert out == '' and 'home_relative' in err


def test_doctor_reports_valid_home_without_creating_it(workmcp_home, capsys):
    assert cli.main(['doctor']) == 0
    out, err = capsys.readouterr()
    assert str(workmcp_home.resolve()) in out and 'not created yet' in out
    assert not workmcp_home.exists()


def test_real_default_home_passes_on_this_pc():
    env = {k: v for k, v in os.environ.items() if k != paths.HOME_ENV}
    if not env.get('USERPROFILE'):
        pytest.skip('USERPROFILE is not set (not a Windows user session)')
    home = validate_home(None, env, REPO_ROOT)
    assert home == Path(env['USERPROFILE']).resolve()/'Motive_work'
    assert not paths.under_appdata(home, env)


# ---- app virtualization (design M3 §10.3, V-1..V-3) ------------------------------------------------

def fake_profile(tmp_path):
    """A fake user profile: ``AppData/Local`` with a ``Packages`` folder of three packages."""
    profile = tmp_path/'profile'
    local = profile/'AppData'/'Local'
    for name in ('Fake_pkg', 'Other_pkg', 'Third_pkg'):
        (local/'Packages'/name/'LocalCache'/'Local').mkdir(parents=True)
    env = {'LOCALAPPDATA': str(local), 'APPDATA': str(profile/'AppData'/'Roaming'), 'USERPROFILE': str(profile)}
    return profile, local, env


def tree(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob('*'))


def test_virtualization_probe_outside_appdata_is_not_applicable(tmp_path):
    profile, _, env = fake_profile(tmp_path)
    home = ensure_home(profile/'Motive_work')
    assert paths.virtualization_probe(home, env) == {'checked': True, 'under_appdata': False, 'redirected_to': None,
                                                     'package': None}
    assert tree(home/'tmp') == []
    assert paths.virtualization_line(home, paths.virtualization_probe(home, env)) == \
        'app virtualization: none (home outside AppData)'


def test_virtualization_probe_detects_redirect_into_a_package(tmp_path, monkeypatch):
    profile, local, env = fake_profile(tmp_path)
    home = ensure_home(local/'Motive_work')
    assert paths.virtualization_probe(home, env) == {'checked': True, 'under_appdata': True, 'redirected_to': None,
                                                     'package': None}
    real = paths._write_marker
    package_home = local/'Packages'/'Other_pkg'/'LocalCache'/'Local'/'Motive_work'

    def redirected(path):  # what the packaged app's file system does: the write lands in its folder
        real(path)
        copy = package_home/'tmp'/path.name
        copy.parent.mkdir(parents=True, exist_ok=True)
        real(copy)

    monkeypatch.setattr(paths, '_write_marker', redirected)
    probe = paths.virtualization_probe(home, env)
    assert probe == {'checked': True, 'under_appdata': True, 'redirected_to': str(package_home),
                     'package': 'Other_pkg'}
    line = paths.virtualization_line(home, probe)
    assert line.startswith('warning: app virtualization:') and 'Other_pkg' in line and paths.HOME_ENV in line
    roaming = ensure_home(profile/'AppData'/'Roaming'/'Motive_work')
    roaming_copy = local/'Packages'/'Fake_pkg'/'LocalCache'/'Roaming'/'Motive_work'

    def redirected_roaming(path):
        real(path)
        (roaming_copy/'tmp').mkdir(parents=True, exist_ok=True)
        real(roaming_copy/'tmp'/path.name)

    monkeypatch.setattr(paths, '_write_marker', redirected_roaming)
    assert paths.virtualization_probe(roaming, env)['redirected_to'] == str(roaming_copy)
    # inside the packaged app the resolved home is already the package folder (dev PC, 2026-10-24)
    monkeypatch.setattr(paths, '_write_marker', real)
    inside = ensure_home(package_home)
    before = tree(inside)  # holds the copy the fake redirection above left behind
    assert paths.virtualization_probe(inside, env) == {'checked': True, 'under_appdata': True,
                                                       'redirected_to': str(inside), 'package': 'Other_pkg'}
    assert tree(inside) == before  # decided from the path: no marker written


def test_virtualization_probe_cleans_its_marker(tmp_path, monkeypatch):
    profile, local, env = fake_profile(tmp_path)
    home = ensure_home(local/'Motive_work')
    before = tree(profile)
    assert paths.virtualization_probe(home, env)['checked'] is True
    assert tree(profile) == before                       # marker removed, nothing made anywhere else
    real_scandir = os.scandir

    def unlistable(path):
        if Path(path) == local/'Packages':
            raise PermissionError('denied')
        return real_scandir(path)

    monkeypatch.setattr(paths.os, 'scandir', unlistable)
    failed = paths.virtualization_probe(home, env)
    monkeypatch.undo()
    assert failed == {'checked': False, 'under_appdata': True, 'redirected_to': None, 'package': None}
    assert tree(profile) == before                       # removed on the failure path too
    missing = local/'Not_yet'
    probe = paths.virtualization_probe(missing, env)
    assert (probe['checked'], probe['under_appdata']) == (False, True) and not missing.exists()
    assert paths.virtualization_line(missing, probe).startswith('app virtualization: not checked')
    package = local/'Packages'/'Fake_pkg'/'LocalCache'/'Local'/'Motive_work'
    assert 'Fake_pkg' in paths.appdata_warning(package, env)
    assert paths.appdata_warning(profile/'Motive_work', env) is None


def test_doctor_reports_app_virtualization(workmcp_home, monkeypatch, capsys, tmp_path):
    profile, local, env = fake_profile(tmp_path)
    home = ensure_home(profile/'Motive_work')
    monkeypatch.setenv('WORKMCP_HOME', str(home))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert cli.main(['doctor']) == 0
    lines = capsys.readouterr().out.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith('data home:'))
    assert lines[at + 1] == 'app virtualization: none (home outside AppData)'
    inside = ensure_home(local/'Packages'/'Fake_pkg'/'LocalCache'/'Local'/'Motive_work')
    monkeypatch.setenv('WORKMCP_HOME', str(inside))
    assert cli.main(['doctor']) == 0
    out = capsys.readouterr().out
    assert f'warning: app virtualization: the data home {inside} lands in package Fake_pkg ({inside})' in out
    assert 'is inside the app package folder' in out


# ---- polluted shell (design M5 §2.7) ------------------------------------------------------------------

def test_environment_lines_warn_about_pythonpath_and_a_foreign_virtual_env(tmp_path):
    venv = str(tmp_path/'repo'/'.venv')
    assert cli.environment_lines({}, prefix=venv, isolated=True) == [
        'env: clean (no PYTHONPATH, no foreign VIRTUAL_ENV)']
    assert cli.environment_lines({'VIRTUAL_ENV': venv, 'PYTHONPATH': ''}, prefix=venv, isolated=False) == [
        'env: clean (no PYTHONPATH, no foreign VIRTUAL_ENV)']  # this venv activated, empty PYTHONPATH
    other = str(tmp_path/'other'/'venv')
    lines = cli.environment_lines({'PYTHONPATH': other, 'VIRTUAL_ENV': other}, prefix=venv, isolated=True)
    assert len(lines) == 2 and all(line.startswith('warning: env: ') for line in lines)
    assert 'PYTHONPATH is set (ignored here by python -I' in lines[0]
    assert lines[1] == f"warning: env: VIRTUAL_ENV={other} is not this interpreter's environment ({venv})"
    used = cli.environment_lines({'PYTHONPATH': other}, prefix=venv, isolated=False)
    assert used[0].startswith('warning: env: PYTHONPATH is set and used by this process')


def test_venv_label_names_this_repository(tmp_path):
    repo = tmp_path/'repo'
    assert cli.venv_label(str(repo/'.venv'), str(tmp_path/'py'), repo) == 'this repository'
    assert cli.venv_label(str(tmp_path/'py'), str(tmp_path/'py'), repo) == 'none (base interpreter)'
    assert cli.venv_label(str(tmp_path/'v'), str(tmp_path/'py'), repo) == str(tmp_path/'v')
    assert cli.venv_label(str(tmp_path/'v'), str(tmp_path/'py'), None) == str(tmp_path/'v')


def test_doctor_prints_the_environment_lines(workmcp_home, monkeypatch, capsys):
    monkeypatch.setenv('PYTHONPATH', r'C:\elsewhere\site-packages')
    monkeypatch.delenv('VIRTUAL_ENV', raising=False)
    assert cli.main(['doctor']) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith('python: ') and '  venv: ' in lines[1]
    assert lines[2].startswith('warning: env: PYTHONPATH is set')
    monkeypatch.delenv('PYTHONPATH')
    assert cli.main(['doctor']) == 0
    assert capsys.readouterr().out.splitlines()[2] == 'env: clean (no PYTHONPATH, no foreign VIRTUAL_ENV)'

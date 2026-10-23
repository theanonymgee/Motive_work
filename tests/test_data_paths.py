"""Data home rules (design §2.2, §8.4 A). Cases call ``validate_home`` directly with explicit env."""
import os
from pathlib import Path

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


def test_default_is_localappdata_motive_work(tmp_path):
    env = {'LOCALAPPDATA': str(tmp_path/'AppData'/'Local')}
    assert validate_home(None, env, REPO_ROOT) == (tmp_path/'AppData'/'Local'/'Motive_work').resolve()


def test_empty_value_means_default(tmp_path):
    env = {'LOCALAPPDATA': str(tmp_path/'AppData'/'Local')}
    assert validate_home('', env, REPO_ROOT) == (tmp_path/'AppData'/'Local'/'Motive_work').resolve()


def test_missing_localappdata_is_unresolvable():
    assert code_of(None, {}) == 'home_unresolvable'


def test_explicit_home_is_returned_resolved(tmp_path):
    assert validate_home(str(tmp_path/'wk'), {}, REPO_ROOT) == (tmp_path/'wk').resolve()


@pytest.mark.parametrize('raw', ['data', r'.\data', '~/wk', r'C:relative'])
def test_relative_path_is_refused(raw):
    assert code_of(raw) == 'home_relative'


@pytest.mark.parametrize('raw', [r'\\server\share\x', '//server/share/x'])
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
                                    'MyDrive', '내 드라이브', '내드라이브', 'GoogleDriveFS', 'MYBOX', 'MyBox'])
def test_sync_folder_component_is_refused(tmp_path, folder):
    assert code_of(str(tmp_path/folder/'x')) == 'home_sync_component'


@pytest.mark.parametrize('name', ['OneDrive', 'OneDriveConsumer', 'OneDriveCommercial', 'OneDriveCommercial2'])
def test_sync_env_folder_is_refused(tmp_path, name):
    env = {name: str(tmp_path/'od')}
    assert code_of(str(tmp_path/'od'/'x'), env) == 'home_sync_env'
    assert code_of(str(tmp_path/'od'), env) == 'home_sync_env'
    assert validate_home(str(tmp_path/'other'), env, REPO_ROOT) == (tmp_path/'other').resolve()


LONG = '\\\\?\\'  # the Windows \\?\ prefix


@pytest.mark.parametrize('make', [lambda t: str(REPO_ROOT/'data.'), lambda t: str(REPO_ROOT) + r' \data',
                                  lambda t: str(REPO_ROOT) + r'.\data', lambda t: str(t/'OneDrive.'/'wk'),
                                  lambda t: str(t/'ok '/'wk'), lambda t: str(t/'wk..'), lambda t: str(t/'wk. ')])
def test_trailing_dot_or_space_component_is_refused(tmp_path, make):
    assert code_of(make(tmp_path)) == 'home_trailing_dot_space'


def test_dot_and_dotdot_components_are_not_trailing_dots(tmp_path):
    assert validate_home(str(tmp_path) + r'\.\a\..\wk', {}, REPO_ROOT) == (tmp_path/'wk').resolve()
    assert code_of(str(REPO_ROOT) + '\\..\\' + REPO_ROOT.name + r'\data') == 'home_inside_repo'


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


@pytest.mark.parametrize('drive, system, warned', [('C:', 'C:', False), ('c:', 'C:', False), ('G:', 'C:', True),
                                                    ('D:', 'C:', True), ('G:', None, False)])
def test_doctor_warns_when_home_is_off_the_system_drive(drive, system, warned):
    env = {'SystemDrive': system} if system else {}
    message = paths.drive_warning(Path(drive + '\\wk'), env)
    assert (message is not None) is warned
    if warned:
        assert drive in message and 'Google Drive' in message


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
        os.rmdir(link)  # removes only the link, so later tmp cleanup never walks into the repository
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
    if not env.get('LOCALAPPDATA'):
        pytest.skip('LOCALAPPDATA is not set (not a Windows user session)')
    home = validate_home(None, env, REPO_ROOT)
    assert home.name == 'Motive_work'

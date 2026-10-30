"""Single place that decides and checks where our data lives (design §2).

Data home = ``WORKMCP_HOME`` if set, else ``%USERPROFILE%\\Motive_work`` (M5 design §2.4, §12 Q4:
outside ``AppData``, so a packaged client app cannot redirect it). The home must be an absolute,
local path outside the repository and outside any sync folder; otherwise ``validate_home`` raises
``DataPathError(code, message)`` and nothing is created. A home under ``AppData`` stays valid here
(``doctor`` reports app virtualization); ``workmcp setup`` refuses it (``under_appdata``). These
rules apply to our storage only, never to the source folders we read.
"""
from __future__ import annotations

import os
import unicodedata
from collections.abc import Mapping
from pathlib import Path, PurePath

HOME_ENV = 'WORKMCP_HOME'
APP_DIR = 'Motive_work'
DEFAULT_BASE_ENV = 'USERPROFILE'  # default home = %USERPROFILE%\Motive_work (M5 design §12 Q4)
SUBDIRS = ('db', 'models', 'logs', 'tmp')

# Compared after casefold + strip, and again with inner spaces removed. Google Drive for desktop
# mirrors into 'My Drive' ('내 드라이브' in the Korean UI) and streams team drives under 'Shared drives'
# ('공유 드라이브'); Dropbox team folders are 'Dropbox (team)'. Names are compared in NFC form.
SYNC_NAMES = frozenset({
    'onedrive', 'onedrive - personal', 'dropbox', 'googledrive', 'google drive', 'mygoogledrive', 'my drive',
    '내 드라이브', 'shared drives', '공유 드라이브', 'googledrivefs', 'iclouddrive', 'icloud drive', 'box', 'boxsync',
    'sync', 'seafile', 'nextcloud', 'owncloud', 'mega', 'pcloud', 'tresorit', 'synologydrive', 'mybox'})
SYNC_NAMES_COMPACT = frozenset(name.replace(' ', '') for name in SYNC_NAMES)
SYNC_PREFIXES = ('onedrive', 'dropbox')  # 'OneDrive - 회사', 'Dropbox (팀)'
SYNC_ENV = ('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial', 'OneDriveCommercial2')
# OneDrive Files On-Demand markers (Windows only).
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
RECALL_ATTRIBUTES = FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

FIX_HINT = f'set {HOME_ENV} to an absolute local folder outside the repository and outside sync folders'


class DataPathError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message


def _probe(path: Path, test) -> bool:
    """``test(path)``; a path that cannot be stat'ed (``PermissionError``, any ``OSError``) counts as absent.

    Same stance as ``virtualization_probe``. Python 3.12 ``Path.exists``/``is_file`` re-raise EACCES:
    ``sudo -u workmcp`` run from ``/root`` crashed on ``/root/.git`` (VPS 2026-10-30).
    """
    try:
        return test(path)
    except OSError:
        return False


def find_repo_root(package_file: str | os.PathLike = __file__, cwd: Path | None = None) -> Path | None:
    """First ancestor of the package with ``pyproject.toml`` (dev install), else a ``.git`` ancestor of cwd.

    An ancestor that cannot be stat'ed is not a repository and the walk goes on; a cwd that is gone
    or unreadable has no ``.git`` chain. A non-editable install (site-packages) therefore sees the
    repository only when the command runs inside the clone.
    """
    for parent in Path(package_file).resolve().parents:
        if _probe(parent/'pyproject.toml', Path.is_file):
            return parent
    try:
        start = (cwd or Path.cwd()).resolve()
    except OSError:  # cwd deleted (FileNotFoundError) or not readable
        return None
    for parent in [start, *start.parents]:
        if _probe(parent/'.git', Path.exists):
            return parent
    return None


def _is_unc(text: str) -> bool:
    return text.startswith(('\\\\', '//')) or PurePath(text).drive.startswith(('\\\\', '//'))


def _strip_long_prefix(text: str) -> str:
    """``\\\\?\\C:\\x`` -> ``C:\\x``. ``\\\\?\\UNC\\...`` and other ``\\\\?\\`` forms are kept (judged as UNC)."""
    if text[:4] in ('\\\\?\\', '//?/') and len(text) >= 7 and text[4].isalpha() and text[5] == ':' and text[6] in '\\/':
        return text[4:]
    return text


def _trailing_dot_space(path: PurePath) -> str | None:
    """First folder name ending in a dot or space (Windows drops them, so the name lies)."""
    for part in path.parts[1:]:
        if part not in ('.', '..') and part != part.rstrip(' .'):
            return part
    return None


def _key(path: PurePath) -> tuple[str, ...]:
    return tuple(os.path.normcase(part) for part in path.parts)


def _within(child: PurePath, parent: PurePath) -> bool:
    """True when ``child`` equals ``parent`` or lies under it (case-insensitive on Windows)."""
    c, p = _key(child), _key(parent)
    return len(c) >= len(p) and c[:len(p)] == p


def _file_attributes(path: Path) -> int | None:
    """Windows file attributes, or None where the platform has none (separate for tests)."""
    return getattr(os.stat(path), 'st_file_attributes', None)


def _sync_component(path: Path) -> str | None:
    for part in path.parts:
        name = unicodedata.normalize('NFC', part).casefold().strip()
        if name in SYNC_NAMES or name.replace(' ', '') in SYNC_NAMES_COMPACT or name.startswith(SYNC_PREFIXES):
            return part
    return None


def validate_home(raw: str | None, env: Mapping[str, str], repo_root: Path | None) -> Path:
    """Resolved data home, or ``DataPathError`` naming the first rule it breaks (design §2.2)."""
    if raw is None or raw == '':
        base = env.get(DEFAULT_BASE_ENV)
        if not base:
            raise DataPathError('home_unresolvable',
                                f'{HOME_ENV} is not set and {DEFAULT_BASE_ENV} is missing; {FIX_HINT}')
        raw = str(Path(base)/APP_DIR)
    local = _strip_long_prefix(raw)
    if _is_unc(local):
        raise DataPathError('home_unc', f'network path not allowed: {raw}; {FIX_HINT}')
    if not Path(local).is_absolute():
        raise DataPathError('home_relative', f'relative path not allowed: {raw}; {FIX_HINT}')
    if (part := _trailing_dot_space(Path(local))) is not None:
        raise DataPathError('home_trailing_dot_space', f'folder name ends in a dot or space ({part!r}): {raw}; {FIX_HINT}')
    # Every rule below is judged on the resolved target, so links and junctions cannot hide it.
    home = Path(_strip_long_prefix(str(Path(local).resolve())))
    if _is_unc(str(home)):
        raise DataPathError('home_unc', f'{raw} resolves to a network path {home}; {FIX_HINT}')
    if repo_root is not None:
        repo = Path(repo_root).resolve()
        if _within(home, repo) or _within(repo, home):
            raise DataPathError('home_inside_repo', f'{home} overlaps the repository {repo}; {FIX_HINT}')
    if (part := _sync_component(home)) is not None:
        raise DataPathError('home_sync_component', f'{home} is inside a sync folder ({part}); {FIX_HINT}')
    for name in SYNC_ENV:
        value = env.get(name)
        if value and Path(value).is_absolute() and _within(home, Path(value).resolve()):
            raise DataPathError('home_sync_env', f'{home} is inside %{name}% ({value}); {FIX_HINT}')
    for folder in [home, *home.parents]:
        if _probe(folder, Path.exists):
            try:
                attributes = _file_attributes(folder)
            except OSError:  # cannot be stat'ed: no attributes to judge (same stance as _probe)
                continue
            if attributes is not None and attributes & RECALL_ATTRIBUTES:
                raise DataPathError('home_sync_attr', f'{folder} carries cloud-sync attributes; {FIX_HINT}')
    return home


def resolve_home(env: Mapping[str, str] | None = None) -> Path:
    """Validated home for the running process (environment + repository of this package)."""
    env = os.environ if env is None else env
    return validate_home(env.get(HOME_ENV), env, find_repo_root())


def drive_warning(home: Path, env: Mapping[str, str]) -> str | None:
    """``doctor`` hint when the home is not on the system drive.

    A Google Drive streaming drive (``G:``) or a removable disk cannot be told apart by folder
    names, and asking Windows for the drive type needs a native call this package does not make,
    so this is a warning, not a refusal.
    """
    system = env.get('SystemDrive')
    if not home.drive or not system or os.path.normcase(home.drive) == os.path.normcase(system):
        return None
    return (f'warning: data home is on {home.drive}, not the system drive {system}; make sure it is a fixed '
            f'local disk (not a Google Drive streaming, network or removable drive)')


VIRTUALIZATION_KEYS = ('checked', 'under_appdata', 'redirected_to', 'package')


def _appdata_parts(home: Path, env: Mapping[str, str]) -> tuple[str, ...] | None:
    """``('Local' | 'Roaming' | ..., *rest)``: where ``home`` lies inside ``AppData``; None outside it."""
    home = Path(home)
    for name, folder in (('LOCALAPPDATA', 'Local'), ('APPDATA', 'Roaming')):
        if env.get(name) and Path(env[name]).is_absolute() and _within(home, Path(env[name])):
            return (folder, *home.parts[len(Path(env[name]).parts):])
    if env.get('USERPROFILE') and Path(env['USERPROFILE']).is_absolute():
        appdata = Path(env['USERPROFILE'])/'AppData'
        if _within(home, appdata):
            return home.parts[len(appdata.parts):]
    return None


def under_appdata(home: Path, env: Mapping[str, str]) -> bool:
    """``home`` lies in ``%LOCALAPPDATA%``, ``%APPDATA%`` or ``%USERPROFILE%\\AppData``: ``setup`` refuses it
    (M5 design §2.3 step 1), since a packaged app may redirect writes there (design M3 §10)."""
    return _appdata_parts(home, env) is not None


def _packages_dir(env: Mapping[str, str]) -> Path | None:
    if env.get('LOCALAPPDATA') and Path(env['LOCALAPPDATA']).is_absolute():
        return Path(env['LOCALAPPDATA'])/'Packages'
    if env.get('USERPROFILE') and Path(env['USERPROFILE']).is_absolute():
        return Path(env['USERPROFILE'])/'AppData'/'Local'/'Packages'
    return None


def _write_marker(path: Path) -> None:
    """Create the probe marker (separate so tests can stand in for the app's redirection)."""
    with open(path, 'x', encoding='ascii') as handle:
        handle.write('workmcp virtualization probe\n')


def virtualization_probe(home: Path, env: Mapping[str, str]) -> dict:
    """Does a packaged (MSIX) app redirect writes to ``home`` into its own folder (design M3 §10.3)?

    ``{'checked', 'under_appdata', 'redirected_to', 'package'}`` (``VIRTUALIZATION_KEYS``). A home
    outside ``AppData`` is never virtualised: ``checked`` True, nothing redirected. Under ``AppData``
    a marker file is written to ``home/tmp`` and looked up in every
    ``%LOCALAPPDATA%\\Packages\\<package>\\LocalCache\\<Local|Roaming>\\<same relative path>\\tmp``; the
    package folder that has it is where this process's writes really land. A home that already lies in
    ``Packages/<package>/LocalCache`` (what ``validate_home``'s resolve gives inside a packaged app) is
    reported as redirected into that package without a marker. The marker is always removed. A home not created yet (no ``tmp``) or a failing probe is ``checked`` False, never an error.
    Standard library only (no native calls).
    """
    home = Path(home)
    parts = _appdata_parts(home, env)
    if parts is None:
        return {'checked': True, 'under_appdata': False, 'redirected_to': None, 'package': None}
    result = {'checked': False, 'under_appdata': True, 'redirected_to': None, 'package': None}
    packages = _packages_dir(env)
    if packages is not None and _within(home, packages) and len(home.parts) > len(packages.parts) + 1 \
            and os.path.normcase(home.parts[len(packages.parts) + 1]) == os.path.normcase('LocalCache'):
        # The home already names a package folder: inside a packaged app, resolving %LOCALAPPDATA%\...
        # gives the redirected location itself (seen on the dev PC), so no marker is needed.
        return {**result, 'checked': True, 'redirected_to': str(home), 'package': home.parts[len(packages.parts)]}
    if packages is None or not (home/'tmp').is_dir():
        return result
    name = f'.virt-probe-{os.getpid()}-{os.urandom(4).hex()}'
    marker = home/'tmp'/name
    try:
        _write_marker(marker)
    except OSError:
        return result
    try:
        if packages.is_dir():
            with os.scandir(packages) as listing:
                names = sorted(entry.name for entry in listing if entry.is_dir(follow_symlinks=False))
        else:
            names = []  # no packaged apps for this user: nothing can redirect
        for package in names:
            redirected = packages/package/'LocalCache'/Path(*parts)
            if (redirected/'tmp'/name).is_file():
                result.update(redirected_to=str(redirected), package=package)
                break
        result['checked'] = True
    except OSError:
        pass  # the folder could not be listed: not checked, never an error (design M3 §15 #12)
    finally:
        try:
            marker.unlink()
        except OSError:
            pass
    return result


def virtualization_line(home: Path, probe: Mapping) -> str:
    """The ``doctor`` line (and the ``serve`` start line on stderr) for ``virtualization_probe``."""
    if not probe['under_appdata']:
        return 'app virtualization: none (home outside AppData)'
    if not probe['checked']:
        return 'app virtualization: not checked (home under AppData not created yet, or the probe failed)'
    if probe['redirected_to'] is None:
        return 'app virtualization: not redirected'
    return (f"warning: app virtualization: the data home {home} lands in package {probe['package']} "
            f"({probe['redirected_to']}); clients started outside this app see a different data home - set "
            f'{HOME_ENV} outside AppData (e.g. %USERPROFILE%\\Motive_work) in every client\'s MCP config')


def appdata_warning(home: Path, env: Mapping[str, str]) -> str | None:
    """``doctor`` warning when the home itself lies in an app package folder (removed with the app)."""
    parts = [os.path.normcase(part) for part in Path(home).parts]
    for at in range(len(parts) - 2):
        if parts[at] == os.path.normcase('Packages') and parts[at + 2] == os.path.normcase('LocalCache'):
            return (f'warning: data home {home} is inside the app package folder {Path(*Path(home).parts[:at + 2])} '
                    f'(removed with the app); set {HOME_ENV} outside AppData')
    return None


def ensure_home(home: Path) -> Path:
    """Create the sub-folders of an already validated home."""
    for name in SUBDIRS:
        (home/name).mkdir(parents=True, exist_ok=True)
    return home


def db_path(home: Path) -> Path:
    return home/'db'/'work.sqlite3'


def models_dir(home: Path) -> Path:
    return home/'models'


PUSH_TOKEN_ENV = 'WORKMCP_PUSH_TOKEN'
PUSH_TOKEN_FILE = 'push.token'


class TokenFileError(Exception):
    """The push token file is outside the data home, missing, unreadable or empty (push exit 4)."""


def read_token(home: Path, path: str | os.PathLike | None = None) -> str:
    """The collector's push token from ``path`` (default ``<home>/push.token``), stripped (design M5 remote
    §4.2, §6.4). The file must lie inside the data home (outside the repository and sync folders by the home
    rules), so ``push`` never reads any other file; the token itself is never printed or logged."""
    home = Path(home).resolve()
    file = Path(path) if path is not None else home/PUSH_TOKEN_FILE
    file = (file if file.is_absolute() else home/file).resolve()
    if not _within(file, home) or file == home:
        raise TokenFileError(f'the push token file must be inside the data home {home}')
    try:
        token = file.read_text(encoding='utf-8-sig').strip()
    except FileNotFoundError:
        raise TokenFileError(f'push token file not found: {file}') from None
    except (OSError, UnicodeDecodeError) as exc:
        raise TokenFileError(f'cannot read the push token file {file} ({type(exc).__name__})') from None
    if not token or any(ch.isspace() for ch in token):
        raise TokenFileError(f'the push token file {file} must hold one token on one line')
    return token

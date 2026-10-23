"""Single place that decides and checks where our data lives (design §2).

Data home = ``WORKMCP_HOME`` if set, else ``%LOCALAPPDATA%\\Motive_work``. The home must be
an absolute, local path outside the repository and outside any sync folder; otherwise
``validate_home`` raises ``DataPathError(code, message)`` and nothing is created. These
rules apply to our storage only, never to the source folders we read.
"""
from __future__ import annotations

import os
import unicodedata
from collections.abc import Mapping
from pathlib import Path, PurePath

HOME_ENV = 'WORKMCP_HOME'
APP_DIR = 'Motive_work'
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


def find_repo_root(package_file: str | os.PathLike = __file__, cwd: Path | None = None) -> Path | None:
    """First ancestor of the package with ``pyproject.toml`` (dev install), else a ``.git`` ancestor of cwd."""
    for parent in Path(package_file).resolve().parents:
        if (parent/'pyproject.toml').is_file():
            return parent
    for parent in [(cwd or Path.cwd()).resolve(), *(cwd or Path.cwd()).resolve().parents]:
        if (parent/'.git').exists():
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
        base = env.get('LOCALAPPDATA')
        if not base:
            raise DataPathError('home_unresolvable', f'{HOME_ENV} is not set and LOCALAPPDATA is missing; {FIX_HINT}')
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
        if folder.exists():
            attributes = _file_attributes(folder)
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


def ensure_home(home: Path) -> Path:
    """Create the sub-folders of an already validated home."""
    for name in SUBDIRS:
        (home/name).mkdir(parents=True, exist_ok=True)
    return home


def db_path(home: Path) -> Path:
    return home/'db'/'work.sqlite3'


def models_dir(home: Path) -> Path:
    return home/'models'

"""Source settings: ``<home>/config.toml`` and the source-root rules (design M1 §2).

The file is the reference; the ``source_roots`` table mirrors it (history and ``documents.root_id``,
rows are never deleted). This module only reads the file: there is no TOML writer and no
``sources add`` command. Secrets never go here (``.env`` only).

Source roots follow rules that differ from the data-home rules: the user's material may be
anywhere local, but a root must be an existing absolute folder, not on the network unless that
root opts in (``allow_network = true``), and must not overlap the data home, the repository or
another root. A root inside a sync folder is allowed with a warning.

M2 adds ``[index]`` (``IndexSettings``) and ``[search]`` (``SearchSettings``), design M2 §1.3. The
index model is a manifest name or ``"none"``; the test-only hash embedder cannot be chosen here.
M3 adds ``[server]`` (``ServerSettings``), design M3 §1.
"""
from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import models, paths, store
from .parsers import Limits

CONFIG_NAME = 'config.toml'
SCAN_KEYS = frozenset({'max_file_mb', 'max_units', 'max_text_chars', 'exclude', 'commit_every',
                       'read_cloud_placeholders'})
SOURCE_KEYS = frozenset({'path', 'exclude', 'allow_network'})
INDEX_KEYS = frozenset({'model', 'embed_batch', 'commit_every', 'threads'})
SEARCH_KEYS = frozenset({'lexical_candidates', 'semantic_candidates', 'max_vectors_in_memory', 'threads',
                         'model_idle_seconds'})
SERVER_KEYS = frozenset({'response_max_chars', 'unload_tick_seconds'})
RESPONSE_MAX_CHARS_RANGE = (4_000, 40_000)    # design M3 §4 (40,000 = tools.RESPONSE_MAX_CHARS)
UNLOAD_TICK_RANGE = (1, 300)                  # design M3 §6.2
NO_MODEL = 'none'  # [index] model = "none": chunks and FTS only (lexical search)
ROOT_CODES = ('source_root_relative', 'source_root_trailing_dot_space', 'source_root_missing', 'source_root_network',
              'source_root_inside_home', 'source_root_contains_home', 'source_root_inside_repo',
              'source_roots_overlap')


class ConfigError(Exception):
    """``config.toml`` cannot be read or has an unknown key or a wrong type (scan exit code 4)."""


class SourceRootError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ScanSettings:
    limits: Limits = Limits()
    exclude: tuple[str, ...] = ()               # file-name patterns added to the defaults
    commit_every: int = 200
    read_cloud_placeholders: bool = False


@dataclass(frozen=True)
class IndexSettings:
    """``[index]`` (design M2 §1.3). ``model`` is a manifest name or ``'none'``."""
    model: str = models.DEFAULT_MODEL
    embed_batch: int = 8                        # fp32 batch; an int8 model runs one text per batch
    commit_every: int = 64                      # vector rows per commit
    threads: int = 0                            # 0: the ONNX Runtime default (index CLI)


@dataclass(frozen=True)
class SearchSettings:
    """``[search]`` (design M2 §1.3). ``like_max_chunks`` is not a config key (tests inject it)."""
    lexical_candidates: int = 200
    semantic_candidates: int = 200
    max_vectors_in_memory: int = 250_000        # over this the vectors are streamed per query
    threads: int = 0                            # 0: max(1, cpu_count // 2), two server processes
    model_idle_seconds: int = 600               # release the model after this long unused
    like_max_chunks: int = 200_000              # above this the short-query LIKE path is not used

    def effective_threads(self, cpu_count: int | None = None) -> int:
        """``intra_op_num_threads`` for the server (design M2 §6.2 M3)."""
        if self.threads:
            return self.threads
        count = os.cpu_count() if cpu_count is None else cpu_count
        return max(1, (count or 2) // 2)


@dataclass(frozen=True)
class ServerSettings:
    """``[server]`` (design M3 §1): the response budget in characters of the serialized envelope and
    the idle-release tick of the long-lived server."""
    response_max_chars: int = 40_000
    unload_tick_seconds: int = 30


@dataclass(frozen=True)
class SourceEntry:
    """One ``[[sources]]`` table as written (or one ``scan --root``)."""
    raw: str
    exclude: tuple[str, ...] = ()               # root-relative patterns, '/' separated
    allow_network: bool = False


@dataclass(frozen=True)
class Config:
    file: Path
    exists: bool
    scan: ScanSettings
    sources: tuple[SourceEntry, ...]
    index: IndexSettings = IndexSettings()
    search: SearchSettings = SearchSettings()
    server: ServerSettings = ServerSettings()


@dataclass(frozen=True)
class RootSpec:
    """A validated source root."""
    path: Path                                  # resolved
    raw: str
    exclude: tuple[str, ...] = ()
    allow_network: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return os.path.normcase(str(self.path))


@dataclass(frozen=True)
class RootCheck:
    """Outcome of validating one entry: ``spec`` or ``error``."""
    entry: SourceEntry
    spec: RootSpec | None
    error: SourceRootError | None


def config_file(home: Path) -> Path:
    return Path(home)/CONFIG_NAME


def _strings(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f'{where} must be a list of non-empty strings')
    return tuple(value)


def _positive_int(value, where: str) -> int:
    if type(value) is not int or value < 1:
        raise ConfigError(f'{where} must be a positive integer')
    return value


def _count(value, where: str) -> int:
    if type(value) is not int or value < 0:
        raise ConfigError(f'{where} must be an integer >= 0')
    return value


def _table(data: dict, name: str, keys: frozenset, file: Path) -> dict:
    table = data.get(name, {})
    if not isinstance(table, dict):
        raise ConfigError(f'{file}: [{name}] must be a table')
    if unknown := set(table) - keys:
        raise ConfigError(f'{file}: unknown [{name}] key {sorted(unknown)}')
    return table


def _index_settings(table: dict, file: Path) -> IndexSettings:
    defaults = IndexSettings()
    model = table.get('model', defaults.model)
    if model != NO_MODEL and model not in models.MANIFEST:
        raise ConfigError(f'{file}: [index] model must be one of {sorted(models.MANIFEST)} or "{NO_MODEL}"')
    return IndexSettings(
        model=model,
        embed_batch=_positive_int(table.get('embed_batch', defaults.embed_batch), '[index] embed_batch'),
        commit_every=_positive_int(table.get('commit_every', defaults.commit_every), '[index] commit_every'),
        threads=_count(table.get('threads', defaults.threads), '[index] threads'))


def _search_settings(table: dict) -> SearchSettings:
    d = SearchSettings()
    return SearchSettings(
        lexical_candidates=_positive_int(table.get('lexical_candidates', d.lexical_candidates),
                                         '[search] lexical_candidates'),
        semantic_candidates=_positive_int(table.get('semantic_candidates', d.semantic_candidates),
                                          '[search] semantic_candidates'),
        max_vectors_in_memory=_positive_int(table.get('max_vectors_in_memory', d.max_vectors_in_memory),
                                            '[search] max_vectors_in_memory'),
        threads=_count(table.get('threads', d.threads), '[search] threads'),
        model_idle_seconds=_positive_int(table.get('model_idle_seconds', d.model_idle_seconds),
                                         '[search] model_idle_seconds'))


def _in_range(value, where: str, bounds: tuple[int, int]) -> int:
    low, high = bounds
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f'{where} must be an integer {low}-{high}')
    return value


def _server_settings(table: dict) -> ServerSettings:
    d = ServerSettings()
    return ServerSettings(
        response_max_chars=_in_range(table.get('response_max_chars', d.response_max_chars),
                                     '[server] response_max_chars', RESPONSE_MAX_CHARS_RANGE),
        unload_tick_seconds=_in_range(table.get('unload_tick_seconds', d.unload_tick_seconds),
                                      '[server] unload_tick_seconds', UNLOAD_TICK_RANGE))


def parse_config(text: str, file: Path) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f'{file} is not valid TOML ({exc})') from None
    if unknown := set(data) - {'scan', 'sources', 'index', 'search', 'server'}:
        raise ConfigError(f'{file}: unknown table or key {sorted(unknown)}')
    scan = data.get('scan', {})
    if not isinstance(scan, dict):
        raise ConfigError(f'{file}: [scan] must be a table')
    if unknown := set(scan) - SCAN_KEYS:
        raise ConfigError(f'{file}: unknown [scan] key {sorted(unknown)}')
    defaults = Limits()
    limits = Limits(
        max_file_bytes=_positive_int(scan['max_file_mb'], '[scan] max_file_mb') * 2**20
        if 'max_file_mb' in scan else defaults.max_file_bytes,
        max_units=_positive_int(scan.get('max_units', defaults.max_units), '[scan] max_units'),
        max_text_chars=_positive_int(scan.get('max_text_chars', defaults.max_text_chars), '[scan] max_text_chars'))
    read_placeholders = scan.get('read_cloud_placeholders', False)
    if not isinstance(read_placeholders, bool):
        raise ConfigError(f'{file}: [scan] read_cloud_placeholders must be true or false')
    settings = ScanSettings(limits=limits, exclude=_strings(scan.get('exclude', []), '[scan] exclude'),
                            commit_every=_positive_int(scan.get('commit_every', 200), '[scan] commit_every'),
                            read_cloud_placeholders=read_placeholders)
    sources = data.get('sources', [])
    if not isinstance(sources, list) or not all(isinstance(item, dict) for item in sources):
        raise ConfigError(f'{file}: sources must be written as [[sources]] tables')
    entries = []
    for number, item in enumerate(sources, 1):
        where = f'[[sources]] #{number}'
        if unknown := set(item) - SOURCE_KEYS:
            raise ConfigError(f'{file}: unknown {where} key {sorted(unknown)}')
        if not isinstance(item.get('path'), str) or not item['path']:
            raise ConfigError(f'{file}: {where} needs path = \'<absolute folder>\'')
        allow = item.get('allow_network', False)
        if not isinstance(allow, bool):
            raise ConfigError(f'{file}: {where} allow_network must be true or false')
        entries.append(SourceEntry(item['path'], _strings(item.get('exclude', []), f'{where} exclude'), allow))
    return Config(file, True, settings, tuple(entries), _index_settings(_table(data, 'index', INDEX_KEYS, file), file),
                  _search_settings(_table(data, 'search', SEARCH_KEYS, file)),
                  _server_settings(_table(data, 'server', SERVER_KEYS, file)))


def load(home: Path) -> Config:
    """Settings of ``<home>/config.toml``; defaults and no sources when the file does not exist."""
    file = config_file(home)
    try:
        text = file.read_text(encoding='utf-8-sig')  # Notepad may write a BOM
    except FileNotFoundError:
        return Config(file, False, ScanSettings(), ())
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f'cannot read {file} ({type(exc).__name__})') from None
    return parse_config(text, file)


def _is_dir(path: Path) -> bool:
    """Separate so tests can stand in for a network folder without touching the network."""
    return path.is_dir()


def _resolve(path: Path) -> Path:
    return path.resolve()


def _sync_warning(resolved: Path, env: Mapping[str, str]) -> str | None:
    part = paths._sync_component(resolved)
    if part is None:
        for name in paths.SYNC_ENV:
            value = env.get(name)
            if value and Path(value).is_absolute() and paths._within(resolved, Path(value)):
                part = f'%{name}%'
                break
    if part is None:
        return None
    return (f'warning: source root {resolved} is inside a sync folder ({part}); the originals follow the '
            f'company sync policy, our store stays local')


def validate_root(raw: str, home: Path, repo_root: Path | None, allow_network: bool = False,
                  env: Mapping[str, str] | None = None) -> tuple[Path, tuple[str, ...]]:
    """``(resolved root, warnings)``, or ``SourceRootError`` naming the first rule broken (design §2.3).

    A network path written as such (``\\\\server\\share``) is refused before the folder is
    touched, so a refused root never causes a request to a file server.
    """
    env = os.environ if env is None else env
    local = paths._strip_long_prefix(str(raw))
    if not Path(local).is_absolute():
        raise SourceRootError('source_root_relative', f'source root must be an absolute folder: {raw}')
    if (part := paths._trailing_dot_space(Path(local))) is not None:
        raise SourceRootError('source_root_trailing_dot_space', f'folder name ends in a dot or space ({part!r}): {raw}')
    network_hint = 'set allow_network = true for this [[sources]] entry to read it over the network'
    if paths._is_unc(local) and not allow_network:
        raise SourceRootError('source_root_network', f'network path: {raw}; {network_hint}')
    if not _is_dir(Path(local)):
        raise SourceRootError('source_root_missing', f'not an existing folder: {raw}')
    resolved = Path(paths._strip_long_prefix(str(_resolve(Path(local)))))
    warnings = []
    if paths._is_unc(local) or paths._is_unc(str(resolved)):
        if not allow_network:
            raise SourceRootError('source_root_network', f'{raw} resolves to a network path {resolved}; {network_hint}')
        warnings.append(f'warning: source root {resolved} is on the network (allow_network = true): file names and '
                        f'contents are read through the file server')
    home = Path(home)
    if paths._within(resolved, home):
        raise SourceRootError('source_root_inside_home', f'{resolved} is inside the data home {home}')
    if paths._within(home, resolved):
        raise SourceRootError('source_root_contains_home', f'{resolved} contains the data home {home}')
    if repo_root is not None:
        repo = Path(repo_root).resolve()
        if paths._within(resolved, repo) or paths._within(repo, resolved):
            raise SourceRootError('source_root_inside_repo', f'{resolved} overlaps the repository {repo}')
    if warning := _sync_warning(resolved, env):
        warnings.append(warning)
    return resolved, tuple(warnings)


def check_roots(entries, home: Path, repo_root: Path | None, env: Mapping[str, str] | None = None) -> list[RootCheck]:
    """Validate every entry; a root that overlaps an earlier valid one gets ``source_roots_overlap``."""
    checks, valid = [], []
    for entry in entries:
        try:
            path, warnings = validate_root(entry.raw, home, repo_root, entry.allow_network, env)
        except SourceRootError as exc:
            checks.append(RootCheck(entry, None, exc))
            continue
        other = next((spec for spec in valid if paths._within(path, spec.path) or paths._within(spec.path, path)), None)
        if other is not None:
            checks.append(RootCheck(entry, None, SourceRootError(
                'source_roots_overlap', f'{path} overlaps the source root {other.path}')))
            continue
        spec = RootSpec(path, entry.raw, entry.exclude, entry.allow_network, warnings)
        valid.append(spec)
        checks.append(RootCheck(entry, spec, None))
    return checks


def sync_roots(db, specs, *, from_config: bool) -> dict[str, int]:
    """Mirror roots into ``source_roots`` (design §2.2); returns ``{RootSpec.key: root id}``.

    Config roots are enabled and every other row is disabled; a ``scan --root`` root is added
    if new and leaves ``enabled`` as it is. Rows are never deleted.
    """
    ids = {}
    db.execute('BEGIN IMMEDIATE')
    try:
        for spec in specs:
            row = db.execute('SELECT id FROM source_roots WHERE path=?', (spec.key,)).fetchone()
            if row is None:
                ids[spec.key] = db.execute('INSERT INTO source_roots(path, enabled, added_at) VALUES(?, 1, ?)',
                                           (spec.key, store.now())).lastrowid
            else:
                ids[spec.key] = row[0]
                if from_config:
                    db.execute('UPDATE source_roots SET enabled=1 WHERE id=?', (row[0],))
        if from_config:
            keys = sorted(ids)
            marks = ','.join('?' * len(keys))
            db.execute(f'UPDATE source_roots SET enabled=0 WHERE path NOT IN ({marks})', keys)
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    return ids

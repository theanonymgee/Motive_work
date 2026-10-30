"""Source settings and root rules (design M1 §2.1-§2.3). Temporary folders only; no network path is touched."""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from workmcp import config, paths, store

UNC = r'\\localhost\C$\synthetic' if os.name == 'nt' else '//localhost/synthetic'
WINDOWS_ONLY = pytest.mark.skipif(sys.platform != 'win32', reason='Windows path form (no UNC long prefix elsewhere)')


@pytest.fixture
def home(tmp_path):
    return paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))


def code_of(raw, home, repo=None, allow_network=False, env=None):
    with pytest.raises(config.SourceRootError) as info:
        config.validate_root(raw, home, repo, allow_network, env or {})
    return info.value.code


def test_valid_root_is_resolved_without_warnings(tmp_path, home):
    (tmp_path/'docs').mkdir()
    path, warnings = config.validate_root(str(tmp_path/'docs'), home, None, env={})
    assert path == (tmp_path/'docs').resolve() and warnings == ()


@pytest.mark.parametrize('case, code', [
    ('relative', 'source_root_relative'),
    ('drive_relative', 'source_root_relative'),
    ('trailing_dot', 'source_root_trailing_dot_space'),
    ('trailing_space', 'source_root_trailing_dot_space'),
    ('missing', 'source_root_missing'),
    ('file', 'source_root_missing'),
    ('unc', 'source_root_network'),
    pytest.param('unc_long', 'source_root_network', marks=WINDOWS_ONLY),
    ('inside_home', 'source_root_inside_home'),
    ('is_home', 'source_root_inside_home'),
    ('contains_home', 'source_root_contains_home'),
    ('inside_repo', 'source_root_inside_repo'),
    ('contains_repo', 'source_root_inside_repo'),
])
def test_root_validation_codes(tmp_path, home, case, code):
    repo = tmp_path/'repo'
    (repo/'docs').mkdir(parents=True)
    (tmp_path/'a-file.txt').write_bytes(b'synthetic')
    raw = {
        'relative': 'relative\\docs',
        'drive_relative': 'C:docs' if os.name == 'nt' else 'docs',
        'trailing_dot': str(tmp_path/'docs.'/'x'),
        'trailing_space': str(tmp_path/'docs '/'x'),
        'missing': str(tmp_path/'nope'),
        'file': str(tmp_path/'a-file.txt'),
        'unc': UNC,
        'unc_long': '\\\\?\\UNC\\localhost\\C$\\synthetic',
        'inside_home': str(home/'db'),
        'is_home': str(home),
        'contains_home': str(tmp_path),
        'inside_repo': str(repo/'docs'),
        'contains_repo': str(tmp_path/'outer'),
    }[case]
    if case == 'contains_repo':
        repo = tmp_path/'outer'/'repo'
        repo.mkdir(parents=True)
    assert code_of(raw, home, repo) == code


def test_long_prefix_local_path_is_accepted(tmp_path, home):
    if os.name != 'nt':
        pytest.skip('Windows path form')
    (tmp_path/'docs').mkdir()
    path, _ = config.validate_root('\\\\?\\' + str(tmp_path/'docs'), home, None, env={})
    assert path == (tmp_path/'docs').resolve()


def test_network_root_is_refused_before_it_is_touched(tmp_path, home, monkeypatch):
    touched = []
    monkeypatch.setattr(config, '_is_dir', lambda path: touched.append(path) or True)
    monkeypatch.setattr(config, '_resolve', lambda path: touched.append(path) or path)
    assert code_of(UNC, home) == 'source_root_network'
    assert touched == []  # no request to a file server for a refused root


def test_network_root_opt_in_passes_with_a_warning(tmp_path, home, monkeypatch):
    monkeypatch.setattr(config, '_is_dir', lambda path: True)
    monkeypatch.setattr(config, '_resolve', lambda path: path)  # stands in: no SMB request in tests
    path, warnings = config.validate_root(UNC, home, None, allow_network=True, env={})
    assert paths._is_unc(str(path))
    assert len(warnings) == 1 and 'network' in warnings[0]


def test_mapped_drive_resolving_to_unc_is_network(tmp_path, home, monkeypatch):
    """A mapped drive letter that resolves to a share (design §2.3 #4) is judged on the resolved path."""
    (tmp_path/'mapped').mkdir()
    monkeypatch.setattr(config, '_resolve', lambda path: Path(UNC))
    assert code_of(str(tmp_path/'mapped'), home) == 'source_root_network'
    path, warnings = config.validate_root(str(tmp_path/'mapped'), home, None, allow_network=True, env={})
    assert paths._is_unc(str(path)) and warnings


@pytest.mark.parametrize('folder', ['OneDrive - 가상회사', 'Dropbox', 'My Drive'])
def test_root_inside_a_sync_folder_is_allowed_with_a_warning(tmp_path, home, folder):
    (tmp_path/folder/'docs').mkdir(parents=True)
    path, warnings = config.validate_root(str(tmp_path/folder/'docs'), home, None, env={})
    assert len(warnings) == 1 and 'sync folder' in warnings[0]


def test_root_inside_onedrive_env_is_allowed_with_a_warning(tmp_path, home):
    (tmp_path/'cloud'/'docs').mkdir(parents=True)
    _, warnings = config.validate_root(str(tmp_path/'cloud'/'docs'), home, None, env={'OneDrive': str(tmp_path/'cloud')})
    assert len(warnings) == 1 and '%OneDrive%' in warnings[0]


def test_overlapping_roots_are_refused(tmp_path, home):
    for name in ('a/b', 'c'):
        (tmp_path/name).mkdir(parents=True)
    entries = [config.SourceEntry(str(tmp_path/'a')), config.SourceEntry(str(tmp_path/'a'/'b')),
               config.SourceEntry(str(tmp_path/'c')), config.SourceEntry(str(tmp_path/'A'))]
    checks = config.check_roots(entries, home, None, env={})
    assert [c.error.code if c.error else 'ok' for c in checks] == [
        'ok', 'source_roots_overlap', 'ok', 'source_roots_overlap' if os.name == 'nt' else 'source_root_missing']


def test_a_root_that_cannot_be_checked_is_refused_not_a_traceback(tmp_path, home, monkeypatch, capsys):
    """VPS 2026-10-30: Python 3.12 ``is_dir`` re-raises EACCES; the root is refused (fail closed)."""
    from workmcp import cli
    (tmp_path/'open').mkdir()
    denied = tmp_path/'denied'
    real = config._is_dir

    def is_dir(path):
        if Path(path) == denied:
            raise PermissionError(13, 'Permission denied (synthetic)', str(path))
        return real(path)
    monkeypatch.setattr(config, '_is_dir', is_dir)
    checks = config.check_roots([config.SourceEntry(str(denied)), config.SourceEntry(str(tmp_path/'open'))],
                                home, None, env={})
    assert [c.error.code if c.error else 'ok' for c in checks] == ['source_root_missing', 'ok']
    assert 'PermissionError' in checks[0].error.message
    write_config(home, f"[[sources]]\npath = '{denied}'\n")
    lines = cli._root_lines(home)
    assert lines == [f'source root {denied}: source_root_missing: cannot check the folder (PermissionError): {denied}']
    assert capsys.readouterr() == ('', '')


def test_every_root_code_is_covered():
    assert set(config.ROOT_CODES) == {
        'source_root_relative', 'source_root_trailing_dot_space', 'source_root_missing', 'source_root_network',
        'source_root_inside_home', 'source_root_contains_home', 'source_root_inside_repo', 'source_roots_overlap'}


# config.toml
def write_config(home, text):
    config.config_file(home).write_text(text, encoding='utf-8')


def test_missing_config_gives_defaults(home):
    cfg = config.load(home)
    assert not cfg.exists and cfg.sources == () and cfg.scan == config.ScanSettings()
    assert cfg.scan.commit_every == 200 and cfg.scan.read_cloud_placeholders is False


def test_config_values_are_read(home):
    write_config(home, "[scan]\nmax_file_mb = 2\nmax_units = 10\nmax_text_chars = 99\nexclude = ['*.bak']\n"
                       "commit_every = 5\nread_cloud_placeholders = true\n\n"
                       "[[sources]]\npath = 'D:\\synthetic\\docs'\nexclude = ['drafts/*']\n\n"
                       "[[sources]]\npath = '\\\\fileserver.invalid\\share'\nallow_network = true\n")
    cfg = config.load(home)
    assert cfg.exists
    assert cfg.scan.limits.max_file_bytes == 2 * 2**20 and cfg.scan.limits.max_units == 10
    assert cfg.scan.limits.max_text_chars == 99 and cfg.scan.limits.csv_max_columns == 1_000
    assert cfg.scan.exclude == ('*.bak',) and cfg.scan.commit_every == 5 and cfg.scan.read_cloud_placeholders
    assert cfg.sources == (config.SourceEntry('D:\\synthetic\\docs', ('drafts/*',), False),
                           config.SourceEntry('\\\\fileserver.invalid\\share', (), True))


def test_config_with_a_bom_is_read(home):
    config.config_file(home).write_bytes(b'\xef\xbb\xbf[scan]\ncommit_every = 3\n')
    assert config.load(home).scan.commit_every == 3


@pytest.mark.parametrize('text', [
    '[scan\n', 'unknown = 1\n', '[scan]\nmax_file_gb = 1\n', '[scan]\ncommit_every = 0\n',
    '[scan]\ncommit_every = "200"\n', '[scan]\nexclude = "*.bak"\n', '[scan]\nread_cloud_placeholders = 1\n',
    '[[sources]]\nexclude = []\n', '[[sources]]\npath = 3\n', "[[sources]]\npath = 'D:\\\\x'\nallow_network = 'yes'\n",
    "[[sources]]\npath = 'D:\\\\x'\nrecursive = true\n", "sources = 'D:\\\\x'\n"])
def test_bad_config_is_refused(home, text):
    write_config(home, text)
    with pytest.raises(config.ConfigError):
        config.load(home)


# [index] and [search] (design M2 §1.3)
def test_index_and_search_settings_default_and_are_read(home):
    cfg = config.load(home)
    assert cfg.index == config.IndexSettings() and cfg.search == config.SearchSettings()
    assert (cfg.index.model, cfg.index.embed_batch, cfg.index.commit_every, cfg.index.threads) == ('bge-m3', 8, 64, 0)
    assert (cfg.search.lexical_candidates, cfg.search.semantic_candidates, cfg.search.max_vectors_in_memory,
            cfg.search.threads, cfg.search.model_idle_seconds) == (200, 200, 250_000, 0, 600)
    write_config(home, '[index]\nmodel = "none"\nembed_batch = 4\ncommit_every = 16\nthreads = 2\n\n'
                       '[search]\nlexical_candidates = 50\nsemantic_candidates = 60\nmax_vectors_in_memory = 10\n'
                       'threads = 3\nmodel_idle_seconds = 120\n')
    cfg = config.load(home)
    assert cfg.index == config.IndexSettings('none', 4, 16, 2)
    assert cfg.search == config.SearchSettings(50, 60, 10, 3, 120)


def test_server_threads_default_is_half_the_cpus():
    assert config.SearchSettings().effective_threads(16) == 8
    assert config.SearchSettings().effective_threads(1) == 1
    assert config.SearchSettings(threads=3).effective_threads(16) == 3
    assert config.SearchSettings().effective_threads() >= 1


@pytest.mark.parametrize('text', [
    '[index]\nmodel = "hash-ngram-test-only"\n', '[index]\nmodel = "multilingual-e5-base-int8"\n',
    '[index]\nembed_batch = 0\n', '[index]\nthreads = -1\n', '[index]\nbatch = 8\n', 'index = 1\n',
    '[search]\nmax_vectors_in_memory = 0\n', '[search]\nthreads = "4"\n', '[search]\nlike_max_chunks = 5\n',
    '[search]\nmodel_idle_seconds = 0\n'])
def test_bad_index_or_search_config_is_refused(home, text):
    write_config(home, text)
    with pytest.raises(config.ConfigError):
        config.load(home)


# source_roots mirror (design §2.2)
def spec(path):
    return config.RootSpec(Path(path), str(path))


def roots_table(db):
    return {path: enabled for path, enabled in db.execute('SELECT path, enabled FROM source_roots')}


def test_source_roots_mirror_follows_the_config(tmp_path, home):
    a, b, c = (spec(tmp_path/name) for name in 'abc')
    db = store.open_home(home)
    try:
        ids = config.sync_roots(db, [a, b], from_config=True)
        assert set(ids) == {a.key, b.key}
        assert roots_table(db) == {a.key: 1, b.key: 1}
        assert config.sync_roots(db, [a], from_config=True) == {a.key: ids[a.key]}
        assert roots_table(db) == {a.key: 1, b.key: 0}          # left the config: disabled, never deleted
        config.sync_roots(db, [c], from_config=False)           # scan --root: added, others untouched
        assert roots_table(db) == {a.key: 1, b.key: 0, c.key: 1}
        config.sync_roots(db, [b], from_config=False)
        assert roots_table(db)[b.key] == 0                      # --root does not change enabled
        assert config.sync_roots(db, [b, a], from_config=True) == {a.key: ids[a.key], b.key: ids[b.key]}
        assert roots_table(db) == {a.key: 1, b.key: 1, c.key: 0}
    finally:
        db.close()


def test_mirror_key_is_case_insensitive_on_windows(tmp_path, home):
    if os.name != 'nt':
        pytest.skip('Windows paths')
    db = store.open_home(home)
    try:
        first = config.sync_roots(db, [spec(str(tmp_path/'Docs'))], from_config=False)
        second = config.sync_roots(db, [spec(str(tmp_path/'DOCS'))], from_config=False)
        assert list(first.values()) == list(second.values())
        assert db.execute('SELECT COUNT(*) FROM source_roots').fetchone()[0] == 1
    finally:
        db.close()


def test_mirror_rolls_back_on_error(tmp_path, home, monkeypatch):
    db = store.open_home(home)
    try:
        bad = config.RootSpec(Path(tmp_path/'a'), 'a')
        monkeypatch.setattr(store, 'now', lambda: None)  # added_at NOT NULL fails inside the transaction
        with pytest.raises(sqlite3.IntegrityError):
            config.sync_roots(db, [bad], from_config=True)
        assert not db.in_transaction and roots_table(db) == {}
    finally:
        db.close()

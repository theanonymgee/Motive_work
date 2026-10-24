"""``workmcp index``/``search``/``fetch-model``/``verify-model`` wiring and ``doctor``'s index line
(design M2 §1.2). Stage 0: exit codes decided before the stage-1 bodies run. In-process, temporary
data home (conftest), synthetic rows only.
"""
import pytest

from workmcp import chunks, cli, models, paths, store


@pytest.fixture
def home(workmcp_home):
    return paths.ensure_home(paths.resolve_home())


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_index_lock_refuses_second_run(home, capsys):
    lock, _ = store.acquire_lock(home, store.INDEX_LOCK_NAME)
    try:
        code, out, err = run(capsys, 'index', '--model', 'none')
        assert (code, out) == (3, '') and 'another index is running' in err
    finally:
        store.release_lock(lock)
    assert not store.lock_path(home, store.INDEX_LOCK_NAME).exists()


@pytest.mark.parametrize('argv', [('index', '--model', 'hash'), ('index', '--model', 'e5'), ('index', '--limit', '0'),
                                  ('fetch-model', 'nope', '--from', 'C:\\'), ('verify-model', 'nope')])
def test_bad_model_name_or_limit_exits_4_before_any_work(home, capsys, argv):
    code, out, err = run(capsys, *argv)
    assert (code, out) == (4, '') and err
    assert not store.lock_path(home, store.INDEX_LOCK_NAME).exists()
    assert not paths.db_path(home).exists()


def test_config_model_none_and_a_test_embedder_is_never_selectable(home, capsys):
    (home/'config.toml').write_text('[index]\nmodel = "hash-ngram-test-only"\n', encoding='utf-8')
    code, _, err = run(capsys, 'index')
    assert code == 4 and '[index] model' in err


def test_fetch_model_requires_from(home, capsys):
    code, out, err = run(capsys, 'fetch-model', 'bge-m3')
    assert (code, out) == (4, '') and '--from' in err and 'does not download' in err
    code, _, err = run(capsys, 'fetch-model', 'bge-m3', '--from', 'relative\\folder')
    assert code == 4 and 'absolute folder' in err


def test_verify_model_reports_every_file(home, capsys):
    code, out, _ = run(capsys, 'verify-model')
    names = [models.local_name(spec) for spec in models.entry('bge-m3')['files']]
    assert code == 1 and out.splitlines() == [f'bge-m3/{name}: missing' for name in names]


@pytest.mark.parametrize('argv', [('search', '회의록'), ('search', '회의록', '--mode', 'fuzzy')])
def test_search_without_store_or_with_a_bad_mode_exits_4(home, capsys, argv):
    code, out, err = run(capsys, *argv)
    assert (code, out) == (4, '') and err
    store.open_home(home).close()
    if '--mode' in argv:
        code, out, err = run(capsys, *argv)
        assert (code, out) == (4, '') and '--mode' in err


def test_doctor_reports_the_index_and_an_fts_gap(home, capsys, monkeypatch):
    db = store.open_home(home)
    try:
        code, out, _ = run(capsys, 'doctor')
        fts = 'fts rows 0 (never synced)' if store.fts5_available() else 'fts unavailable'
        assert code == 0 and f'index: chunks 0 (chunk_version none), vectors 0, {fts}' in out
        root = db.execute(r"INSERT INTO source_roots(path, added_at) VALUES('C:\synthetic', 'x')").lastrowid
        doc = db.execute(r"INSERT INTO documents(root_id, path, path_key, ext, status, first_seen_at, last_seen_at) "
                         r"VALUES(?, 'C:\synthetic\a.txt', 'c:\synthetic\a.txt', '.txt', 'active', 'x', 'x')",
                         (root,)).lastrowid
        version = db.execute("INSERT INTO versions(document_id, seq, sha256, size, mtime_ns, observed_at, parse_status, "
                             "unit_count) VALUES(?, 1, ?, 1, 1, 'x', 'ok', 1)", (doc, 'a' * 64)).lastrowid
        with monkeypatch.context() as patch:
            patch.setattr(store, 'fts5_available', lambda: False)
            store.insert_chunks(db, version, chunks.CHUNK_VERSION, [chunks.ChunkSpec(1, 1, 1, 0, 6, '합성자료 본문')])
    finally:
        db.close()
    code, out, _ = run(capsys, 'doctor')
    assert code == 0 and f'index: chunks 1 (chunk_version {chunks.CHUNK_VERSION}), vectors 0' in out
    assert ('run workmcp index --rebuild-fts' in out) is store.fts5_available()

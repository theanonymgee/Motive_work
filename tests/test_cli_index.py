"""``workmcp index``/``search``/``fetch-model``/``verify-model`` wiring and ``doctor``'s index line
(design M2 §1.2, §4.4): exit codes, the index summary and log, end to end over the synthetic bundle.
In-process, temporary data home (conftest), synthetic data only.
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
    from workmcp import tools
    notice = tools.status(home)['notice']
    assert any('--rebuild-fts' in line for line in notice) is store.fts5_available()


# -- index end to end (design M2 §4.4, §10) --------------------------------------------------

@pytest.fixture
def scanned(home, tmp_path, capsys):
    from fixtures.bundle import write_bundle
    root = tmp_path/'bundle'
    write_bundle(root)
    code, _, _ = run(capsys, 'scan', '--root', str(root))
    assert code == 0
    return root


def test_index_summary_and_log_carry_no_document_text(home, scanned, capsys):
    from fixtures import SENTINEL, SENTINEL_ASCII
    code, out, err = run(capsys, 'index', '--model', 'none')
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0].startswith('index #1 started') and 'model none' in lines[0] and chunks.CHUNK_VERSION in lines[0]
    assert 'chunked' in lines[1] and 'embedded 0' in lines[2] and 'vectors: skipped (model none)' in lines[3]
    log = next((home/'logs').glob('index-*.log')).read_text(encoding='utf-8')
    assert ' | chunked | ' in log and 'done | index #1' in log
    for marker in (SENTINEL, SENTINEL_ASCII):
        assert marker not in out + err and marker not in log
    db = store.open_read(home)
    try:
        assert db.execute('SELECT COUNT(*) FROM chunks WHERE instr(text, ?) > 0', (SENTINEL,)).fetchone()[0] > 0
    finally:
        db.close()


def test_index_twice_then_a_change_reindexes_only_that_document(home, scanned, capsys):
    code, out, _ = run(capsys, 'index', '--model', 'none')
    assert code == 0 and 'unchanged 0' in out
    code, out, _ = run(capsys, 'index', '--model', 'none')
    assert code == 0 and 'chunked 0 (reused 0' in out and 'dropped 0 versions' in out
    note = scanned/'docs'/'notes_utf8.txt'
    note.write_text(note.read_text(encoding='utf-8') + '\n바뀐 합성 줄', encoding='utf-8')
    assert run(capsys, 'scan', '--root', str(scanned))[0] == 0
    code, out, _ = run(capsys, 'index', '--model', 'none')
    assert code == 0 and 'chunked 1 (reused 0' in out and 'dropped 1 versions' in out
    code, out, _ = run(capsys, 'verify')
    assert code == 0 and 'binding ok' in out


def test_index_without_the_model_files_builds_chunks_with_a_note(home, scanned, capsys):
    code, out, err = run(capsys, 'index')  # default model bge-m3, not in this temporary home
    assert code == 0, err
    assert 'vectors: skipped (model_missing' in out


def test_doctor_and_status_report_the_index_after_a_run(home, scanned, capsys):
    from workmcp import tools
    assert run(capsys, 'index', '--model', 'none')[0] == 0
    code, out, _ = run(capsys, 'doctor')
    assert code == 0 and f'(chunk_version {chunks.CHUNK_VERSION})' in out and 'warning' not in out
    result = tools.status(home)
    assert result['ok'] and result['notice'] == []
    index_status = result['result']['index']
    assert index_status['chunk_version'] == chunks.CHUNK_VERSION
    assert index_status['last_run']['chunks'] == result['result']['db']['chunks'] > 0
    assert index_status['model'] == {'name': None, 'model_key': None, 'vectors': 0}
    assert index_status['fts']['consistent'] is store.fts5_available()


def test_index_loads_the_model_only_when_there_is_new_text(home, scanned, capsys, monkeypatch):
    """The model key comes from the manifest; the model loads at the first embed (design M2 §6.3 P6),
    and a model that cannot load leaves chunks and FTS with the fixed reason code (§3.4)."""
    from workmcp import embedder
    loads = []

    class Blocked:
        def __init__(self, *args, **kwargs):
            loads.append(args)
            raise ImportError('DLL load failed (synthetic)')

    monkeypatch.setattr(models, 'quick_status', lambda name, home: {'name': name, 'available': True, 'message': 'ok'})
    monkeypatch.setattr(embedder, 'OnnxEmbedder', Blocked)
    code, out, err = run(capsys, 'index')
    assert code == 0, err
    assert len(loads) == 1 and 'vectors: skipped (runtime_import_failed:ImportError)' in out
    assert 'embedded 0' in out
    # every text has a vector for the model key: the next run neither loads nor hashes the model
    from workmcp import index
    key = index.model_key(models.identity('bge-m3'))
    db = store.open_home(home)
    try:
        db.executemany("INSERT INTO chunk_vectors VALUES(?, ?, 1024, x'00', 'x')",
                       [(key, sha) for (sha,) in db.execute('SELECT DISTINCT text_sha256 FROM chunks').fetchall()])
    finally:
        db.close()
    code, out, _ = run(capsys, 'index')
    assert code == 0 and len(loads) == 1 and 'embedded 0' in out and 'remaining 0' in out
    assert 'load 0.0s' in out

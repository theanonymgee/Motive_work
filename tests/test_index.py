"""Chunk/vector index (design M2 §4, §6.2 I1-I2, §10): the static rule that chunk rows are written and
removed only through ``store.insert_chunks``/``store.drop_chunks`` (X-3), the ``--reparse`` path
through ``store.drop_chunks``, the shared helpers, and ``run_index`` (incremental runs, reuse, drop,
``--gc``, resume, ``--limit``, FTS sync, locks, memory). HashEmbedder and synthetic files only.
"""
import ast
import re
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from fixtures import LINES_KO
from workmcp import chunks, collect, config, embedder, index, paths, store

PACKAGE = Path(__file__).resolve().parents[1]/'src'/'workmcp'
# SQL that adds, changes or removes rows of chunks or chunks_fts.
CHUNK_WRITE = re.compile(r'\b(INSERT\s+(OR\s+\w+\s+)?INTO|REPLACE\s+INTO|DELETE\s+FROM|UPDATE(\s+OR\s+\w+)?)\s+'
                         r'["`\[]?chunks(_fts)?\b', re.IGNORECASE)
ALLOWED = {('store.py', 'insert_chunks'), ('store.py', 'drop_chunks'), ('store.py', 'rebuild_fts')}


def scoped_strings(tree):
    """``(function name or '<module>', text)``: the string constants of each top-level function or
    method body (and of the module outside them) joined in source order, so a statement split over
    several literals (``'DELETE FROM ' + 'chunks'``, f-strings) is still seen whole."""
    scopes = {}

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                scopes.setdefault(scope, []).append(child.value)
            visit(child, inner)

    visit(tree, '<module>')
    return {scope: ' '.join(parts) for scope, parts in scopes.items()}


def chunk_sql_outside_store(name: str, source: str) -> list[str]:
    return [f'{name}:{scope}' for scope, text in scoped_strings(ast.parse(source)).items()
            if CHUNK_WRITE.search(text) and (name, scope) not in ALLOWED]


def test_chunk_sql_lives_only_in_store_insert_and_drop():
    found = []
    for path in sorted(PACKAGE.rglob('*.py')):
        found += chunk_sql_outside_store(path.relative_to(PACKAGE).as_posix(), path.read_text(encoding='utf-8'))
    assert found == []
    store_source = (PACKAGE/'store.py').read_text(encoding='utf-8')
    writers = {scope for scope, text in scoped_strings(ast.parse(store_source)).items() if CHUNK_WRITE.search(text)}
    assert writers == {'insert_chunks', 'drop_chunks', 'rebuild_fts'}


@pytest.mark.parametrize('snippet', [
    "db.execute('DELETE FROM chunks WHERE version_id=?', (v,))",
    "db.execute('delete from chunks_fts where rowid=?', (r,))",
    "db.execute(\"INSERT INTO chunks_fts(chunks_fts, rowid, text) SELECT 'delete', id, text FROM chunks\")",
    "db.execute('INSERT INTO chunks(version_id, text) VALUES(?, ?)', row)",
    "db.execute('INSERT OR REPLACE INTO chunks VALUES(?)', row)",
    "db.execute('REPLACE INTO chunks VALUES(?)', row)",
    "db.execute('UPDATE chunks SET text=? WHERE id=?', row)",
    "db.execute('DELETE FROM ' + 'chunks')",
    "db.execute(f'DELETE FROM chunks WHERE {where}')",
    "db.execute('DELETE FROM \"chunks\" WHERE 1')",
    "db.executemany('''INSERT INTO\n chunks(text) VALUES(?)''', rows)",
])
def test_chunk_sql_check_flags_every_form(snippet):
    source = f'def fill(db, v, r, row, rows, where):\n    {snippet}\n'
    assert chunk_sql_outside_store('collect.py', source) == ['collect.py:fill']
    assert chunk_sql_outside_store('store.py', source) == ['store.py:fill']  # other store functions too


def test_chunk_sql_check_lets_other_tables_and_reads_pass():
    source = ('def f(db):\n'
              "    db.execute('DELETE FROM chunk_vectors WHERE text_sha256 NOT IN (SELECT text_sha256 FROM chunks)')\n"
              "    db.execute('SELECT id, text FROM chunks WHERE version_id=?', (1,))\n"
              "    db.execute('DELETE FROM units WHERE version_id=?', (1,))\n"
              "    db.execute('INSERT INTO units(version_id) SELECT version_id FROM chunks')\n")
    assert chunk_sql_outside_store('index.py', source) == []


@pytest.mark.skipif(not store.fts5_available(), reason='SQLite without FTS5 trigram')
def test_reparse_drops_chunks_and_their_fts_rows_through_store(workmcp_home, tmp_path):
    """``scan --reparse`` re-parses in place and removes the version's chunks with their FTS rows
    (M1 appendix A: there is no chunk foreign key in the way any more)."""
    home = paths.ensure_home(paths.resolve_home())
    db = store.open_home(home)
    try:
        root = tmp_path/'src'
        root.mkdir()
        (root/'a.txt').write_text('\n'.join(LINES_KO), encoding='utf-8')
        spec = config.RootSpec(root.resolve(), str(root))
        ids = config.sync_roots(db, [spec], from_config=False)
        collect.scan(db, [(ids[spec.key], spec)], config.ScanSettings(), partial=True)
        [(version, units)] = db.execute('SELECT id, unit_count FROM versions').fetchall()
        text = LINES_KO[0]
        store.insert_chunks(db, version, chunks.CHUNK_VERSION, [chunks.ChunkSpec(1, 1, 1, 0, len(text), text)])
        db.execute('UPDATE versions SET parser_version=? WHERE id=?', ('text-0', version))
        db.execute("INSERT INTO assertions(kind, statement, recorded_from, status, source_kind, recorded_by) "
                   "VALUES('fact', '합성 사실', '2026-10-24T09:00:00+09:00', 'active', 'evidence', 'test')")
        chunk_id = db.execute('SELECT id FROM chunks').fetchone()[0]
        db.execute("INSERT INTO evidence(assertion_id, version_id, chunk_id, locator_json, quote, quote_sha256) "
                   "VALUES(1, ?, ?, '{}', '합성', ?)", (version, chunk_id, '0' * 64))
        result = collect.scan(db, [(ids[spec.key], spec)], config.ScanSettings(), partial=True, reparse=True)
        assert result['total']['reparsed'] == 1
        assert db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0] == 0
        assert db.execute('SELECT unit_count FROM versions WHERE id=?', (version,)).fetchone()[0] == units
        assert store.fts_status(db)['consistent'] and store.fts_rows(db) == 0
        assert db.execute('SELECT chunk_id FROM evidence').fetchone()[0] == chunk_id  # the hint stays
    finally:
        db.close()


def test_model_key_is_the_canonical_identity_hash():
    first = embedder.HashEmbedder(64).identity
    assert index.model_key(first) == index.model_key(dict(reversed(list(first.items()))))
    assert index.model_key(first) != index.model_key(embedder.HashEmbedder(64, salt='x').identity)
    assert re.fullmatch('[0-9a-f]{64}', index.model_key(first))


def test_vector_blobs_round_trip_and_reject_corrupt_rows():
    vector = embedder.HashEmbedder(32).embed(['합성 문장'], query=False)[0]
    blob = index.to_blob(vector)
    assert len(blob) == 32 * 4
    assert np.array_equal(index.from_blob(blob, 32), vector)
    assert index.from_blob(blob[:-4], 32) is None
    assert index.from_blob(index.to_blob(vector * 2), 32) is None
    assert index.from_blob(index.to_blob(np.full(32, np.nan)), 32) is None


def test_validate_vectors_checks_count_dims_and_values():
    matrix = np.array([[3.0, 4.0], [0.0, 2.0]])
    assert np.allclose(index.validate_vectors(matrix, 2, 2), [[0.6, 0.8], [0.0, 1.0]])
    assert index.validate_vectors(matrix, 2, 2).dtype == np.float32
    for bad, count, dims in ((matrix, 3, 2), (matrix, 2, 3), (np.array([[0.0, 0.0]]), 1, 2),
                             (np.array([[np.inf, 1.0]]), 1, 2), ([[1.0, 0.0]], 1, 2)):
        with pytest.raises(ValueError):
            index.validate_vectors(bad, count, dims)


def test_run_signature_is_fixed():
    """Stage 0 contract for stage 1 (A fills ``run``; B/C call it)."""
    import inspect
    params = inspect.signature(index.run_index).parameters
    assert list(params)[:2] == ['db', 'home']
    assert {'embedder', 'settings', 'on_event', 'limit', 'doc_ids', 'rebuild_fts', 'gc', 'keep_history',
            'vectors_note'} <= {name for name, p in params.items() if p.kind is p.KEYWORD_ONLY}
    assert {'chunked', 'embedded', 'dropped_versions', 'fts_rows', 'vectors_note', 'elapsed'} <= index.RUN_KEYS


# -- run_index (design M2 §4.1, §10) ---------------------------------------------------------

def scan_root(db, root):
    spec = config.RootSpec(root.resolve(), str(root))
    ids = config.sync_roots(db, [spec], from_config=False)
    return collect.scan(db, [(ids[spec.key], spec)], config.ScanSettings(), partial=True)


@pytest.fixture
def bundle_db(workmcp_home, tmp_path):
    from fixtures.bundle import write_bundle
    home = paths.ensure_home(paths.resolve_home())
    root = tmp_path/'bundle'
    root.mkdir()
    write_bundle(root)
    db = store.open_home(home)
    scan_root(db, root)
    yield home, db, root
    db.close()


def count(db, sql, *args):
    return db.execute(sql, args).fetchone()[0]


def test_first_index_chunks_and_embeds_every_eligible_version(bundle_db):
    home, db, _ = bundle_db
    result = index.run_index(db, home, embedder=embedder.HashEmbedder(64))
    assert set(result) == index.RUN_KEYS
    eligible = [row[0] for row in db.execute(f'SELECT v.id FROM {store.eligible_versions_sql()}')]
    assert eligible and result['versions_eligible'] == len(eligible) == result['chunked']
    for version_id in eligible:
        assert count(db, 'SELECT COUNT(*) FROM chunks WHERE version_id=? AND chunk_version=?',
                     version_id, chunks.CHUNK_VERSION) > 0
    distinct = count(db, 'SELECT COUNT(DISTINCT text_sha256) FROM chunks')
    assert result['embedded'] == distinct == count(db, 'SELECT COUNT(*) FROM chunk_vectors') == result['vectors_for_model']
    assert result['chunks_total'] == count(db, 'SELECT COUNT(*) FROM chunks')
    if store.fts5_available():
        assert result['fts_rows'] == store.fts_rows(db) == result['chunks_total']
    assert store.meta_get(db, 'index_generation') == '1'
    assert store.meta_get(db, 'chunk_version') == chunks.CHUNK_VERSION
    assert db.execute('SELECT status FROM index_runs').fetchall() == [('done',)]


def test_second_index_processes_nothing(bundle_db):
    home, db, _ = bundle_db
    vectors = embedder.HashEmbedder(64)
    index.run_index(db, home, embedder=vectors)
    second = index.run_index(db, home, embedder=vectors)
    assert (second['chunked'], second['embedded'], second['dropped_versions'], second['remaining']) == (0, 0, 0, 0)
    assert second['unchanged'] == second['versions_eligible']
    assert store.meta_get(db, 'index_generation') == '2'


def long_lines(tag, count=30, width=260):
    """Synthetic lines of about ``width`` characters, distinct per ``tag`` and line."""
    base = ' '.join(LINES_KO) * 3
    return [f'{tag} 줄 {i:03d} ' + base[(i * 7) % 50:(i * 7) % 50 + width] for i in range(1, count + 1)]


@pytest.fixture
def text_home(workmcp_home, tmp_path):
    """Data home with a small root of synthetic text files: ``a.txt``, ``b.txt``, ``c.md``."""
    home = paths.ensure_home(paths.resolve_home())
    root = tmp_path/'texts'
    root.mkdir()
    for name, tag in (('a.txt', '가'), ('b.txt', '나'), ('c.md', '다')):
        (root/name).write_text('\n'.join(long_lines(f'합성{tag}')), encoding='utf-8')
    db = store.open_home(home)
    scan_root(db, root)
    yield home, db, root
    db.close()


def vector_hashes(db):
    return {row[0] for row in db.execute('SELECT text_sha256 FROM chunk_vectors')}


def chunk_hashes(db, version_id=None):
    sql, args = 'SELECT text_sha256 FROM chunks', ()
    if version_id is not None:
        sql, args = sql + ' WHERE version_id=?', (version_id,)
    return {row[0] for row in db.execute(sql, args)}


def latest_version(db, name):
    return db.execute('SELECT v.id FROM versions v JOIN documents d ON d.id = v.document_id '
                      'WHERE d.path LIKE ? ORDER BY v.seq DESC LIMIT 1', (f'%\\{name}',)).fetchone()[0]


def check_fts(db):
    if store.fts5_available():
        assert store.fts_status(db)['consistent']


def test_changed_document_reembeds_only_new_text(text_home):
    home, db, root = text_home
    vectors = embedder.HashEmbedder(64)
    index.run_index(db, home, embedder=vectors)
    old_version = latest_version(db, 'a.txt')
    before = vector_hashes(db)
    lines = long_lines('합성가')
    lines[20] = '바뀐 합성 줄 ' + lines[20]
    (root/'a.txt').write_text('\n'.join(lines), encoding='utf-8')
    scan_root(db, root)
    new_version = latest_version(db, 'a.txt')
    assert new_version != old_version
    result = index.run_index(db, home, embedder=vectors)
    new_texts = chunk_hashes(db, new_version) - before
    assert result['chunked'] == 1 and result['dropped_versions'] == 1
    assert 0 < result['embedded'] == len(new_texts) < len(chunk_hashes(db, new_version))
    assert result['reused_vectors'] == len(chunk_hashes(db, new_version)) - len(new_texts)
    assert chunk_hashes(db, old_version) == set()  # the superseded version's chunks are gone
    assert before <= vector_hashes(db)  # ... but its vectors stay (content-addressed)
    check_fts(db)


def test_restored_or_copied_document_reuses_chunks_and_vectors(text_home):
    home, db, root = text_home
    vectors = embedder.HashEmbedder(64)
    index.run_index(db, home, embedder=vectors)
    original = (root/'b.txt').read_text(encoding='utf-8')
    (root/'b.txt').write_text(original + '\n덧붙인 합성 줄', encoding='utf-8')  # A -> B
    scan_root(db, root)
    (root/'b.txt').write_text(original, encoding='utf-8')                   # B -> A, before the next index
    scan_root(db, root)
    (root/'b_copy.txt').write_text(original, encoding='utf-8')              # and a copy of A
    scan_root(db, root)
    result = index.run_index(db, home, embedder=vectors)
    assert result['chunked'] == 2 and result['reused_chunks'] == 2  # restored and copy take the chunks of A
    assert result['embedded'] == 0 and result['reused_vectors'] > 0
    assert chunk_hashes(db, latest_version(db, 'b.txt')) == chunk_hashes(db, latest_version(db, 'b_copy.txt'))
    check_fts(db)
    # A -> B -> A across index runs: A's chunks were dropped meanwhile, so they are built again, vectors reused
    (root/'b.txt').write_text(original + '\n또 덧붙인 줄', encoding='utf-8')
    scan_root(db, root)
    index.run_index(db, home, embedder=vectors)
    (root/'b.txt').write_text(original, encoding='utf-8')
    scan_root(db, root)
    result = index.run_index(db, home, embedder=vectors)
    assert result['chunked'] == 1 and result['embedded'] == 0


def test_deleted_document_chunks_are_dropped_and_gc_removes_orphan_vectors(text_home):
    home, db, root = text_home
    vectors = embedder.HashEmbedder(64)
    index.run_index(db, home, embedder=vectors)
    gone = latest_version(db, 'c.md')
    gone_texts = chunk_hashes(db, gone)
    (root/'c.md').unlink()
    scan_root(db, root)
    result = index.run_index(db, home, embedder=vectors)
    assert result['dropped_versions'] == 1 and result['gc_removed'] == 0
    assert chunk_hashes(db, gone) == set()
    check_fts(db)
    assert gone_texts <= vector_hashes(db)  # kept until --gc
    index.run_index(db, home, embedder=embedder.HashEmbedder(32, salt='other'))
    result = index.run_index(db, home, embedder=vectors, gc=True)
    assert result['gc_removed'] == len(gone_texts)  # every model's rows of texts no chunk uses
    assert vector_hashes(db) == chunk_hashes(db)
    assert gone_texts.isdisjoint(vector_hashes(db))
    assert count(db, 'SELECT COUNT(DISTINCT model_key) FROM chunk_vectors') == 2


class Spy:
    """Embedder wrapper: records batch sizes and raises ``error`` on the ``fail_at``-th call."""

    def __init__(self, inner, fail_at=None, error=KeyboardInterrupt):
        self.inner, self.fail_at, self.error, self.calls = inner, fail_at, error, []
        self.identity = inner.identity

    def embed(self, texts, *, query):
        self.calls.append(len(texts))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise self.error()
        return self.inner.embed(texts, query=query)


def test_index_resumes_after_interrupt(text_home):
    home, db, _ = text_home
    settings = config.IndexSettings(embed_batch=2, commit_every=4)
    index.run_index(db, home)  # chunks only
    total = count(db, 'SELECT COUNT(DISTINCT text_sha256) FROM chunks')
    assert total > 10
    spy = Spy(embedder.HashEmbedder(64), fail_at=4)
    with pytest.raises(KeyboardInterrupt):
        index.run_index(db, home, embedder=spy, settings=settings)
    committed = count(db, 'SELECT COUNT(*) FROM chunk_vectors')
    assert committed == 4  # three batches of 2 embedded, one commit of 4 rows reached the store
    assert db.execute('SELECT status FROM index_runs ORDER BY id DESC LIMIT 1').fetchone() == ('aborted',)
    result = index.run_index(db, home, embedder=embedder.HashEmbedder(64), settings=settings)
    assert result['embedded'] == total - committed and result['remaining'] == 0
    clean = embedder.HashEmbedder(64)
    for sha, blob in db.execute('SELECT text_sha256, vector FROM chunk_vectors'):
        text = db.execute('SELECT text FROM chunks WHERE text_sha256=? LIMIT 1', (sha,)).fetchone()[0]
        assert np.array_equal(index.from_blob(blob, 64), clean.embed([text], query=False)[0])


def test_stale_running_row_is_marked_aborted(text_home):
    home, db, _ = text_home
    db.execute("INSERT INTO index_runs(started_at, status, chunk_version) VALUES('x', 'running', 'v')")
    index.run_index(db, home)
    assert [row[0] for row in db.execute('SELECT status FROM index_runs ORDER BY id')] == ['aborted', 'done']


def test_index_limit_bounds_the_run(text_home):
    home, db, _ = text_home
    vectors = embedder.HashEmbedder(64)
    first = index.run_index(db, home, embedder=vectors, limit=5)
    total = count(db, 'SELECT COUNT(DISTINCT text_sha256) FROM chunks')
    assert (first['embedded'], first['remaining'], first['limit']) == (5, total - 5, 5)
    second = index.run_index(db, home, embedder=vectors, limit=5)
    assert second['embedded'] == 5 and second['chunked'] == 0
    rest = index.run_index(db, home, embedder=vectors)
    assert rest['embedded'] == total - 10 and rest['remaining'] == 0


def test_embed_batches_never_exceed_batch_size(text_home):
    home, db, _ = text_home
    spy = Spy(embedder.HashEmbedder(64))
    result = index.run_index(db, home, embedder=spy, settings=config.IndexSettings(embed_batch=3, commit_every=5))
    assert spy.calls and max(spy.calls) <= 3 and sum(spy.calls) == result['embedded']


def test_index_without_model_builds_chunks_and_fts_only(text_home):
    from workmcp import cli
    home, db, _ = text_home
    result = index.run_index(db, home, vectors_note='model_missing: synthetic')
    assert result['chunked'] == 3 and result['embedded'] == 0 and result['model_key'] is None
    assert result['vectors_note'] == 'model_missing: synthetic'
    assert count(db, 'SELECT COUNT(*) FROM chunk_vectors') == 0 and result['chunks_total'] > 0
    check_fts(db)
    assert 'vectors: skipped (model_missing: synthetic)' in cli.index_summary_lines(result, 'log')[3]


def test_chunk_version_change_rechunks_and_drops_old_rows(text_home, monkeypatch):
    home, db, _ = text_home
    index.run_index(db, home)
    before = count(db, 'SELECT COUNT(*) FROM chunks')
    monkeypatch.setattr(chunks, 'CHUNK_VERSION', 'units-test-v2')
    result = index.run_index(db, home)
    assert result['chunked'] == 3 and result['chunk_version'] == 'units-test-v2'
    assert {row[0] for row in db.execute('SELECT DISTINCT chunk_version FROM chunks')} == {'units-test-v2'}
    assert count(db, 'SELECT COUNT(*) FROM chunks') == before
    check_fts(db)
    assert store.meta_get(db, 'chunk_version') == 'units-test-v2'


def test_keep_history_and_doc_filter(text_home):
    home, db, root = text_home
    index.run_index(db, home)
    old = latest_version(db, 'a.txt')
    (root/'a.txt').write_text('새 합성 내용 한 줄', encoding='utf-8')
    (root/'b.txt').write_text('새 합성 내용 두 줄', encoding='utf-8')
    scan_root(db, root)
    doc_a = db.execute("SELECT id FROM documents WHERE path LIKE '%a.txt'").fetchone()[0]
    result = index.run_index(db, home, doc_ids=[doc_a], keep_history=True)
    assert (result['versions_eligible'], result['chunked'], result['dropped_versions']) == (1, 1, 0)
    assert chunk_hashes(db, old)  # history kept
    assert not chunk_hashes(db, latest_version(db, 'b.txt'))  # other documents untouched
    result = index.run_index(db, home)
    assert result['chunked'] == 1 and result['dropped_versions'] == 2
    check_fts(db)


@pytest.mark.skipif(not store.fts5_available(), reason='SQLite without FTS5 trigram')
def test_fts_is_rebuilt_on_request_and_when_counts_differ(text_home, monkeypatch):
    home, db, root = text_home
    assert not index.run_index(db, home)['fts_rebuilt']
    assert not index.run_index(db, home)['fts_rebuilt']
    assert index.run_index(db, home, rebuild_fts=True)['fts_rebuilt']
    # chunks written by a Python without FTS5: the next run finds the gap and rebuilds the index
    (root/'d.txt').write_text('\n'.join(long_lines('합성라')), encoding='utf-8')
    scan_root(db, root)
    with monkeypatch.context() as patch:
        patch.setattr(store, 'fts5_available', lambda: False)
        assert index.run_index(db, home)['fts_rows'] is None
    assert not store.fts_status(db)['consistent']
    events = []
    result = index.run_index(db, home, on_event=events.append)
    assert result['fts_rebuilt'] and store.fts_status(db)['consistent']
    assert [e['event'] for e in events] == ['fts_rebuilt']


def test_locked_version_is_skipped_and_counted(text_home, monkeypatch):
    home, db, _ = text_home
    db.execute('PRAGMA busy_timeout=50')
    other = store.open_home(home)
    try:
        other.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError):  # another writer holds the store from the start
            index.run_index(db, home)
        other.execute('ROLLBACK')
    finally:
        other.close()
    real, calls = index._chunk_version, []

    def busy_once(*args):  # a writer that takes the store while the run is going
        calls.append(args)
        if len(calls) == 1:
            raise sqlite3.OperationalError('database is locked')
        return real(*args)

    monkeypatch.setattr(index, '_chunk_version', busy_once)
    result = index.run_index(db, home)
    assert result['skipped_locked'] == 1 and result['chunked'] == 2
    assert index.run_index(db, home)['chunked'] == 1  # picked up by the next run


def test_index_events_and_runs_carry_no_document_text(text_home):
    from fixtures import SENTINEL
    home, db, root = text_home
    (root/'s.txt').write_text(f'{SENTINEL} 합성 표지 줄\n' * 3, encoding='utf-8')
    scan_root(db, root)
    events = []
    index.run_index(db, home, embedder=embedder.HashEmbedder(64), on_event=events.append)
    assert events and all(set(e) == index.EVENT_KEYS for e in events)
    assert SENTINEL not in repr(events)
    assert SENTINEL not in repr(db.execute('SELECT * FROM index_runs').fetchall())
    assert count(db, 'SELECT COUNT(*) FROM chunks WHERE instr(text, ?) > 0', SENTINEL) > 0


def test_index_holds_one_version_at_a_time(workmcp_home, tmp_path):
    """Design M2 §6.2 I1: peak Python allocation stays within the bound of the largest version,
    ``4 B x 1.2 x T_v + 1 KiB x U_v`` (characters, units), well under the three versions together."""
    import tracemalloc
    home = paths.ensure_home(paths.resolve_home())
    root = tmp_path/'big'
    root.mkdir()
    bounds = []
    for n in range(3):
        lines = long_lines(f'큰{n}', count=1500, width=300)
        (root/f'big{n}.txt').write_text('\n'.join(lines), encoding='utf-8')
        bounds.append(4 * 1.2 * sum(map(len, lines)) + 1024 * len(lines))
    db = store.open_home(home)
    try:
        scan_root(db, root)
        tracemalloc.start()
        try:
            result = index.run_index(db, home, embedder=embedder.HashEmbedder(64))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    finally:
        db.close()
    assert result['chunked'] == 3
    assert peak <= max(bounds) < sum(bounds) / 2, (peak, bounds)


@pytest.mark.skipif(not store.fts5_available(), reason='SQLite without FTS5 trigram')
def test_index_by_a_python_without_fts5_is_detected_and_rebuilt(text_home, monkeypatch, capsys):
    """Review M2-2: a change indexed without FTS5 leaves chunks_fts behind even when the row counts agree;
    ``status``/``doctor`` report it and the next run with FTS5 rebuilds it, so lexical search finds the text."""
    from workmcp import cli, search, tools
    home, db, root = text_home
    index.run_index(db, home)
    lines = long_lines('합성가')
    lines[5] = '교체된 줄 고유어휘조합 확인용'
    (root/'a.txt').write_text('\n'.join(lines), encoding='utf-8')
    scan_root(db, root)
    with monkeypatch.context() as patch:
        patch.setattr(store, 'fts5_available', lambda: False)
        result = index.run_index(db, home)
        assert result['chunked'] == 1 and result['dropped_versions'] == 1 and result['fts_rows'] is None
    status = store.fts_status(db)
    assert status['dirty'] and not status['consistent']
    assert any('--rebuild-fts' in line for line in tools.status(home)['notice'])
    assert cli.main(['doctor']) == 0 and 'run workmcp index --rebuild-fts' in capsys.readouterr()[0]
    hits = search.Engine(home).search('고유어휘조합', mode='lexical')['hits']
    assert hits == []                           # the index does not hold the new chunk yet
    result = index.run_index(db, home)
    assert result['fts_rebuilt'] and store.fts_status(db)['consistent']
    hits = search.Engine(home).search('고유어휘조합', mode='lexical')['hits']
    assert hits and Path(hits[0]['doc']['path']).name == 'a.txt'
    assert tools.status(home)['notice'] == []


def test_a_chunk_removed_during_the_vector_step_is_skipped(text_home):
    """Review M2-L3: a concurrent ``scan --reparse`` may remove chunks after the list of texts was made;
    the run skips them (counted in ``remaining``) instead of stopping."""
    home, db, _ = text_home
    index.run_index(db, home)
    victim = latest_version(db, 'c.md')
    gone = len(chunk_hashes(db, victim))
    total = count(db, 'SELECT COUNT(DISTINCT text_sha256) FROM chunks')

    class Remover:
        identity = embedder.HashEmbedder(64).identity

        def __init__(self):
            self.inner, self.calls = embedder.HashEmbedder(64), 0

        def embed(self, texts, *, query):
            self.calls += 1
            if self.calls == 1:  # as if another process dropped c.md's chunks now
                db.execute('BEGIN IMMEDIATE')
                store.drop_chunks(db, victim)
                db.execute('COMMIT')
            return self.inner.embed(texts, query=query)

    result = index.run_index(db, home, embedder=Remover(), settings=config.IndexSettings(embed_batch=2))
    assert result['remaining'] > 0 and result['remaining'] <= gone
    assert result['embedded'] + result['remaining'] == total
    assert count(db, 'SELECT COUNT(*) FROM chunk_vectors') == result['embedded']
    assert db.execute('SELECT status FROM index_runs ORDER BY id DESC LIMIT 1').fetchone() == ('done',)

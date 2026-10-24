"""Chunk/vector index (design M2 §4, §10). Stage 0: the static rule that chunk rows are written and
removed only through ``store.insert_chunks``/``store.drop_chunks`` (X-3), the ``--reparse`` path
that now goes through ``store.drop_chunks``, and the shared helpers of ``index``. Synthetic only.
"""
import ast
import re
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

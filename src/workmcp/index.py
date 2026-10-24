"""Incremental chunk and vector index of the latest active versions (design M2 §4).

``run_index`` chunks every eligible version (``store.eligible_versions_sql``) that has no rows of
``chunks.CHUNK_VERSION`` (copying them from a version with the same sha256 and parser version
when one exists), drops the chunks of other chunk versions and of versions that are no longer
eligible (unless ``keep_history``), then embeds every chunk text that has no vector for the
embedder's ``model_key``. Chunk rows go through ``store.insert_chunks``/``store.drop_chunks``
only (they keep ``chunks_fts`` in step). Vectors are content-addressed
``(model_key, text_sha256)``, so a text seen before (A -> B -> A, copies, repeated paragraphs) is
never embedded again. Commits: one version per transaction for chunks, every
``settings.commit_every`` vectors, so a stopped run resumes where it stopped. A second run over
unchanged data reports ``chunked 0, embedded 0``.

Ported in part from Motive_vari ``services/kakao_evidence/index.py`` (tag
kakao-p1-complete-20261022): ``model_key``, ``to_blob``/``from_blob``, vector validation.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np

from . import chunks, store
from .config import IndexSettings

NORM_TOLERANCE = 1e-3

# Keys of the dict ``run_index`` returns (the CLI summary and ``index_runs.counts_json`` read them).
RUN_KEYS = frozenset({
    'index_id', 'started_at', 'finished_at', 'chunk_version',
    'model',            # manifest name / embedder identity name, or None without an embedder
    'model_key',        # model_key(embedder.identity), or None
    'vectors_note',     # None, or why vectors were skipped ('model_missing: ...', 'runtime_import_failed:...')
    'versions_eligible', 'chunked', 'reused_chunks', 'unchanged', 'dropped_versions', 'skipped_locked',
    'chunks_total',
    'embedded', 'reused_vectors', 'truncated', 'limit', 'remaining',   # remaining: texts still to embed
    'vectors_for_model', 'fts_rows', 'fts_rebuilt', 'gc_removed',
    'elapsed',          # {'load', 'chunk', 'embed', 'total'} seconds
})

# Events passed to ``on_event`` (the CLI writes them to the index log; never document text).
EVENT_KEYS = frozenset({'event', 'path', 'sha12', 'chunks', 'note'})


def model_key(identity: dict) -> str:
    """SHA-256 of the canonical identity JSON: a different model, revision, file hash, precision,
    pooling or prefix is a different key."""
    canonical = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def validate_vectors(matrix, expected_count: int, dims: int) -> np.ndarray:
    """Count, dimension, finiteness and positive norm; returns an L2-normalised float32 copy."""
    if not isinstance(matrix, np.ndarray) or matrix.ndim != 2 or matrix.shape[0] != expected_count:
        raise ValueError('embedding count mismatch')
    if matrix.shape[1] != dims:
        raise ValueError('invalid embedding dimension')
    values = matrix.astype(np.float64)
    if not np.all(np.isfinite(values)):
        raise ValueError('embedding contains non-finite values')
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    if np.any(norms <= 0):
        raise ValueError('embedding must have a positive norm')
    return (values / norms).astype(np.float32)


def to_blob(vector: Sequence[float] | np.ndarray) -> bytes:
    """float32 little-endian bytes (4 * dims)."""
    return np.asarray(vector, dtype='<f4').tobytes()


def from_blob(blob: bytes, dims: int) -> np.ndarray | None:
    """Stored vector, or None when the row is corrupt (wrong length, non-finite, not unit norm)."""
    if len(blob) != dims * 4:
        return None
    vector = np.frombuffer(blob, dtype='<f4')
    if not np.all(np.isfinite(vector)) or abs(float(np.linalg.norm(vector)) - 1.0) > NORM_TOLERANCE:
        return None
    return vector


class EmbedderUnavailable(Exception):
    """Raised by a lazily loaded embedder whose model cannot load; ``note`` is the fixed reason code
    (``runtime_import_failed:<Class>``, ``model_unverified(...)``). ``run_index`` then finishes with
    chunks and FTS only and reports the note, as for a missing model (design M2 §3.4)."""

    def __init__(self, note: str):
        super().__init__(note)
        self.note = note


def _busy(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return 'locked' in message or 'busy' in message


def _rollback(db: sqlite3.Connection) -> None:
    if db.in_transaction:
        db.execute('ROLLBACK')


def _doc_filter(doc_ids: Sequence[int] | None) -> tuple[str, list]:
    if not doc_ids:
        return '', []
    return f" AND d.id IN ({','.join('?' * len(doc_ids))})", [int(i) for i in doc_ids]


def _chunk_version(db: sqlite3.Connection, version_id: int, sha256: str, parser_version: str) -> tuple[int, bool]:
    """Store the chunks of one version under ``CHUNK_VERSION``: copied from a version with the same
    sha256 and parser version when one has them, else built from its units. ``(rows, copied)``.
    Holds one version's units and chunks at a time (design M2 §6.2 I1)."""
    source = db.execute(
        'SELECT c.version_id FROM chunks c JOIN versions s ON s.id = c.version_id '
        'WHERE s.sha256 = ? AND s.parser_version IS ? AND c.chunk_version = ? AND c.version_id <> ? LIMIT 1',
        (sha256, parser_version, chunks.CHUNK_VERSION, version_id)).fetchone()
    if source is not None:
        return store.insert_chunks(db, version_id, chunks.CHUNK_VERSION, copy_from=source[0]), True
    # build_chunks reads a unit only through group_key, so units of one group share one representative
    # dict: memory per unit is a tuple, not a parsed locator.
    groups: dict[tuple, dict] = {}
    units = []
    for ordinal, unit_json, text in db.execute(
            'SELECT ordinal, locator_json, text FROM units WHERE version_id = ? ORDER BY ordinal', (version_id,)):
        unit = json.loads(unit_json)
        units.append((ordinal, groups.setdefault(chunks.group_key(unit), unit), text))
    if [unit[0] for unit in units] != list(range(1, len(units) + 1)):
        raise RuntimeError('units are not numbered 1..n')
    specs = chunks.build_chunks(units)
    texts = [text for _, _, text in units]
    for spec in specs:  # a mismatch is a bug in build_chunks: stop the run (design M2 §4.1)
        if '\n'.join(texts[spec.unit_first - 1:spec.unit_last])[spec.char_start:spec.char_end] != spec.text:
            raise AssertionError('chunk text is not the joined unit slice')
    return store.insert_chunks(db, version_id, chunks.CHUNK_VERSION, specs), False


def _start_run(db: sqlite3.Connection, key: str | None) -> tuple[int, str]:
    started = store.now()
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute("UPDATE index_runs SET status='aborted', finished_at=?, note='stopped before it finished' "
                   "WHERE status='running'", (started,))
        run_id = db.execute("INSERT INTO index_runs(started_at, status, model_key, chunk_version) "
                            "VALUES(?, 'running', ?, ?)", (started, key, chunks.CHUNK_VERSION)).lastrowid
        db.execute('COMMIT')
    except BaseException:
        _rollback(db)
        raise
    return run_id, started


def _sync_fts(db: sqlite3.Connection, force: bool) -> bool:
    """Rebuild ``chunks_fts`` when asked, when its row count differs from ``chunks``, or when this
    store has chunks but no record of a sync (made by a Python without FTS5). True when rebuilt."""
    if not store.fts_available(db):
        return False
    chunk_rows = db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
    never_synced = store.meta_get(db, 'fts_rows') is None and chunk_rows > 0
    if not (force or never_synced or store.fts_rows(db) != chunk_rows):
        return False
    db.execute('BEGIN IMMEDIATE')
    try:
        store.rebuild_fts(db)
        db.execute('COMMIT')
    except BaseException:
        _rollback(db)
        raise
    return True


def _texts_to_embed(db: sqlite3.Connection, key: str, doc_sql: str, doc_args: list) -> list[str]:
    """Distinct ``text_sha256`` of eligible chunks without a vector for ``key``, in chunk order
    (hashes only; texts are read a batch at a time)."""
    cursor = db.execute(
        f'SELECT c.text_sha256 FROM {store.latest_chunks_sql()} AND c.chunk_version = ?{doc_sql} '
        'AND NOT EXISTS(SELECT 1 FROM chunk_vectors x WHERE x.model_key = ? AND x.text_sha256 = c.text_sha256) '
        'GROUP BY c.text_sha256 ORDER BY MIN(c.id)', [chunks.CHUNK_VERSION, *doc_args, key])
    todo = []
    while block := cursor.fetchmany(4096):
        todo += [row[0] for row in block]
    return todo


def _embed(db: sqlite3.Connection, embedder, key: str, todo: list[str], settings: IndexSettings,
           emit: Callable[[dict], None]) -> tuple[int, int]:
    """Embed the texts of ``todo`` in batches of ``embed_batch``; write ``commit_every`` rows per short
    transaction (no write lock is held while the model runs). ``(embedded, truncated)``."""
    identity = embedder.identity
    dims = int(identity.get('dims') or 0)
    max_tokens = int(identity.get('max_tokens') or 0)
    batch_size = max(1, int(settings.embed_batch))
    commit_every = max(1, int(settings.commit_every))
    counter = getattr(embedder, 'token_counts', None)
    embedded = truncated = 0
    pending: list[tuple] = []

    def write():
        nonlocal embedded
        if not pending:
            return
        db.execute('BEGIN IMMEDIATE')
        try:
            db.executemany('INSERT OR IGNORE INTO chunk_vectors(model_key, text_sha256, dims, vector, embedded_at) '
                           'VALUES(?,?,?,?,?)', pending)
            db.execute('COMMIT')
        except BaseException:
            _rollback(db)
            raise
        embedded += len(pending)
        emit({'event': 'vectors', 'path': '', 'sha12': '', 'chunks': len(pending), 'note': f'{embedded} embedded'})
        pending.clear()

    for at in range(0, len(todo), batch_size):
        hashes = todo[at:at + batch_size]
        texts = [db.execute('SELECT text FROM chunks WHERE text_sha256 = ? LIMIT 1', (sha,)).fetchone()[0]
                 for sha in hashes]
        matrix = embedder.embed(texts, query=False)
        vectors = validate_vectors(matrix, len(texts), dims or int(getattr(matrix, 'shape', (0, 0))[1]))
        if counter is not None and max_tokens:
            truncated += sum(1 for count in counter(texts) if count > max_tokens)
        stamp = store.now()
        pending += [(key, sha, vectors.shape[1], to_blob(vector), stamp) for sha, vector in zip(hashes, vectors)]
        if len(pending) >= commit_every:
            write()
    write()
    return embedded, truncated


def run_index(db: sqlite3.Connection, home: Path, *, embedder=None, settings: IndexSettings = IndexSettings(),
              on_event: Callable[[dict], None] | None = None, limit: int | None = None,
              doc_ids: Sequence[int] | None = None, rebuild_fts: bool = False, gc: bool = False,
              keep_history: bool = False, vectors_note: str | None = None) -> dict:
    """Index the store at ``db`` (an ``store.open_home`` connection; the caller holds ``index.lock``).

    ``embedder``: an ``embedder.Embedder`` or None (chunks and FTS only; ``vectors_note`` says why,
    e.g. ``'model_missing: ...'``). ``limit``: at most this many texts embedded in this run.
    ``doc_ids``: only these documents. ``rebuild_fts``: ``store.rebuild_fts`` first (also done
    automatically when ``chunks_fts`` and ``chunks`` counts differ). ``gc``: drop vectors no chunk
    uses (every model). ``keep_history``: keep the chunks of superseded or deleted versions.
    Returns a dict with exactly ``RUN_KEYS``; records an ``index_runs`` row and ``meta``
    (``chunk_version``, ``index_generation`` + 1, ``fts_rows``, ``fts_synced_at``).

    Order: FTS check, chunks (one version per transaction; a version another writer holds is
    skipped and counted in ``skipped_locked``), removal of the chunks of versions that are no
    longer eligible, vectors (``commit_every`` rows per transaction), ``gc``, ``meta``. Chunks are
    removed before the long embedding step so that a stopped run leaves the chunk set clean.
    """
    begin = time.monotonic()
    emit = on_event or (lambda event: None)
    key = model_key(embedder.identity) if embedder is not None else None
    run_id, started = _start_run(db, key)
    result = dict.fromkeys(RUN_KEYS, 0)
    result.update(index_id=run_id, started_at=started, finished_at=None, chunk_version=chunks.CHUNK_VERSION,
                  model=embedder.identity.get('name') if embedder is not None else None, model_key=key,
                  vectors_note=None if embedder is not None else vectors_note, limit=limit, fts_rows=None)
    try:
        result['fts_rebuilt'] = _sync_fts(db, rebuild_fts)
        if result['fts_rebuilt']:
            emit({'event': 'fts_rebuilt', 'path': '', 'sha12': '', 'chunks': store.fts_rows(db), 'note': ''})
        doc_sql, doc_args = _doc_filter(doc_ids)
        eligible = list(db.execute(f'SELECT v.id, v.sha256, v.parser_version, d.path FROM '
                                   f'{store.eligible_versions_sql()}{doc_sql} ORDER BY v.id', doc_args))
        result['versions_eligible'] = len(eligible)

        chunk_begin = time.monotonic()
        for version_id, sha256, parser_version, path in eligible:
            has_current = db.execute('SELECT 1 FROM chunks WHERE version_id = ? AND chunk_version = ? LIMIT 1',
                                     (version_id, chunks.CHUNK_VERSION)).fetchone()
            has_other = db.execute('SELECT 1 FROM chunks WHERE version_id = ? AND chunk_version <> ? LIMIT 1',
                                   (version_id, chunks.CHUNK_VERSION)).fetchone()
            if has_current and not has_other:
                result['unchanged'] += 1
                continue
            try:
                db.execute('BEGIN IMMEDIATE')
                stored, copied = (0, False) if has_current else _chunk_version(db, version_id, sha256, parser_version)
                store.drop_chunks(db, version_id, keep=chunks.CHUNK_VERSION)
                db.execute('COMMIT')
            except sqlite3.OperationalError as exc:
                _rollback(db)
                if not _busy(exc):
                    raise
                result['skipped_locked'] += 1
                emit({'event': 'skipped_locked', 'path': path, 'sha12': sha256[:12], 'chunks': 0, 'note': ''})
                continue
            except BaseException:
                _rollback(db)
                raise
            if has_current:
                result['unchanged'] += 1
                continue
            result['chunked'] += 1
            result['reused_chunks'] += copied
            if key is not None:
                result['reused_vectors'] += db.execute(
                    'SELECT COUNT(DISTINCT c.text_sha256) FROM chunks c WHERE c.version_id = ? AND c.chunk_version = ? '
                    'AND EXISTS(SELECT 1 FROM chunk_vectors x WHERE x.model_key = ? AND x.text_sha256 = c.text_sha256)',
                    (version_id, chunks.CHUNK_VERSION, key)).fetchone()[0]
            emit({'event': 'copied' if copied else 'chunked', 'path': path, 'sha12': sha256[:12], 'chunks': stored,
                  'note': ''})

        if not keep_history:
            stale = list(db.execute(
                f'SELECT DISTINCT c.version_id, v.sha256, d.path FROM chunks c JOIN versions v ON v.id = c.version_id '
                f'JOIN documents d ON d.id = v.document_id WHERE c.version_id NOT IN '
                f'(SELECT v.id FROM {store.eligible_versions_sql()}){doc_sql} ORDER BY c.version_id', doc_args))
            for version_id, sha256, path in stale:
                try:
                    db.execute('BEGIN IMMEDIATE')
                    removed = store.drop_chunks(db, version_id)
                    db.execute('COMMIT')
                except sqlite3.OperationalError as exc:
                    _rollback(db)
                    if not _busy(exc):
                        raise
                    result['skipped_locked'] += 1
                    continue
                except BaseException:
                    _rollback(db)
                    raise
                result['dropped_versions'] += 1
                emit({'event': 'dropped', 'path': path, 'sha12': sha256[:12], 'chunks': removed, 'note': 'history'})
        result['elapsed_chunk'] = time.monotonic() - chunk_begin

        embed_begin = time.monotonic()
        if embedder is not None:
            todo = _texts_to_embed(db, key, doc_sql, doc_args)
            run = todo if limit is None else todo[:max(0, int(limit))]
            try:
                result['embedded'], result['truncated'] = _embed(db, embedder, key, run, settings, emit)
            except EmbedderUnavailable as exc:
                result.update(vectors_note=exc.note, model=None, model_key=None)
                emit({'event': 'vectors_skipped', 'path': '', 'sha12': '', 'chunks': 0, 'note': exc.note})
            result['remaining'] = len(todo) - result['embedded']
            if result['model_key'] is not None:
                result['vectors_for_model'] = db.execute('SELECT COUNT(*) FROM chunk_vectors WHERE model_key = ?',
                                                         (key,)).fetchone()[0]
        embed_elapsed = time.monotonic() - embed_begin

        db.execute('BEGIN IMMEDIATE')
        try:
            if gc:
                result['gc_removed'] = db.execute(
                    'DELETE FROM chunk_vectors WHERE text_sha256 NOT IN (SELECT text_sha256 FROM chunks)').rowcount
                if key is not None:
                    result['vectors_for_model'] = db.execute(
                        'SELECT COUNT(*) FROM chunk_vectors WHERE model_key = ?', (key,)).fetchone()[0]
            result['chunks_total'] = db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
            store.meta_set(db, 'chunk_version', chunks.CHUNK_VERSION)
            generation = int(store.meta_get(db, 'index_generation', '0') or 0) + 1
            store.meta_set(db, 'index_generation', str(generation))
            if store.fts_available(db):
                result['fts_rows'] = store.fts_rows(db)
                store.meta_set(db, 'fts_rows', str(result['fts_rows']))
                store.meta_set(db, 'fts_synced_at', store.now())
            result['finished_at'] = store.now()
            result['elapsed'] = {'load': 0.0, 'chunk': result.pop('elapsed_chunk'), 'embed': embed_elapsed,
                                 'total': time.monotonic() - begin}
            db.execute("UPDATE index_runs SET finished_at=?, status='done', counts_json=? WHERE id=?",
                       (result['finished_at'], json.dumps(result, ensure_ascii=False, sort_keys=True), run_id))
            db.execute('COMMIT')
        except BaseException:
            _rollback(db)
            raise
    except BaseException as exc:
        _rollback(db)
        try:
            db.execute("UPDATE index_runs SET finished_at=?, status='aborted', note=? WHERE id=? AND status='running'",
                       (store.now(), type(exc).__name__, run_id))
        except sqlite3.Error:
            pass  # the next run marks a 'running' row as aborted
        raise
    return result


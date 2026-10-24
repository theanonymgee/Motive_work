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

Status (M2 stage 0): ``model_key``, ``to_blob``, ``from_blob`` and ``validate_vectors`` are
complete; ``run_index`` is a signature to be filled in stage 1.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np

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
    """
    raise NotImplementedError('index.run_index: M2 stage 1')

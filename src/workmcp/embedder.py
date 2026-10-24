"""Local embedders (design M2 §3). Ported from Motive_vari ``services/kakao_evidence/embedder.py``
(tag kakao-p1-complete-20261022).

``OnnxEmbedder`` runs a manifest model with ONNX Runtime on the CPU execution provider only.
``onnxruntime`` and ``tokenizers`` are imported inside ``OnnxEmbedder.__init__`` only, so importing
this module (status, lexical search, the import audit) loads neither DLL; when the import fails
(blocked DLL) search falls back to lexical results.

``HashEmbedder`` is a deterministic character n-gram hash for tests. It is not a model and its
identity says so; code paths take it only as an injected argument (``index.run_index(embedder=...)``,
``search.Engine(embedder_factory=...)``), never from the CLI or the settings.

Every ``embed`` returns a float32 matrix ``(len(texts), dims)`` with unit rows, and a vector is a
function of its text alone (not of its batch-mates): vectors are stored by text hash.

Status (M2 stage 0): ``Embedder``, ``l2_normalize`` and ``HashEmbedder`` are complete;
``OnnxEmbedder`` is a signature to be filled in stage 1.
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np

HASH_NAME = 'hash-ngram-test-only'


class Embedder(Protocol):
    identity: dict      # canonical identity; index.model_key(identity) keys the stored vectors

    def embed(self, texts: Sequence[str], *, query: bool) -> np.ndarray: ...


def l2_normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if not np.all(np.isfinite(norms)) or np.any(norms <= 0):
        raise ValueError('embedding has a non-finite or zero norm')
    return matrix / norms


class OnnxEmbedder:
    """Tokenize with ``tokenizer.json``, run ONNX on CPU, pool per manifest, L2-normalise (design M2 §3.1).

    ``name``/``home`` select the verified model folder (``models.require_verified(name, home)``
    when ``verify``); ``batch_size`` is forced to 1 for an int8 model; ``threads`` sets
    ``intra_op_num_threads`` (None or 0: the ONNX Runtime default), ``inter_op_num_threads`` is 1.
    Raises ``ImportError`` when onnxruntime/tokenizers cannot be loaded and ``models.ModelFileError``
    when the files do not verify (search maps them to ``runtime_import_failed:<Class>`` and
    ``model_unverified(...)``).
    """
    identity: dict

    def __init__(self, name: str, home: Path, *, batch_size: int = 8, threads: int | None = None,
                 verify: bool = True):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        raise NotImplementedError('embedder.OnnxEmbedder: M2 stage 1')

    def token_counts(self, texts: Sequence[str], *, query: bool = False) -> list[int]:
        """Untruncated token counts (special tokens included, padding excluded)."""
        raise NotImplementedError('embedder.OnnxEmbedder.token_counts: M2 stage 1')

    def embed(self, texts: Sequence[str], *, query: bool) -> np.ndarray:
        raise NotImplementedError('embedder.OnnxEmbedder.embed: M2 stage 1')


class HashEmbedder:
    """Test-only: signed character 2/3-gram hashing into ``dims`` buckets. Not a real model."""

    def __init__(self, dims: int = 256, salt: str = ''):
        self.dims = dims
        self.salt = salt
        self.identity = {'name': HASH_NAME, 'repo': 'none: not a real model',
                         'revision': f'hash-v1{salt}', 'file_sha256': '', 'backend': 'python-hash',
                         'precision': 'fp32', 'pooling': 'none', 'query_prefix': '', 'passage_prefix': '',
                         'dims': dims, 'max_tokens': 0, 'real_model': False}

    def _one(self, text: str, out: np.ndarray) -> None:
        text = ' '.join(text.casefold().split())
        for n in (2, 3):
            for i in range(len(text) - n + 1):
                digest = hashlib.blake2b((self.salt + text[i:i + n]).encode(), digest_size=8).digest()
                value = int.from_bytes(digest, 'little')
                out[value % self.dims] += 1.0 if value >> 63 else -1.0
        if not out.any():
            out[0] = 1.0

    def embed(self, texts: Sequence[str], *, query: bool) -> np.ndarray:
        matrix = np.zeros((len(texts), self.dims), dtype=np.float64)
        for row, text in zip(matrix, texts):
            self._one(text, row)
        return l2_normalize(matrix).astype(np.float32) if len(texts) else matrix.astype(np.float32)

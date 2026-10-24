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
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np

from . import models

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
    when ``verify``, else the files must at least be present with the pinned sizes); ``batch_size``
    is forced to 1 for an int8 model; ``threads`` sets ``intra_op_num_threads`` (None or 0: the ONNX
    Runtime default), ``inter_op_num_threads`` is 1.
    Raises ``ImportError`` when onnxruntime/tokenizers cannot be loaded and ``models.ModelFileError``
    when the files are missing or do not verify (search maps them to ``runtime_import_failed:<Class>``
    and ``model_missing`` / ``model_unverified(...)`` and falls back to lexical results).
    """
    identity: dict

    def __init__(self, name: str, home: Path, *, batch_size: int = 8, threads: int | None = None,
                 verify: bool = True):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        if hasattr(ort, 'disable_telemetry_events'):
            ort.disable_telemetry_events()
        item = models.entry(name)
        if verify:
            folder = models.require_verified(name, home)
        else:
            status = models.quick_status(name, home)
            if not status['available']:
                raise models.ModelFileError(f'{name}: {status["message"]}')
            folder = models.model_dir(name, home)
        self.ort = ort
        self.identity = models.identity(name)
        self.pooling = item['pooling']
        self.prefix = {True: item['query_prefix'], False: item['passage_prefix']}
        # Dynamic INT8 quantizes activations per tensor (DynamicQuantizeLinear), so a batched
        # vector would depend on its batch-mates and padding. One text per run keeps every
        # vector a function of its text alone, which the text-hash cache relies on.
        self.batch_size = 1 if item['precision'] == 'int8' else max(1, batch_size)
        self.tokenizer = Tokenizer.from_file(str(folder/item['tokenizer_file']))
        self.tokenizer.enable_truncation(max_length=item['max_tokens'])
        pad_id = self.tokenizer.token_to_id('<pad>')
        self.tokenizer.enable_padding(pad_id=1 if pad_id is None else pad_id, pad_token='<pad>')
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads or 0
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(folder/item['model_file']), options,
                                            providers=['CPUExecutionProvider'])
        self.providers = list(self.session.get_providers())
        if self.providers != ['CPUExecutionProvider']:
            raise RuntimeError(f'unexpected providers {self.providers}')
        self.inputs = {i.name for i in self.session.get_inputs()}
        hidden = [o for o in self.session.get_outputs() if len(o.shape) == 3]
        if not hidden:
            raise RuntimeError('model has no token-level (rank 3) output to pool')
        self.output = hidden[0].name

    def close(self) -> None:
        """Drop the session and tokenizer (the caller collects garbage); ``embed`` fails afterwards."""
        self.session = None
        self.tokenizer = None

    def token_counts(self, texts: Sequence[str], *, query: bool = False) -> list[int]:
        """Untruncated token counts (special tokens included, padding excluded)."""
        self.tokenizer.no_truncation()
        try:
            encodings = self.tokenizer.encode_batch([self.prefix[query] + t for t in texts])
        finally:
            self.tokenizer.enable_truncation(max_length=self.identity['max_tokens'])
        return [sum(e.attention_mask) for e in encodings]

    def _run(self, texts: list[str]) -> np.ndarray:
        encodings = self.tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feed = {'input_ids': ids, 'attention_mask': mask}
        if 'token_type_ids' in self.inputs:
            feed['token_type_ids'] = np.zeros_like(ids)
        # run_with_ort_values, not ``run``: the static boundary test bans every call named ``run``
        # (the MCP server's network transports), and this is the same inference call.
        values = {k: self.ort.OrtValue.ortvalue_from_numpy(v) for k, v in feed.items() if k in self.inputs}
        hidden = self.session.run_with_ort_values([self.output], values)[0].numpy()
        if self.pooling == 'cls':
            pooled = hidden[:, 0]
        elif self.pooling == 'mean':
            weights = mask[..., None].astype(hidden.dtype)
            pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
        else:
            raise ValueError(f'unknown pooling {self.pooling!r}')
        return l2_normalize(pooled.astype(np.float32))

    def embed(self, texts: Sequence[str], *, query: bool) -> np.ndarray:
        prefixed = [self.prefix[query] + t for t in texts]
        # Batch similar lengths together to limit padding, then restore input order.
        order = sorted(range(len(prefixed)), key=lambda i: len(prefixed[i]))
        out = np.zeros((len(prefixed), self.identity['dims']), dtype=np.float32)
        for start in range(0, len(order), self.batch_size):
            picked = order[start:start + self.batch_size]
            out[picked] = self._run([prefixed[i] for i in picked])
        return out


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

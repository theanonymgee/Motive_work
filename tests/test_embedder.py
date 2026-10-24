"""Embedders (design M2 §3): the test-only ``HashEmbedder`` and ``l2_normalize`` that the index and search
tests use, ``OnnxEmbedder`` failure behaviour with synthetic files, and ``OnnxEmbedder`` on the real model
(only when it is there: ``WORKMCP_TEST_MODEL_HOME`` or the default data home; otherwise skipped with the
reason). Synthetic text only.
"""
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import _real_model
from workmcp import embedder, index, models

TEXTS = ['합성 회의록 첫째 문장', 'Synthetic quote for Q-0042', '합성 회의록 첫째 문장', '']


def test_hash_embedder_is_deterministic_normalised_and_not_real():
    model = embedder.HashEmbedder(64)
    first, again = model.embed(TEXTS, query=False), embedder.HashEmbedder(64).embed(TEXTS, query=True)
    assert np.array_equal(first, again)  # same text, same vector; query/passage alike (no prefixes)
    assert np.array_equal(first[0], first[2]) and not np.array_equal(first[0], first[1])
    assert np.allclose(np.linalg.norm(first, axis=1), 1.0, atol=1e-6)
    assert first[3].tolist() == [1.0] + [0.0] * 63  # empty text still gets a unit vector
    assert model.identity['real_model'] is False and model.identity['name'] == embedder.HASH_NAME
    assert model.identity['dims'] == 64 and embedder.HashEmbedder(64, salt='x').identity != model.identity
    assert index.model_key(model.identity) != index.model_key(embedder.HashEmbedder(65).identity)


def test_embed_returns_float32_matrix():
    for dims in (32, 1024):
        matrix = embedder.HashEmbedder(dims).embed(TEXTS, query=False)
        assert isinstance(matrix, np.ndarray) and matrix.dtype == np.float32 and matrix.shape == (len(TEXTS), dims)
    empty = embedder.HashEmbedder(8).embed([], query=True)
    assert empty.dtype == np.float32 and empty.shape == (0, 8)


def test_hash_vector_is_independent_of_batch_mates():
    model = embedder.HashEmbedder(128)
    alone = model.embed([TEXTS[0]], query=False)[0]
    batched = model.embed(['x' * 50, TEXTS[1], TEXTS[0], '짧음'], query=False)[2]
    assert np.array_equal(alone, batched)


def test_hash_embedder_ignores_case_and_spacing():
    model = embedder.HashEmbedder(64)
    a, b = model.embed(['Synthetic  Quote\n Q-0042', 'synthetic quote q-0042'], query=False)
    assert np.array_equal(a, b)


def test_l2_normalize_rejects_zero_and_non_finite_rows():
    assert np.allclose(embedder.l2_normalize(np.array([[3.0, 4.0]])), [[0.6, 0.8]])
    for bad in (np.array([[0.0, 0.0]]), np.array([[np.nan, 1.0]])):
        with pytest.raises(ValueError):
            embedder.l2_normalize(bad)


def test_onnx_embedder_signature_is_fixed():
    """Stage 0 contract (B fills the body): ``OnnxEmbedder(name, home, *, batch_size, threads, verify)``."""
    import inspect
    params = inspect.signature(embedder.OnnxEmbedder.__init__).parameters
    assert list(params)[:3] == ['self', 'name', 'home']
    assert {'batch_size', 'threads', 'verify'} <= {n for n, p in params.items() if p.kind is p.KEYWORD_ONLY}
    assert {'embed', 'token_counts'} <= set(dir(embedder.OnnxEmbedder))


# --- OnnxEmbedder without the real model ------------------------------------------------------------

def test_onnx_embedder_without_model_files_raises_model_file_error(tmp_path):
    for verify in (True, False):
        with pytest.raises(models.ModelFileError):
            embedder.OnnxEmbedder('bge-m3', tmp_path/f'empty-home-{verify}', verify=verify)


def test_onnx_embedder_rejects_files_with_wrong_size_or_hash(tmp_path):
    folder = models.model_dir('bge-m3', tmp_path)
    folder.mkdir(parents=True)
    for spec in models.entry('bge-m3')['files']:
        (folder/models.local_name(spec)).write_bytes(b'not the model')
    for verify in (True, False):
        with pytest.raises(models.ModelFileError, match='size'):
            embedder.OnnxEmbedder('bge-m3', tmp_path, verify=verify)


def test_onnx_embedder_reports_a_blocked_runtime_as_import_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'onnxruntime', None)  # what a blocked DLL looks like to the caller
    with pytest.raises(ImportError):
        embedder.OnnxEmbedder('bge-m3', tmp_path)


def test_importing_the_embedder_loads_neither_onnxruntime_nor_tokenizers():
    src = str(Path(embedder.__file__).resolve().parents[1])
    code = ('import sys, workmcp.embedder, workmcp.models;'
            'print(sorted(m for m in ("onnxruntime", "tokenizers", "huggingface_hub") if m in sys.modules))')
    out = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'PYTHONPATH': src},
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == '[]', out.stderr


# --- OnnxEmbedder on the real model (skipped when it is not on this PC) --------------------------------

@pytest.fixture(scope='module')
def real():
    home = _real_model.real_model_home()
    if home is None:
        pytest.skip(_real_model.SKIP_REASON)
    loaded = embedder.OnnxEmbedder(models.DEFAULT_MODEL, home, batch_size=4, verify=False)  # hashes: test_models
    yield loaded
    loaded.close()


MIXED = ['회의에서 결정된 예산 항목과 담당자를 정리했다', 'The quarterly budget was approved by the committee', 'Q-0042 합성 인용', '']


def test_real_embedding_shape_dtype_norm_and_identity(real):
    matrix = real.embed(MIXED, query=False)
    dims = models.entry(models.DEFAULT_MODEL)['dims']
    assert matrix.shape == (len(MIXED), dims) and matrix.dtype == np.float32 and np.isfinite(matrix).all()
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-4)
    assert real.identity == models.identity(models.DEFAULT_MODEL) and real.identity['real_model'] is True
    assert real.providers == ['CPUExecutionProvider'] and real.embed([], query=True).shape == (0, dims)


def test_real_same_input_gives_the_same_vector_and_query_equals_passage_without_prefix(real):
    first = real.embed(MIXED[:2], query=False)
    assert np.array_equal(first, real.embed(MIXED[:2], query=False))
    assert np.array_equal(first, real.embed(MIXED[:2], query=True))  # bge-m3 has no prefixes


def test_vector_is_independent_of_batch_mates(real):
    text = MIXED[0]
    alone = real.embed([text], query=False)[0]
    mates = ['긴 문장 '*60, 'short', MIXED[1], text, 'x', MIXED[2], '다른 길이의 문장입니다 '*7, 'a b c']
    together = real.embed(mates, query=False)[mates.index(text)]
    assert float(alone @ together) >= 0.9999


def test_real_batch_size_does_not_change_vectors(real):
    texts = [MIXED[0], MIXED[1], 'z '*100, MIXED[2]]
    one_by_one = np.vstack([real.embed([t], query=False) for t in texts])
    batched = real.embed(texts, query=False)
    assert np.all(np.einsum('ij,ij->i', one_by_one, batched) >= 0.9999)


def test_real_vectors_rank_related_text_first(real):
    query = real.embed(['예산 승인 결과'], query=True)[0]
    related, unrelated = real.embed(['위원회가 올해 예산을 승인했다', '점심 메뉴는 김치찌개였다'], query=False)
    assert float(query @ related) > float(query @ unrelated)


def test_token_counts_without_truncation(real):
    long_text = '합성 문장입니다. '*400
    counts = real.token_counts(['hello world', long_text])
    assert counts[0] >= 4 and counts[1] > real.identity['max_tokens']  # counted untruncated
    assert real.embed([long_text], query=False).shape[0] == 1          # embedding still truncates and works
    assert real.token_counts(['hello world']) == counts[:1]            # truncation setting restored


def test_close_drops_the_session(real):
    spare = embedder.OnnxEmbedder(models.DEFAULT_MODEL, _real_model.real_model_home(), verify=False)
    spare.close()
    assert spare.session is None and spare.tokenizer is None

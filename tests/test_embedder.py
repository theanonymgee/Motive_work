"""Embedders (design M2 §3). Stage 0: the test-only ``HashEmbedder`` and ``l2_normalize`` that the index
and search tests use; the ``OnnxEmbedder`` tests come with its body in stage 1. Synthetic text only.
"""
import numpy as np
import pytest

from workmcp import embedder, index

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

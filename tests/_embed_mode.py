"""Body of the ``embed`` audit mode (design M2 §7.2); ``_audited_server.mode_embed`` calls ``run``.

Runs under the audit hook: load the real model from ``home`` (``OnnxEmbedder``, files verified), embed
three synthetic passages and one query, and report counts only (never text). The hook, not this code,
judges the run: no socket to a non-loopback address, no name resolution, no process creation.
``hub_modules_loaded`` is checked here too, because a hub client in ``sys.modules`` would mean a
download-capable library was imported.
"""
import sys

import numpy as np

PASSAGES = ['합성 회의록의 첫째 문장이다.', 'A synthetic sentence about Q-0042.', '합성 문서 셋째 줄 SENTINEL-EMBED']
QUERY = '합성 회의록'
HUB_MODULES = ('huggingface_hub', 'requests', 'httpx', 'urllib3')


def run(home) -> dict:
    from workmcp import embedder, models
    loaded = embedder.OnnxEmbedder(models.DEFAULT_MODEL, home)
    try:
        passages = loaded.embed(PASSAGES, query=False)
        query = loaded.embed([QUERY], query=True)
        return {'providers': loaded.providers, 'dims': int(passages.shape[1]), 'texts': len(PASSAGES),
                'queries': int(query.shape[0]),
                'unit_norms': bool(np.allclose(np.linalg.norm(np.vstack([passages, query]), axis=1), 1.0, atol=1e-4)),
                'model_key_identity': loaded.identity['name'],
                'hub_modules_loaded': sorted(m for m in HUB_MODULES if m in sys.modules)}
    finally:
        loaded.close()

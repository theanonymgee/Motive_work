"""Operating server: apply one push batch to the store and answer the collector's state requests (design M5
remote §3.2, §3.3, appendix A).

Pure functions over a SQLite connection the caller owns (the server's ``push_db``): a parsed body in, the
answer envelope out (``sync.ok`` / ``sync.error``). No starlette, no network, no file access; ``remote.py``
checks the token and the request size, decompresses (``sync.ungzip_json``) and calls these.

Rules (stage 1): one batch is one ``BEGIN IMMEDIATE`` transaction, nothing is written for a refused batch; a
``batch_id`` seen before answers its stored response and applies nothing; ids are the server's own
(``collector_documents``/``collector_versions`` map the collector's); chunks are rebuilt here with the same
``CHUNK_VERSION`` (the collector never sends chunk text); vectors are content addressed
``(model_key, text_sha256)``; a deleted document's versions and units are kept.

Stage 0 (design M5 remote §11.1) fixes the signatures and result keys (``sync.RESULT_KEYS``,
``sync.STATE_KEYS``, ``sync.STATE_PAGE_KEYS``, ``sync.VECTORS_PAGE_KEYS``); the bodies come in stage 1.
"""
from __future__ import annotations

from . import sync

STAGE1 = 'push receive: not implemented yet (M5 stage 1)'
# The collector a push token belongs to (``remote.lookup_token`` -> ``collectors`` row).
COLLECTOR_KEYS = ('id', 'name', 'retired_at')


def apply(db, collector: dict, body: dict, *, raw_bytes: int, now: str | None = None) -> dict:
    """Apply one checked-size, decompressed batch ``body`` of ``collector`` (``COLLECTOR_KEYS``) and return
    ``sync.ok(result)`` with ``sync.RESULT_KEYS[kind]`` (or ``sync.UNITS_PARTIAL_KEYS``) or ``sync.error``
    (``sync.ERROR_CODES``). ``raw_bytes`` is the request body size for ``pushes.bytes_in``."""
    raise NotImplementedError(STAGE1)


def state(db, collector: dict, *, model_key: str | None) -> dict:
    """``GET push/state``: ``sync.STATE_KEYS`` for this collector (its summary hashes from ``sync.summary``,
    the server's schema and chunk version, the model keys it holds vectors for, ``events_after``)."""
    raise NotImplementedError(STAGE1)


def state_page(db, collector: dict, *, after: int = 0, limit: int = sync.STATE_PAGE_DOCUMENTS) -> dict:
    """``GET push/state?full=1&after=N``: ``sync.STATE_PAGE_KEYS`` (``documents``: ``sync.document_summaries``
    of this collector after ``N``; ``next_after``: the last id, or None at the end)."""
    raise NotImplementedError(STAGE1)


def vectors_page(db, collector: dict, *, model_key: str, after: str = '',
                 limit: int = sync.STATE_PAGE_VECTORS) -> dict:
    """``GET push/state/vectors?model_key=K&after=SHA``: ``sync.VECTORS_PAGE_KEYS`` (``missing``: sorted
    ``text_sha256`` of this collector's target chunks without a vector for ``model_key``)."""
    raise NotImplementedError(STAGE1)

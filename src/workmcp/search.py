"""Hybrid search over the latest active versions: FTS5 trigram bm25 + cosine, RRF (design M2 §5).

Lexical candidates come from ``chunks_fts`` (``MATCH`` of quoted phrases only, so a query can never
be FTS syntax) or, for short-token queries and Pythons without FTS5, from ``LIKE``; semantic
candidates from the stored vectors of the current model (float16 matrix in memory up to
``SearchSettings.max_vectors_in_memory`` rows, streamed above that). The two rankings are fused
with reciprocal rank fusion (k = 60, average ranks for ties). Every hit carries a snippet cut
inside one unit with its exact ``locator`` and the ``units[]`` locators of the chunk (no text).
Search only reads: every connection is ``store.open_read`` (``mode=ro``). Without a model, or
when onnxruntime cannot load, it answers lexically with a ``semantic_note``.

Ported in part from Motive_vari ``services/kakao_evidence/search.py`` (tag
kakao-p1-complete-20261022): ``average_ranks``, RRF, sort keys, ``TOKEN``.

Status (M2 stage 0): constants, result keys, ``SearchError`` and the ``Engine``/``search``
signatures are fixed; the bodies are filled in stage 1.
"""
from __future__ import annotations

import gc
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np

from .config import SearchSettings

SEARCH_VERSION = 'fts5-trigram-bm25+cos-rrf60-tieavg-v1'
RRF_K = 60
TOKEN = re.compile(r'[\w가-힣]+')
QUERY_MAX_CHARS = 1000
MAX_TOKENS = 64
MIN_MATCH_CHARS = 3             # trigram matches nothing shorter
LIMIT_DEFAULT = 10
LIMIT_MAX = 50
EXT_MAX = 20
PATH_PREFIX_MAX = 1024
UNITS_CAP = 20                  # units[] per hit; more -> 'units_truncated': n
MODES = ('hybrid', 'lexical', 'semantic')
ERROR_CODES = ('empty_query', 'query_too_long', 'limit_out_of_range', 'filter_invalid')
NO_RESULTS_NOTICE = '검색 결과 없음 ≠ 회사에 그 정보가 없음'
OBSERVED_NOTICE = 'since/until은 파일이 관측된 시각(versions.observed_at)이며 문서 안의 날짜가 아님'
SHORT_QUERY_NOTICE = '질의가 짧아 어휘 검색을 할 수 없음 — 3자 이상 낱말을 포함해 주세요'

# The result shape (design M2 §5.7, the M3 ``search`` tool contract).
RESULT_KEYS = frozenset({'hits', 'meta', 'notice'})
HIT_KEYS = frozenset({'rank', 'chunk_id', 'version_id', 'doc', 'snippet', 'locator', 'units', 'scores'})
HIT_OPTIONAL_KEYS = frozenset({'units_truncated'})
DOC_KEYS = frozenset({'id', 'path', 'ext', 'sha256', 'observed_at'})
SCORE_KEYS = frozenset({'bm25', 'lexical_rank', 'cosine', 'semantic_rank', 'fusion', 'hit', 'score_is_probability'})
HIT_REASONS = ('both', 'lexical', 'semantic')
META_KEYS = frozenset({'mode', 'search_version', 'lexical', 'semantic', 'semantic_note', 'eligible_chunks',
                       'filters', 'elapsed_ms'})
LEXICAL_META_KEYS = frozenset({'engine', 'terms', 'candidates'})          # engine: 'fts5' | 'like' | 'none'
SEMANTIC_META_KEYS = frozenset({'model', 'model_key', 'real_model', 'vectors', 'candidates', 'streamed'})
ELAPSED_KEYS = frozenset({'lexical', 'embed_query', 'semantic', 'total'})
UNAVAILABLE = 'unavailable'     # meta['semantic'] when there is no usable model


class SearchError(ValueError):
    """Bad input; ``code`` is one of ``ERROR_CODES`` (CLI exit code 4)."""

    def __init__(self, code: str, message: str):
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message


class VectorCache:
    """Vectors of the current model for one ``meta.index_generation`` (design M2 §5.4, §6.2 S1)."""
    keys: list[str]             # text_sha256 per matrix row
    matrix: np.ndarray          # float16 (rows, dims)
    generation: str
    model_key: str
    streamed: bool

    @classmethod
    def load(cls, db: sqlite3.Connection, model_key: str, dims: int, settings: SearchSettings) -> VectorCache:
        """Read the vectors of eligible chunks in blocks; corrupt rows are skipped and counted."""
        raise NotImplementedError('search.VectorCache.load: M2 stage 1')

    @staticmethod
    def stream(db: sqlite3.Connection, model_key: str, dims: int, query: np.ndarray, top: int):
        """Over-cap path: score blocks of rows straight from SQL, keep the top ``top``."""
        raise NotImplementedError('search.VectorCache.stream: M2 stage 1')


def search(db: sqlite3.Connection, query: str, *, embedder=None, vectors: VectorCache | None = None,
           settings: SearchSettings = SearchSettings(), mode: str = 'hybrid', limit: int = LIMIT_DEFAULT,
           path_prefix: str | None = None, ext: Sequence[str] | None = None, since: str | None = None,
           until: str | None = None, semantic_note: str | None = None) -> dict:
    """One search on a read-only connection; returns ``{'hits', 'meta', 'notice'}`` (``RESULT_KEYS``).

    ``embedder``/``vectors`` None means lexical only (``semantic_note`` says why). Raises
    ``SearchError`` for bad input. Pure apart from reading ``db``.
    """
    raise NotImplementedError('search.search: M2 stage 1')


class Engine:
    """Per-process search state: lazy model, vector cache, idle release (design M2 §5.6).

    ``embedder_factory``: a callable returning an ``Embedder`` (tests pass ``HashEmbedder``); None
    means the manifest model through ``embedder.OnnxEmbedder``. A load failure is recorded once per
    process (``model_missing: ...``, ``model_unverified(<file>: <status>)``,
    ``runtime_import_failed:<Class>``, ``runtime_error:<Class>``) and later searches only report it.
    """

    def __init__(self, home: Path, settings: SearchSettings = SearchSettings(), *,
                 embedder_factory: Callable[[], object] | None = None, clock: Callable[[], float] = time.monotonic):
        self.home = Path(home)
        self.settings = settings
        self.embedder_factory = embedder_factory
        self.clock = clock
        self._embedder = None
        self._vectors = None
        self.load_failure: str | None = None
        self.last_used: float | None = None

    def search(self, query: str, *, mode: str = 'hybrid', limit: int = LIMIT_DEFAULT,
               path_prefix: str | None = None, ext: Sequence[str] | None = None, since: str | None = None,
               until: str | None = None) -> dict:
        """Open ``store.open_read(home)``, run ``search(...)``, close; returns ``RESULT_KEYS``."""
        raise NotImplementedError('search.Engine.search: M2 stage 1')

    def embedder(self):
        """The loaded embedder or None (lazy; failure recorded once; updates ``last_used``)."""
        raise NotImplementedError('search.Engine.embedder: M2 stage 1')

    def vectors(self, db: sqlite3.Connection) -> VectorCache | None:
        """Vector cache for the current ``meta.index_generation`` (reloaded when it changes)."""
        raise NotImplementedError('search.Engine.vectors: M2 stage 1')

    def maybe_unload(self, now: float | None = None) -> bool:
        """Release model and matrix when unused for ``settings.model_idle_seconds``; True if released."""
        raise NotImplementedError('search.Engine.maybe_unload: M2 stage 1')

    def unload(self) -> None:
        self._embedder = None
        self._vectors = None
        gc.collect()

    def reset(self) -> None:
        """Forget a recorded load failure so the next search tries the model again."""
        self.unload()
        self.load_failure = None

    def status(self) -> dict:
        """``{'loaded', 'rows', 'generation', 'matrix_bytes', 'load_failure'}`` (no text)."""
        raise NotImplementedError('search.Engine.status: M2 stage 1')


def format_hits(result: dict) -> list[str]:
    """Lines for the ``workmcp search`` console output: rank, scores, one source line
    (``locator.display``) and the snippet per hit, then the notices."""
    raise NotImplementedError('search.format_hits: M2 stage 1')

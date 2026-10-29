"""Hybrid search over the latest active versions: FTS5 trigram bm25 + cosine, RRF (design M2 §5).

Lexical candidates come from ``chunks_fts`` (``MATCH`` of quoted phrases only, so a query can never
be FTS syntax) or, for short-token queries and Pythons without FTS5, from ``LIKE`` (``instr``);
semantic candidates from the stored vectors of the current model (float16 matrix in memory up to
``SearchSettings.max_vectors_in_memory`` rows, streamed above that). The two rankings are fused
with reciprocal rank fusion (k = 60, average ranks for ties). Every hit carries a snippet cut
inside one unit with its exact ``locator`` and the ``units[]`` locators of the chunk (no text).
Search only reads: every connection is ``store.open_read`` (``mode=ro``). Without a model, or
when onnxruntime cannot load, it answers lexically with a ``semantic_note``.

Ported in part from Motive_vari ``services/kakao_evidence/search.py`` (tag
kakao-p1-complete-20261022): ``average_ranks``, RRF, sort keys, ``TOKEN``.

Memory (design M2 §6.2 S1, §6.4): vectors are read only in ``VectorCache.load`` (the cached
float16 matrix, at most ``max_vectors_in_memory`` rows) and ``VectorCache.stream`` (blocks of
``STREAM_BLOCK`` rows); chunk texts are read as lists only in ``_hits`` (at most ``limit`` chunks).
Other queries iterate their cursor or read in ``fetchmany`` blocks.

Background load (2026-10-29, operating server ``[server] model_preload``): ``Engine.preload_in_background``
runs the one model load on a daemon thread, so the server listens while the 2.28 GB model is checked
(SHA-256) and loaded. The thread runs ``Engine._load`` only (files, onnxruntime; no store, no network) and
never keeps the process alive; while it runs, ``embedder()`` answers None and a search is lexical with
``MODEL_LOADING_NOTE`` (the server's search tool first waits for the load, at most a bounded time).
"""
from __future__ import annotations

import gc
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from . import chunks, embedder as embedders, index, locator, models, parsers, snippet, store
from .config import SearchSettings

# v2 (M2 stage 2): in hybrid mode a chunk holding the whole query verbatim ranks before the others
SEARCH_VERSION = 'fts5-trigram-bm25+cos-rrf60-tieavg-exact-v2'
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
ERROR_CODES = ('empty_query', 'query_too_long', 'limit_out_of_range', 'filter_invalid', 'mode_invalid')
NO_RESULTS_NOTICE = '검색 결과 없음 ≠ 회사에 그 정보가 없음'
OBSERVED_NOTICE = 'since/until은 파일이 관측된 시각(versions.observed_at)이며 문서 안의 날짜가 아님'
SHORT_QUERY_NOTICE = '질의가 짧아 어휘 검색을 할 수 없음 — 3자 이상 낱말을 포함해 주세요'
# N1 (design M3 §9.1): chunks changed without FTS5 since the last rebuild, so chunks_fts may name other texts
FTS_DIRTY_NOTICE = '전문 색인이 어긋나 있어 느린 어휘 검색을 씁니다 — `workmcp index --rebuild-fts`'
NO_VECTORS_NOTE = 'no_vectors: no stored vectors for this model (run workmcp index with the model)'
MODEL_LOADING_NOTE = 'model_loading: the model is still loading in the background; this answer is lexical'

# The result shape (design M2 §5.7, the M3 ``search`` tool contract).
RESULT_KEYS = frozenset({'hits', 'meta', 'notice'})
HIT_KEYS = frozenset({'rank', 'chunk_id', 'version_id', 'doc', 'snippet', 'locator', 'units', 'scores'})
HIT_OPTIONAL_KEYS = frozenset({'units_truncated'})
DOC_KEYS = frozenset({'id', 'path', 'ext', 'sha256', 'observed_at'})
SCORE_KEYS = frozenset({'bm25', 'lexical_rank', 'cosine', 'semantic_rank', 'fusion', 'exact_phrase', 'hit',
                        'score_is_probability'})
HIT_REASONS = ('both', 'lexical', 'semantic')
META_KEYS = frozenset({'mode', 'search_version', 'lexical', 'semantic', 'semantic_note', 'eligible_chunks',
                       'filters', 'elapsed_ms', 'collapsed', 'signals'})  # collapsed, signals: M3 §2.2, §9.3
LEXICAL_META_KEYS = frozenset({'engine', 'terms', 'candidates'})          # engine: 'fts5' | 'like' | 'none'
SEMANTIC_META_KEYS = frozenset({'model', 'model_key', 'real_model', 'vectors', 'candidates', 'streamed'})
FILTER_KEYS = frozenset({'path_prefix', 'ext', 'since', 'until'})
ELAPSED_KEYS = frozenset({'lexical', 'embed_query', 'semantic', 'total'})
# Facts for an "absent information" judgement (design M3 §9.3), in every result: the lexical candidate
# count and how many hold the whole query (None in semantic mode: no lexical search ran), the cosine of
# the 1st and 10th semantic candidate and their difference (None without a model or with fewer
# candidates), and ``low`` computed by ``low_signal`` with ``LOW_SIGNAL``.
SIGNAL_KEYS = frozenset({'lexical_candidates', 'exact_phrase_hits', 'cosine_top1', 'cosine_top10', 'cosine_gap',
                         'low'})
DEFAULT_SIGNALS = {'lexical_candidates': None, 'exact_phrase_hits': None, 'cosine_top1': None, 'cosine_top10': None,
                   'cosine_gap': None, 'low': False}
# Rules (design M3 §9.3): R1 cosine_top1 < the cosine_top1 threshold, R2 cosine_gap < the gap threshold,
# R3 no lexical candidate and no exact phrase, R4 = R3 and R1, R5 = R3 or (R1 and no exact phrase): a query
# found as written is never "low" however far its cosine. A rule whose facts are missing (None) is False
# (semantic mode has no lexical facts, so R5 is False there). Measured in
# doc/validation/2026-10-24_M3-신호측정_v1.md; the coordinator adopted R5 with cosine_top1 < 0.49 on the
# synthetic corpus (to be measured again on real documents in M5).
LOW_SIGNAL_RULES = ('R1', 'R2', 'R3', 'R4', 'R5')
LOW_SIGNAL = {'rule': 'R5', 'cosine_top1': 0.49, 'gap': None}
LOW_SIGNAL_NOTICE = '질의와 가까운 내용이 적습니다(어휘 후보 {lexical}, 코사인 1위 {top1}) — 없는 정보일 수 있음'
UNAVAILABLE = 'unavailable'     # meta['semantic'] when there is no usable model
ENGINE_STATUS_KEYS = frozenset({'loaded', 'rows', 'generation', 'data_generation', 'matrix_bytes', 'load_failure',
                                'idle_seconds', 'loads', 'last_load_ms', 'loading', 'last_verify_ms'})
NOT_REQUESTED = 'not_requested'  # meta['semantic'] in lexical mode (the model is not even looked at)

LOAD_BLOCK = 4096               # rows per fetchmany while loading the matrix
SCORE_BLOCK = 65536             # matrix rows converted to float32 at a time (<= 256 MiB at 1024 dims)
STREAM_BLOCK = 2048             # rows per block on the streaming path: 8 MiB of float32 at 1024 dims; the
                                # blob list, its join and the float16/float32 copies make ~5x that at peak
_CONTROL = re.compile('[\x00-\x1f\x7f-\x9f​-‏  ﻿\ud800-\udfff]')
_EXT = re.compile(r'\.[a-z0-9]{1,10}')


class SearchError(ValueError):
    """Bad input; ``code`` is one of ``ERROR_CODES`` (CLI exit code 4)."""

    def __init__(self, code: str, message: str):
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message


# ---- input --------------------------------------------------------------------------------------

def clean_query(query) -> str:
    """Control, zero-width and lone-surrogate characters become spaces; then strip (design M2 §5.1)."""
    if not isinstance(query, str):
        raise SearchError('empty_query', 'query must be a string')
    text = _CONTROL.sub(' ', query).strip()
    if not text:
        raise SearchError('empty_query', 'the query is empty')
    if len(text) > QUERY_MAX_CHARS:
        raise SearchError('query_too_long', f'the query has {len(text)} characters (at most {QUERY_MAX_CHARS})')
    return text


def _instant(value, name: str) -> str:
    """An ISO 8601 date or time as an aware ISO string (local time zone when none is given)."""
    if not isinstance(value, str) or len(value) > 64:
        raise SearchError('filter_invalid', f'{name} must be an ISO 8601 date or time')
    try:
        moment = datetime.fromisoformat(value.strip())
        if moment.tzinfo is None:
            moment = moment.astimezone()   # OSError/OverflowError on Windows at the ends of the range (review L-a)
    except (ValueError, OverflowError, OSError):
        raise SearchError('filter_invalid', f'{name} must be an ISO 8601 date or time') from None
    offset = moment.utcoffset()
    if offset.seconds % 60 or offset.microseconds:  # julianday() cannot read it (review M4 v1 L4)
        raise SearchError('filter_invalid', f'{name} must be an ISO 8601 date or time (offset in whole minutes)')
    return moment.isoformat(timespec='seconds')


def check_filters(path_prefix=None, ext=None, since=None, until=None) -> dict:
    """Normalised filters (``FILTER_KEYS``); raises ``SearchError('filter_invalid')``."""
    out = {'path_prefix': None, 'ext': None, 'since': None, 'until': None}
    if path_prefix is not None:
        if not isinstance(path_prefix, str) or not path_prefix.strip() or len(path_prefix) > PATH_PREFIX_MAX:
            raise SearchError('filter_invalid', f'path_prefix must be 1-{PATH_PREFIX_MAX} characters')
        if _CONTROL.search(path_prefix):
            raise SearchError('filter_invalid', 'path_prefix has control characters')
        out['path_prefix'] = os.path.normcase(path_prefix.strip())
    if ext is not None:
        if isinstance(ext, str) or not isinstance(ext, Sequence) or not 1 <= len(ext) <= EXT_MAX:
            raise SearchError('filter_invalid', f'ext must be a list of 1-{EXT_MAX} extensions')
        normal = []
        for item in ext:
            value = item.strip().lower() if isinstance(item, str) else ''
            if not _EXT.fullmatch(value) or value not in parsers.SUPPORTED_EXTENSIONS:
                raise SearchError('filter_invalid', f'unsupported extension {str(item)[:20]!r}')
            if value not in normal:
                normal.append(value)
        out['ext'] = sorted(normal)
    if since is not None:
        out['since'] = _instant(since, 'since')
    if until is not None:
        out['until'] = _instant(until, 'until')
    if out['since'] and out['until'] and datetime.fromisoformat(out['since']) > datetime.fromisoformat(out['until']):
        raise SearchError('filter_invalid', 'since is after until')
    return out


def check_input(query, mode: str, limit) -> str:
    text = clean_query(query)
    if mode not in MODES:
        raise SearchError('mode_invalid', f'mode must be one of {", ".join(MODES)}')
    if type(limit) is not int or not 1 <= limit <= LIMIT_MAX:
        raise SearchError('limit_out_of_range', f'limit must be 1-{LIMIT_MAX}')
    return text


def _filter_sql(filters: Mapping) -> tuple[str, list]:
    """``AND ...`` conditions on the aliases of ``store.latest_chunks_sql`` for the filters."""
    sql, params = '', []
    if filters.get('path_prefix'):
        escaped = filters['path_prefix'].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        sql += " AND d.path_key LIKE ? ESCAPE '\\'"
        params.append(escaped + '%')
    if filters.get('ext'):
        sql += f" AND d.ext IN ({','.join('?' * len(filters['ext']))})"
        params += list(filters['ext'])
    if filters.get('since'):
        sql += ' AND julianday(v.observed_at) >= julianday(?)'
        params.append(filters['since'])
    if filters.get('until'):
        sql += ' AND julianday(v.observed_at) <= julianday(?)'
        params.append(filters['until'])
    return sql, params


def _scope(filters: Mapping) -> tuple[str, list]:
    """``FROM`` body of the eligible chunks of the current chunk rules after the filters."""
    sql, params = _filter_sql(filters)
    return f'{store.latest_chunks_sql()} AND c.chunk_version = ?{sql}', [chunks.CHUNK_VERSION, *params]


# ---- lexical ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Terms:
    tokens: tuple[str, ...]     # TOKEN matches, at most MAX_TOKENS
    long: tuple[str, ...]       # >= MIN_MATCH_CHARS characters
    short: tuple[str, ...]      # shorter: never matched by trigram, used as a tie-break bonus
    match: tuple[str, ...]      # quoted FTS phrases, OR-ed
    phrases: tuple[str, ...] = ()   # the same phrases unquoted

    @property
    def snippet_terms(self) -> tuple[str, ...]:
        """What the snippet looks for, longest first in ``chunks.snippet_in_unit``: the phrases
        (the whole query first of all) and every token."""
        return tuple(dict.fromkeys((*self.phrases, *self.tokens)))

    @property
    def expression(self) -> str:
        return ' OR '.join(self.match)


def quote_phrase(text: str) -> str:
    """An FTS5 string: double quotes around, inner quotes doubled. Nothing inside is syntax."""
    return '"' + text.replace('"', '""') + '"'


def lexical_terms(query: str) -> Terms:
    """Tokens and the MATCH phrases of a cleaned query (design M2 §5.2): the whole query with its
    whitespace folded, each pair of neighbouring tokens as written between them (so ``PRJ-012``
    keeps its hyphen), and every token of at least 3 characters; phrases shorter than 3 characters
    are left out (trigram cannot match them)."""
    found = list(TOKEN.finditer(query))[:MAX_TOKENS]
    tokens = tuple(match.group() for match in found)
    phrases: list[str] = []

    def add(text: str) -> None:
        text = ' '.join(text.split())
        if len(text) >= MIN_MATCH_CHARS and text not in phrases:
            phrases.append(text)

    add(query)
    for left, right in zip(found, found[1:]):
        add(query[left.start():right.end()])
    for token in tokens:
        add(token)
    return Terms(tokens, tuple(t for t in tokens if len(t) >= MIN_MATCH_CHARS),
                 tuple(t for t in tokens if len(t) < MIN_MATCH_CHARS), tuple(quote_phrase(p) for p in phrases),
                 tuple(phrases))


def fts_usable(db: sqlite3.Connection) -> bool:
    """``chunks_fts`` exists, this SQLite can read it (FTS5 with trigram) and it is in step with the
    chunks (no ``meta.fts_dirty``, the judgement of ``store.fts_in_step``; N1, design M3 §9.1: a dirty
    index may hold old texts under reused chunk ids). Tested on ``db`` itself: no extra connection, so
    the search phase opens nothing but its read-only store."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'").fetchone() is None:
        return False
    try:
        db.execute('SELECT rowid FROM chunks_fts LIMIT 0').fetchone()
    except sqlite3.Error:
        return False
    return not fts_dirty(db)


def fts_dirty(db: sqlite3.Connection) -> bool:
    """``meta.fts_dirty`` is set: chunks changed without FTS5 since the last rebuild."""
    return store.meta_get(db, 'fts_dirty') is not None


def low_signal(signals: Mapping, rule: Mapping | None = None) -> bool:
    """``rule['rule']`` (``LOW_SIGNAL_RULES``; None: ``LOW_SIGNAL``) on the facts of ``signals``; False
    when a fact it needs is None (that mode did not produce it) or its threshold is not set."""
    rule = LOW_SIGNAL if rule is None else rule
    name = rule['rule']
    if name not in LOW_SIGNAL_RULES:
        raise ValueError('unknown low-signal rule')
    lexical, exact = signals.get('lexical_candidates'), signals.get('exact_phrase_hits')
    top1, gap = signals.get('cosine_top1'), signals.get('cosine_gap')
    r1 = top1 is not None and rule.get('cosine_top1') is not None and top1 < rule['cosine_top1']
    r2 = gap is not None and rule.get('gap') is not None and gap < rule['gap']
    r3 = lexical == 0 and exact == 0
    r5 = r3 or (r1 and exact == 0)
    return {'R1': r1, 'R2': r2, 'R3': r3, 'R4': r3 and r1, 'R5': r5}[name]


def signals_of(lexical: int | None, exact: int | None, cosines: Sequence[float],
               rule: Mapping | None = None) -> dict:
    """``SIGNAL_KEYS`` from the lexical candidate and exact-phrase counts (None: no lexical search) and
    the semantic candidates' cosines best first (empty without a model); ``low`` by ``rule``."""
    top1 = round(float(cosines[0]), 4) if len(cosines) else None
    top10 = round(float(cosines[9]), 4) if len(cosines) >= 10 else None
    gap = round(top1 - top10, 4) if top1 is not None and top10 is not None else None
    out = {'lexical_candidates': lexical, 'exact_phrase_hits': exact, 'cosine_top1': top1, 'cosine_top10': top10,
           'cosine_gap': gap}
    out['low'] = low_signal(out, rule)
    return out


def _contains_sum(column: str, count: int) -> str:
    return ' + '.join([f'(instr(lower({column}), ?) > 0)'] * count) or '0'


def _fts_candidates(db, terms: Terms, scope: tuple[str, list], top: int) -> dict[int, tuple]:
    """chunk id -> (bm25, bonus) for the best ``top`` matches (bm25 is smaller-better)."""
    body, params = scope
    short = [t.casefold() for t in terms.short]
    sql = (f'SELECT c.id, bm25(chunks_fts), {_contains_sum("c.text", len(short))} '
           f'FROM chunks_fts JOIN {body} AND chunks_fts.rowid = c.id AND chunks_fts MATCH ? '
           'ORDER BY bm25(chunks_fts), c.id LIMIT ?')
    rows = db.execute(sql, [*short, *params, terms.expression, top])
    return {chunk_id: (float(score), bonus / len(short) if short else 0.0) for chunk_id, score, bonus in rows}


def _like_candidates(db, terms: Terms, scope: tuple[str, list], top: int) -> dict[int, tuple]:
    """chunk id -> (None, contained-token count) by substring (``instr``) over the eligible chunks."""
    body, params = scope
    folded = list(dict.fromkeys(t.casefold() for t in terms.tokens))
    if not folded:
        return {}
    contained = _contains_sum('c.text', len(folded))
    sql = (f'SELECT c.id, {contained} AS n FROM {body} AND ({" OR ".join(["instr(lower(c.text), ?) > 0"] * len(folded))}) '
           'ORDER BY n DESC, c.id LIMIT ?')
    rows = db.execute(sql, [*folded, *params, *folded, top])
    return {chunk_id: (None, float(count)) for chunk_id, count in rows}


def exact_phrase_ids(db, query: str, candidates) -> set[int]:
    """Lexical candidates whose text holds the whole query verbatim (whitespace folded to one space,
    case folded as ``LIKE`` does), for queries of at least ``MIN_MATCH_CHARS`` characters.

    Hybrid ranking puts these first (design M2 stage 2): an exact occurrence of everything the user
    typed is the strongest evidence there is, and rank fusion alone can let a chunk that only
    resembles the query in meaning outrank it."""
    phrase = ' '.join(query.split()).casefold()
    ids = list(candidates)
    if len(phrase) < MIN_MATCH_CHARS or not ids:
        return set()
    found: set[int] = set()
    for start in range(0, len(ids), 500):
        block = ids[start:start + 500]
        found.update(row[0] for row in db.execute(
            f'SELECT id FROM chunks WHERE id IN ({",".join("?" * len(block))}) AND instr(lower(text), ?) > 0',
            [*block, phrase]))
    return found


def average_ranks(scores: Mapping) -> dict:
    """Fractional ranks, highest score first: n keys tied after p higher-scoring keys all get rank
    p + (n + 1) / 2, the mean of positions p+1 … p+n. Scores may be tuples (compared in order)."""
    ordered = sorted(scores.values(), reverse=True)
    first, count = {}, {}
    for position, value in enumerate(ordered, 1):
        first.setdefault(value, position)
        count[value] = count.get(value, 0) + 1
    return {key: first[value] + (count[value] - 1) / 2 for key, value in scores.items()}


def rrf(rankings: Sequence[Mapping]) -> dict:
    """Reciprocal rank fusion: key -> sum of 1 / (RRF_K + rank) over the rankings that hold it."""
    fusion: dict = {}
    for ranks in rankings:
        for key, rank in ranks.items():
            fusion[key] = fusion.get(key, 0.0) + 1.0 / (RRF_K + rank)
    return fusion


# ---- semantic -----------------------------------------------------------------------------------

def _valid_rows(block: np.ndarray) -> np.ndarray:
    """Rows that are finite and of unit norm (``index.from_blob``'s check, vectorised)."""
    finite = np.isfinite(block).all(axis=1)
    norms = np.linalg.norm(np.where(finite[:, None], block, 0.0), axis=1)
    return finite & (np.abs(norms - 1.0) <= index.NORM_TOLERANCE)


def _top(ids: np.ndarray, scores: np.ndarray, top: int) -> tuple[np.ndarray, np.ndarray]:
    """Positive scores only, best first (ties by chunk id), at most ``top``."""
    keep = scores > 0
    ids, scores = ids[keep], scores[keep]
    order = np.lexsort((ids, -scores))[:top]
    return ids[order], scores[order]


class VectorCache:
    """Vectors of the current model for one ``meta.index_generation`` (design M2 §5.4, §6.2 S1).

    In memory: a float16 matrix of the distinct texts that have a vector, and two arrays mapping
    every eligible chunk to its matrix row. Over ``max_vectors_in_memory`` texts nothing is held
    (``streamed``) and each query reads the vectors in blocks.

    The matrix belongs to ``generation`` (``meta.index_generation``: new vectors); the chunk mapping
    belongs to ``data_generation`` (``meta.data_generation``: every commit that changes which chunks are
    eligible, scans included). ``Engine`` re-maps when only the latter moved (review M2-1)."""

    def __init__(self, model_key: str, dims: int, generation: str, keys: list[str], matrix: np.ndarray,
                 chunk_ids: np.ndarray, chunk_rows: np.ndarray, *, streamed: bool, total: int, invalid: int = 0):
        self.model_key = model_key
        self.dims = dims
        self.generation = generation
        self.keys = keys
        self.matrix = matrix
        self.chunk_ids = chunk_ids
        self.chunk_rows = chunk_rows
        self.streamed = streamed
        self.total = total              # distinct texts with a vector for this model (eligible chunks)
        self.invalid_vectors = invalid
        self.data_generation: str | None = None

    @property
    def rows(self) -> int:
        return int(self.matrix.shape[0])

    @classmethod
    def load(cls, db: sqlite3.Connection, model_key: str, dims: int, settings: SearchSettings) -> VectorCache:
        """Read the vectors of eligible chunks in blocks; corrupt rows are skipped and counted."""
        generation = store.meta_get(db, 'index_generation', '0')
        body, params = _scope({})
        texts = f'SELECT c.text_sha256 FROM {body}'
        total = db.execute(f'SELECT COUNT(*) FROM chunk_vectors WHERE model_key = ? AND text_sha256 IN ({texts})',
                           [model_key, *params]).fetchone()[0]
        empty_ids = np.zeros(0, dtype=np.int64)
        if total > settings.max_vectors_in_memory:
            return cls(model_key, dims, generation, [], np.zeros((0, dims), dtype=np.float16), empty_ids,
                       np.zeros(0, dtype=np.int64), streamed=True, total=total)
        matrix = np.empty((total, dims), dtype=np.float16)
        keys: list[str] = []
        invalid = 0
        cursor = db.execute(f'SELECT text_sha256, vector FROM chunk_vectors WHERE model_key = ? '
                            f'AND text_sha256 IN ({texts}) ORDER BY text_sha256', [model_key, *params])
        while block := cursor.fetchmany(LOAD_BLOCK):
            sized = [(key, blob) for key, blob in block if isinstance(blob, bytes) and len(blob) == dims * 4]
            invalid += len(block) - len(sized)
            if not sized or len(keys) + len(sized) > total:
                continue
            values = np.frombuffer(b''.join(blob for _, blob in sized), dtype='<f4').reshape(len(sized), dims)
            good = _valid_rows(values)
            invalid += int((~good).sum())
            values = values[good]
            matrix[len(keys):len(keys) + len(values)] = values.astype(np.float16)
            keys.extend(key for (key, _), ok in zip(sized, good) if ok)
        matrix = matrix[:len(keys)] if len(keys) < total else matrix
        cache = cls(model_key, dims, generation, keys, matrix, empty_ids, np.zeros(0, dtype=np.int64),
                    streamed=False, total=total, invalid=invalid)
        cache.remap(db)
        return cache

    def remap(self, db: sqlite3.Connection) -> None:
        """Map every chunk eligible now to its matrix row (chunks whose text has no row are left out).
        Called after a load and whenever ``meta.data_generation`` moved since the last mapping."""
        self.data_generation = store.meta_get(db, 'data_generation', '0')
        if self.streamed:
            return
        body, params = _scope({})
        row_of = {key: row for row, key in enumerate(self.keys)}
        ids, rows = [], []
        for chunk_id, text_sha256 in db.execute(f'SELECT c.id, c.text_sha256 FROM {body} ORDER BY c.id', params):
            row = row_of.get(text_sha256)
            if row is not None:
                ids.append(chunk_id)
                rows.append(row)
        del row_of
        self.chunk_ids = np.array(ids, dtype=np.int64)
        self.chunk_rows = np.array(rows, dtype=np.int64)

    def expected_texts(self, ids) -> dict[int, str]:
        """``chunk id -> text_sha256`` of the matrix row each candidate was scored with (``_hits`` checks
        the chunk still has that text)."""
        where = np.searchsorted(self.chunk_ids, np.asarray(ids, dtype=np.int64))
        return {int(i): self.keys[int(self.chunk_rows[w])] for i, w in zip(ids, where)
                if w < len(self.chunk_ids) and self.chunk_ids[w] == i}

    def scores(self, query: np.ndarray) -> np.ndarray:
        """Cosine per matrix row (float32), converting ``SCORE_BLOCK`` rows at a time."""
        out = np.empty(self.rows, dtype=np.float32)
        for start in range(0, self.rows, SCORE_BLOCK):
            out[start:start + SCORE_BLOCK] = self.matrix[start:start + SCORE_BLOCK].astype(np.float32) @ query
        return out

    def candidates(self, query: np.ndarray, top: int, allowed: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Best ``top`` chunks with cosine > 0 (``allowed``: chunk ids the filters keep)."""
        per_chunk = self.scores(query)[self.chunk_rows] if self.rows else np.zeros(0, dtype=np.float32)
        ids = self.chunk_ids
        if allowed is not None:
            keep = np.isin(ids, allowed)
            ids, per_chunk = ids[keep], per_chunk[keep]
        return _top(ids, per_chunk, top)

    @staticmethod
    def stream(db: sqlite3.Connection, model_key: str, dims: int, query: np.ndarray, top: int,
               scope: tuple[str, list] | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Over-cap path: score blocks of rows straight from SQL, keep the top ``top``.

        Values are rounded through float16 exactly like the cached matrix, so both paths rank alike."""
        body, params = scope or _scope({})
        joined = body.replace(' WHERE ', ' JOIN chunk_vectors v ON v.model_key = ? '
                              'AND v.text_sha256 = c.text_sha256 WHERE ', 1)
        cursor = db.execute(f'SELECT c.id, v.vector FROM {joined}', [model_key, *params])
        best_ids, best_scores = np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float32)
        while block := cursor.fetchmany(STREAM_BLOCK):
            sized = [(chunk_id, blob) for chunk_id, blob in block if isinstance(blob, bytes) and len(blob) == dims * 4]
            if not sized:
                continue
            values = np.frombuffer(b''.join(blob for _, blob in sized), dtype='<f4').reshape(len(sized), dims)
            good = _valid_rows(values)
            ids = np.array([chunk_id for chunk_id, _ in sized], dtype=np.int64)[good]
            scored = values[good].astype(np.float16).astype(np.float32) @ query
            best_ids, best_scores = _top(np.concatenate([best_ids, ids]), np.concatenate([best_scores, scored]), top)
        return best_ids, best_scores


def embed_query(embedder, text: str, dims: int) -> np.ndarray:
    vector = np.asarray(embedder.embed([text], query=True), dtype=np.float32)
    if vector.shape != (1, dims):
        raise ValueError('query embedding has the wrong shape')
    vector = vector[0]
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm <= 0:
        raise ValueError('invalid query embedding')
    return vector / norm


# ---- hits ---------------------------------------------------------------------------------------

def _hits(db: sqlite3.Connection, ranked: Sequence[tuple[int, dict]], terms: Sequence[str],
          expected: Mapping[int, str] | None = None, *, limit: int | None = None, collapse: bool = False,
          counts: dict | None = None) -> list[dict]:
    """Hit dicts (``HIT_KEYS``) for ``ranked`` ``(chunk_id, scores)``: snippet and locator inside one
    unit, ``units[]`` with the chunk's range in each unit (at most ``UNITS_CAP``; ``snippet.cover``
    computes the ranges, the same function ``get_snippet`` uses).

    A chunk is returned only if it is still eligible now (latest active version not in progress on the
    operating server, current chunk rules; ``store.latest_chunks_sql``, review M6 v1 M6-5) and, when ``expected`` names a text hash for it, still holds that text (review M2-1: a candidate
    from an older mapping, or a chunk id reused since, never becomes a hit).

    ``collapse`` (design M3 §2.2): only the first hit of each document; the hits skipped for that are
    counted in ``counts['collapsed']``. Stops at ``limit`` hits (None: all of ``ranked``)."""
    if not ranked:
        return []
    ids = [chunk_id for chunk_id, _ in ranked]
    rows = db.execute('SELECT c.id, c.version_id, c.unit_first, c.unit_last, c.char_start, c.char_end, '
                      'v.sha256, v.observed_at, d.id, d.path, d.ext, c.text_sha256 FROM chunks c '
                      'JOIN versions v ON v.id = c.version_id JOIN documents d ON d.id = v.document_id '
                      f'WHERE c.id IN ({",".join("?" * len(ids))}) AND {store.ELIGIBLE_VERSIONS} '
                      f'AND v.id NOT IN ({store.IN_PROGRESS_SQL}) '      # the same rule as the candidates (M6-5)
                      'AND c.chunk_version = ?', [*ids, chunks.CHUNK_VERSION]).fetchall()
    by_id = {row[0]: row for row in rows}
    expected = expected or {}
    out: list[dict] = []
    seen_docs: set[int] = set()
    skipped = 0
    for chunk_id, scores in ranked:
        if limit is not None and len(out) >= limit:
            break
        if chunk_id not in by_id:
            continue  # no longer eligible (scan or index since the candidates were taken)
        (_, version_id, first, last, char_start, char_end, sha256, observed_at, doc_id, path, ext,
         text_sha256) = by_id[chunk_id]
        if expected.get(chunk_id, text_sha256) != text_sha256:
            continue  # the id now holds another text than the one scored
        if collapse and doc_id in seen_docs:
            skipped += 1
            continue
        units = db.execute('SELECT ordinal, locator_json, text FROM units WHERE version_id = ? '
                           'AND ordinal BETWEEN ? AND ? ORDER BY ordinal', (version_id, first, last)).fetchall()
        covered = snippet.cover(units, char_start, char_end)
        if not covered:
            continue
        seen_docs.add(doc_id)
        ordinal, start, end = chunks.snippet_in_unit(covered, list(terms))
        unit, text = next((u, t) for o, u, t, _ in covered if o == ordinal)
        hit = {'rank': len(out) + 1, 'chunk_id': chunk_id, 'version_id': version_id,
               'doc': {'id': doc_id, 'path': path, 'ext': ext, 'sha256': sha256, 'observed_at': observed_at},
               'snippet': text[start:end],
               'locator': locator.make(doc_id, path, sha256, unit, text, [start, end]),
               'units': [locator.make(doc_id, path, sha256, u, t, list(r)) for _, u, t, r in covered[:UNITS_CAP]],
               'scores': scores}
        if len(covered) > UNITS_CAP:
            hit['units_truncated'] = len(covered) - UNITS_CAP
        out.append(hit)
    if counts is not None:
        counts['collapsed'] = skipped
    return out


# ---- search -------------------------------------------------------------------------------------

def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


def search(db: sqlite3.Connection, query: str, *, embedder=None, vectors: VectorCache | None = None,
           settings: SearchSettings = SearchSettings(), mode: str = 'hybrid', limit: int = LIMIT_DEFAULT,
           path_prefix: str | None = None, ext: Sequence[str] | None = None, since: str | None = None,
           until: str | None = None, semantic_note: str | None = None, collapse_by_document: bool = False) -> dict:
    """One search on a read-only connection; returns ``{'hits', 'meta', 'notice'}`` (``RESULT_KEYS``).

    ``collapse_by_document`` (design M3 §2.2): walk the whole fused order and keep the first hit of
    each document until ``limit`` hits; ranks are renumbered from 1, scores are unchanged,
    ``meta.collapsed`` counts the hits skipped. ``meta.signals`` (design M3 §9.3): ``signals_of``; a
    ``low`` result with hits adds ``LOW_SIGNAL_NOTICE``. ``chunks_fts`` out of step (``fts_dirty``,
    N1) means the ``LIKE`` path and ``FTS_DIRTY_NOTICE``.

    ``embedder``/``vectors`` None means lexical only (``semantic_note`` says why); with an embedder
    and no ``vectors`` the cache is loaded here. Raises ``SearchError`` for bad input. Pure apart
    from reading ``db``.
    """
    began = time.perf_counter()
    text = check_input(query, mode, limit)
    filters = check_filters(path_prefix, ext, since, until)
    terms = lexical_terms(text)
    scope = _scope(filters)
    notice: list[str] = []
    elapsed = {'lexical': 0.0, 'embed_query': 0.0, 'semantic': 0.0, 'total': 0.0}
    requested = mode
    if mode != 'lexical' and embedder is None:
        mode = 'lexical'
    eligible = db.execute(f'SELECT COUNT(*) FROM {scope[0]}', scope[1]).fetchone()[0]

    # Lexical candidates.
    lexical: dict[int, tuple] = {}
    engine = 'none'
    if mode != 'semantic':
        started = time.perf_counter()
        fts = fts_usable(db)
        dirty = not fts and fts_dirty(db)
        if dirty:
            notice.append(FTS_DIRTY_NOTICE)
        if fts and terms.match:
            engine = 'fts5'
            lexical = _fts_candidates(db, terms, scope, settings.lexical_candidates)
        use_like = (not fts) or not terms.match or (not lexical and terms.short)
        if use_like and terms.tokens:
            if eligible <= settings.like_max_chunks:
                engine = 'like'
                lexical = _like_candidates(db, terms, scope, settings.lexical_candidates)
            elif not dirty:
                notice.append(SHORT_QUERY_NOTICE)
        elif not terms.tokens and not terms.match:
            notice.append(SHORT_QUERY_NOTICE)
        elapsed['lexical'] = _ms(time.perf_counter() - started)

    # Semantic candidates.
    cosine: dict[int, float] = {}
    best_cosines: list[float] = []      # the first 10 semantic candidates' cosines, best first (signals)
    expected: dict[int, str] = {}
    semantic_meta = NOT_REQUESTED if requested == 'lexical' else UNAVAILABLE
    if mode != 'lexical':
        identity = embedder.identity
        dims = int(identity['dims'])
        key = index.model_key(identity)
        if vectors is None or vectors.model_key != key:
            vectors = VectorCache.load(db, key, dims, settings)
        started = time.perf_counter()
        query_vector = embed_query(embedder, text, dims)
        elapsed['embed_query'] = _ms(time.perf_counter() - started)
        started = time.perf_counter()
        if vectors.streamed:
            ids, scores = VectorCache.stream(db, key, dims, query_vector, settings.semantic_candidates, scope)
        else:
            allowed = None
            if any(filters.values()):
                allowed = np.array([row[0] for row in db.execute(f'SELECT c.id FROM {scope[0]}', scope[1])],
                                   dtype=np.int64)
            ids, scores = vectors.candidates(query_vector, settings.semantic_candidates, allowed)
            expected = vectors.expected_texts(ids)
        cosine = {int(i): float(s) for i, s in zip(ids, scores)}
        best_cosines = [float(s) for s in scores[:10]]
        elapsed['semantic'] = _ms(time.perf_counter() - started)
        semantic_meta = {'model': identity.get('name'), 'model_key': key,
                         'real_model': bool(identity.get('real_model', True)), 'vectors': vectors.total,
                         'candidates': len(cosine), 'streamed': vectors.streamed}
        if vectors.total == 0 and semantic_note is None:
            semantic_note = NO_VECTORS_NOTE

    # Ranks, fusion, order.
    if engine == 'fts5':
        lexical_key = {k: (-bm25, bonus) for k, (bm25, bonus) in lexical.items()}
    else:
        lexical_key = {k: (count, 0.0) for k, (_, count) in lexical.items()}
    lexical_ranks = average_ranks(lexical_key)
    semantic_ranks = average_ranks(cosine)
    fusion = rrf([lexical_ranks, semantic_ranks])
    holding = exact_phrase_ids(db, text, lexical) if mode != 'semantic' else set()
    exact = holding if mode == 'hybrid' else set()     # ordering and scores: hybrid only (M2 contract)
    if mode == 'lexical':
        order = sorted(lexical, key=lambda k: (tuple(-x for x in lexical_key[k]), k))
    elif mode == 'semantic':
        order = sorted(cosine, key=lambda k: (-cosine[k], k))
    else:
        order = sorted(fusion, key=lambda k: (k not in exact, -fusion[k],
                                              tuple(-x for x in lexical_key.get(k, (float('-inf'),) * 2)),
                                              -cosine.get(k, float('-inf')), k))
    ranked = []
    for k in (order if collapse_by_document else order[:limit]):
        in_lex, in_sem = k in lexical_ranks, k in semantic_ranks
        ranked.append((k, {
            'bm25': lexical[k][0] if in_lex else None,
            'lexical_rank': lexical_ranks.get(k), 'cosine': cosine.get(k), 'semantic_rank': semantic_ranks.get(k),
            'fusion': fusion.get(k, 0.0), 'exact_phrase': k in exact,
            'hit': 'both' if in_lex and in_sem else 'lexical' if in_lex else 'semantic',
            'score_is_probability': False}))
    counts = {'collapsed': 0}
    hits = _hits(db, ranked, terms.snippet_terms, expected, limit=limit, collapse=collapse_by_document,
                 counts=counts)
    lexical_ran = mode != 'semantic'
    signals = signals_of(len(lexical) if lexical_ran else None, len(holding) if lexical_ran else None,
                         best_cosines)
    if not hits:
        notice.append(NO_RESULTS_NOTICE)
    elif signals['low']:
        notice.append(LOW_SIGNAL_NOTICE.format(
            lexical='-' if signals['lexical_candidates'] is None else signals['lexical_candidates'],
            top1=_number(signals['cosine_top1'], 2)))
    if filters['since'] or filters['until']:
        notice.append(OBSERVED_NOTICE)
    elapsed['total'] = _ms(time.perf_counter() - began)
    meta = {'mode': mode, 'search_version': SEARCH_VERSION,
            'lexical': {'engine': engine,
                        'terms': {'match': list(terms.match) if engine == 'fts5' else [], 'short': list(terms.short)},
                        'candidates': len(lexical)},
            'semantic': semantic_meta, 'semantic_note': semantic_note, 'eligible_chunks': eligible,
            'filters': filters, 'elapsed_ms': elapsed, 'collapsed': counts['collapsed'], 'signals': signals}
    return {'hits': hits, 'meta': meta, 'notice': notice}


# ---- engine -------------------------------------------------------------------------------------

def load_failure_code(exc: BaseException) -> str:
    """Fixed reason codes of a failed model load (design M2 §3.3)."""
    if isinstance(exc, ImportError):
        return f'runtime_import_failed:{type(exc).__name__}'
    if isinstance(exc, models.ModelFileError):
        return f'model_unverified({str(exc)[:200]})'
    return f'runtime_error:{type(exc).__name__}'


class Engine:
    """Per-process search state: lazy model, vector cache, idle release (design M2 §5.6).

    ``embedder_factory``: a callable returning an ``Embedder`` (tests pass ``HashEmbedder``); None
    means the manifest model through ``embedder.OnnxEmbedder`` with ``settings.effective_threads()``
    intra-op threads. A load failure is recorded once per process (``model_missing: ...``,
    ``model_unverified(<file>: <status>)``, ``runtime_import_failed:<Class>``,
    ``runtime_error:<Class>``) and later searches only report it. No thread lock: the MCP servers call
    tools from one thread and the CLI makes one call; the one other thread is ``preload_in_background``'s
    load, and while ``loading`` is True nothing else loads or reads the model (``embedder()`` answers None).
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
        self.loads = 0                  # model loads attempted in this process
        self.last_load_ms: float | None = None   # duration of the last load attempt (design M3 §6.3)
        self.last_verify_ms: float | None = None  # of which the SHA-256 check of the model files
        self.loading = False            # True while preload_in_background's thread loads the model

    def search(self, query: str, *, mode: str = 'hybrid', limit: int = LIMIT_DEFAULT,
               path_prefix: str | None = None, ext: Sequence[str] | None = None, since: str | None = None,
               until: str | None = None, collapse_by_document: bool = False) -> dict:
        """Open ``store.open_read(home)``, run ``search(...)``, close; returns ``RESULT_KEYS``.
        Input is checked before the model is touched; lexical mode never loads it."""
        check_input(query, mode, limit)
        check_filters(path_prefix, ext, since, until)
        db = store.open_read(self.home)
        try:
            model = self.embedder() if mode != 'lexical' else None
            vectors = self.vectors(db) if model is not None else None
            note = None
            if mode != 'lexical' and model is None:   # None without a failure: the background load runs
                note = self.load_failure or MODEL_LOADING_NOTE
            result = search(db, query, embedder=model, vectors=vectors, settings=self.settings, mode=mode,
                            limit=limit, path_prefix=path_prefix, ext=ext, since=since, until=until,
                            semantic_note=note, collapse_by_document=collapse_by_document)
        finally:
            db.close()
        if model is not None:
            self.last_used = self.clock()
        return result

    def embedder(self):
        """The loaded embedder or None (lazy; failure recorded once; updates ``last_used``). None, without
        waiting, while ``preload_in_background`` loads (``loading`` is read first: once it is False the thread
        has set ``_embedder`` or ``load_failure``)."""
        if self.loading:
            return None
        if self._embedder is not None:
            self.last_used = self.clock()
            return self._embedder
        if self.load_failure is not None:
            return None
        return self._load()

    def preload_in_background(self, done: Callable[[], None] | None = None) -> bool:
        """Start the model load on a daemon thread (the operating server's ``[server] model_preload``) and
        return True; False when a model is loaded, a failure is recorded or a load runs. ``done()`` is called
        on that thread after the load, whatever its result (``status()`` tells which)."""
        if self.loading or self._embedder is not None or self.load_failure is not None:
            return False
        self.loading = True

        def run() -> None:
            try:
                self._load()
            finally:
                self.loading = False
                if done is not None:
                    done()

        threading.Thread(target=run, name='workmcp-model-load', daemon=True).start()
        return True

    def _load(self):
        """One load attempt: the factory, or the manifest model after its SHA-256 check (``last_verify_ms``)."""
        self.loads += 1
        started = time.perf_counter()
        try:
            if self.embedder_factory is not None:
                self._embedder = self.embedder_factory()
            else:
                status = models.quick_status(models.DEFAULT_MODEL, self.home)
                if not status['available']:
                    self.load_failure = f"model_missing: {status['message']}"
                    return None
                verify_started = time.perf_counter()
                models.require_verified(models.DEFAULT_MODEL, self.home)   # every file's pinned SHA-256
                self.last_verify_ms = _ms(time.perf_counter() - verify_started)
                self._embedder = embedders.OnnxEmbedder(models.DEFAULT_MODEL, self.home, verify=False,
                                                        threads=self.settings.effective_threads())
        except Exception as exc:  # noqa: BLE001 - any load failure means lexical search, recorded once
            self.load_failure = load_failure_code(exc)
            self._embedder = None
            return None
        finally:
            self.last_load_ms = _ms(time.perf_counter() - started)
        self.last_used = self.clock()
        return self._embedder

    def vectors(self, db: sqlite3.Connection) -> VectorCache | None:
        """Vector cache for the current store: the matrix is reloaded when ``meta.index_generation`` (or
        the model) changes, the chunk mapping when ``meta.data_generation`` does (a scan, or an index
        run that stopped before it finished) — a long-lived Engine never maps to chunks that are no
        longer eligible (review M2-1)."""
        model = self.embedder()
        if model is None:
            return None
        key = index.model_key(model.identity)
        generation = store.meta_get(db, 'index_generation', '0')
        cache = self._vectors
        if cache is None or cache.generation != generation or cache.model_key != key:
            self._vectors = None
            self._vectors = VectorCache.load(db, key, int(model.identity['dims']), self.settings)
        elif cache.data_generation != store.meta_get(db, 'data_generation', '0'):
            cache.remap(db)
        return self._vectors

    def maybe_unload(self, now: float | None = None) -> bool:
        """Release model and matrix when unused for ``settings.model_idle_seconds``; True if released."""
        if self.loading or (self._embedder is None and self._vectors is None):
            return False
        now = self.clock() if now is None else now
        if self.last_used is not None and now - self.last_used < self.settings.model_idle_seconds:
            return False
        self.unload()
        return True

    def unload(self) -> None:
        self._embedder = None
        self._vectors = None
        gc.collect()

    def reset(self) -> None:
        """Forget a recorded load failure so the next search tries the model again."""
        self.unload()
        self.load_failure = None

    def status(self) -> dict:
        """``ENGINE_STATUS_KEYS`` (no text): model loaded, matrix rows and bytes, the index and data
        generations the cache belongs to, seconds since the model was last used (None: never), load
        attempts and the last one's duration in ms (of which the SHA-256 check), the recorded load failure,
        whether the background load runs."""
        cache = self._vectors
        return {'loaded': self._embedder is not None, 'rows': cache.rows if cache else 0,
                'generation': cache.generation if cache else None,
                'data_generation': cache.data_generation if cache else None,
                'matrix_bytes': int(cache.matrix.nbytes) if cache else 0, 'load_failure': self.load_failure,
                'idle_seconds': None if self.last_used is None else round(self.clock() - self.last_used, 1),
                'loads': self.loads, 'last_load_ms': self.last_load_ms, 'loading': self.loading,
                'last_verify_ms': self.last_verify_ms}


# ---- console output -----------------------------------------------------------------------------

def _number(value, digits: int) -> str:
    return '-' if value is None else f'{value:.{digits}f}'


def format_hits(result: dict) -> list[str]:
    """Lines for the ``workmcp search`` console output: rank, scores, one source line
    (``locator.display``) and the snippet per hit, then the notices."""
    meta = result['meta']
    semantic = meta['semantic']
    semantic_text = (f"{semantic['model']} ({semantic['candidates']} candidates"
                     f"{', streamed' if semantic['streamed'] else ''})" if isinstance(semantic, dict) else semantic)
    lines = [f"{len(result['hits'])} hits  mode {meta['mode']}  lexical {meta['lexical']['engine']} "
             f"({meta['lexical']['candidates']} candidates)  semantic {semantic_text}  "
             f"chunks {meta['eligible_chunks']}  {meta['elapsed_ms']['total']} ms"]
    if meta['semantic_note']:
        lines.append(f"semantic: {meta['semantic_note']}")
    for hit in result['hits']:
        scores = hit['scores']
        tag = scores['hit'] + (', exact' if scores.get('exact_phrase') else '')
        lines.append(f"{hit['rank']:>2}. [{tag}] fusion {_number(scores['fusion'], 4)}  "
                     f"bm25 {_number(scores['bm25'], 2)}  cosine {_number(scores['cosine'], 3)}")
        lines.append(f"    {locator.display(hit['locator'])}")
        more = f" (+{hit['units_truncated']} more)" if hit.get('units_truncated') else ''
        lines.append(f"    units {len(hit['units'])}{more}")
        lines.append('    ' + ' '.join(hit['snippet'].split()))
    lines += [f'notice: {item}' for item in result['notice']]
    return lines

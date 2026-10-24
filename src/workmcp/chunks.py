"""Unit-based chunk rules (design M2 §2). Standard library only: ``binding`` imports this module.

A chunk is a run of whole units of one version, joined with ``'\\n'``, that never crosses a page,
sheet, slide, mail part or section boundary (``group_key``); a unit longer than ``CHUNK_MAX`` is
cut at sentence boundaries into pieces that cover it without gaps. ``char_start``/``char_end``
index the joined text of units ``unit_first..unit_last``, so
``'\\n'.join(unit texts)[char_start:char_end] == text`` (the binding check re-derives it). The
lengths are characters (``len(str)``), not tokens, so the chunks do not depend on the model.

Every chunk is at most ``CHUNK_MAX`` characters: a carried overlap that would push a chunk past it
is shortened from its front (dropped when needed). Sentence boundaries are those of
``locator.sentences``, found in one streaming pass (``_sentence_starts``) so a unit of millions of
characters is chunked in memory linear in its output. Changing a constant or the algorithm means a
new ``CHUNK_VERSION``.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from . import locator

CHUNK_TARGET = 700      # characters; Korean ~1.79 chars/token measured -> ~391 tokens
CHUNK_MAX = 900         # ~503 tokens <= 512; a longer unit is split at sentences
CHUNK_OVERLAP = 140     # at most this many characters of whole trailing units carried forward
SNIPPET_MAX = 600       # design M0 §4.2
CHUNK_VERSION = 'units-700-900-140-v1'

# The pieces of locator.py the rules depend on; a change there means a new CHUNK_VERSION too.
COORD_VERSION = locator.COORD_VERSION

_SEPARATOR = re.compile('[\\s​]')  # what locator._WORD splits on (whitespace, zero-width space)
HARD_CUT_WINDOW = 100   # a sentence over CHUNK_MAX is cut at the last word start in [TARGET-100, TARGET]
SNIPPET_BEFORE = 200    # snippet window [m - 200, m + 400) around the first match
SNIPPET_AFTER = 400

# (ordinal, unit dict, unit text), in ordinal order: what build_chunks reads for one version.
UnitRow = tuple[int, dict, str]


@dataclass(frozen=True, slots=True)
class ChunkSpec:
    """One chunk of a version, as ``store.insert_chunks`` stores it."""
    ordinal: int            # 1-based order within the version
    unit_first: int         # units.ordinal, 1-based, inclusive
    unit_last: int
    char_start: int         # [start, end) in '\n'.join(texts of unit_first..unit_last)
    char_end: int
    text: str               # == that slice

    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode('utf-8')).hexdigest()


def group_key(unit: dict) -> tuple:
    """No chunk crosses a change of this key (design M2 §2.2)."""
    kind = unit['kind']
    if kind == 'page':
        return ('page', unit['page'])
    if kind == 'sheet_cell':
        return ('sheet', unit['sheet'])
    if kind in ('slide', 'slide_cell'):
        return ('slide', unit['slide'])
    if kind == 'mail':
        return ('part', unit['part'])
    if kind == 'mail_header':
        return ('header',)
    if kind == 'section_para':
        return ('section', unit['section'])
    return ('body',)  # paragraph (tables included), line, cell




def _joined_length(parts: Sequence[tuple[int, str]]) -> int:
    return sum(len(text) for _, text in parts) + max(len(parts) - 1, 0)


def build_chunks(units: Sequence[UnitRow]) -> list[ChunkSpec]:
    """Chunks of one version from its units in ordinal order. Pure and deterministic (design M2 §2.2).

    Whole units are grouped up to ``CHUNK_TARGET`` characters inside one ``group_key``; the next
    chunk starts with the trailing whole units of the previous one whose lengths sum to at most
    ``CHUNK_OVERLAP`` (never all of them; no overlap across a group change; shortened from its front
    when it would push the chunk past ``CHUNK_MAX``). Such chunks have ``char_start == 0`` and
    ``char_end == len(text)``. A unit longer than ``CHUNK_MAX`` is chunked alone by
    ``split_large_unit``.
    """
    out: list[ChunkSpec] = []
    current: list[tuple[int, str]] = []
    previous_key = None

    def flush():
        if current:
            joined = '\n'.join(text for _, text in current)
            out.append(ChunkSpec(len(out) + 1, current[0][0], current[-1][0], 0, len(joined), joined))

    for ordinal, unit, text in units:
        key = group_key(unit)
        if len(text) > CHUNK_MAX:
            flush()
            current = []
            out.extend(split_large_unit(ordinal, text, len(out) + 1))
            previous_key = key
            continue
        if current and (key != previous_key or _joined_length(current) + 1 + len(text) > CHUNK_TARGET):
            flush()
            current = carry(current) if key == previous_key else []
            while current and _joined_length(current) + 1 + len(text) > CHUNK_MAX:
                current = current[1:]
        current.append((ordinal, text))
        previous_key = key
    flush()
    return out


def carry(current: Sequence[tuple[int, str]]) -> list[tuple[int, str]]:
    """Longest suffix of ``(ordinal, text)`` pairs whose joined length is <= ``CHUNK_OVERLAP`` and
    that is shorter than ``current`` (empty when the last unit alone is longer)."""
    taken, size = 0, -1
    for _, text in reversed(current[1:]):
        if size + 1 + len(text) > CHUNK_OVERLAP:
            break
        size += 1 + len(text)
        taken += 1
    return list(current[len(current) - taken:]) if taken else []


def _sentence_starts(text: str) -> Iterator[int]:
    """Start offsets of the sentences of ``locator.sentences(text)``, in one streaming pass.

    Same rule (a word ending in terminal punctuation, the last word, or a word followed by a line
    break ends a sentence) without building the word list, so memory does not grow with the text."""
    previous = None
    for match in locator._WORD.finditer(text):
        if previous is None:
            yield match.start()
        elif (locator._SENTENCE_END.search(text, previous.start(), previous.end())
              or locator._LINE_BREAK.search(text, previous.end(), match.start())):
            yield match.start()
        previous = match


def _is_word_start(text: str, at: int) -> bool:
    return 0 < at < len(text) and bool(_SEPARATOR.match(text, at - 1)) and not _SEPARATOR.match(text, at)


def _hard_cut(text: str, start: int, end: int) -> Iterator[tuple[int, int]]:
    """``[start, end)`` as is, or cut every ``CHUNK_TARGET`` characters at the last word start in
    ``[CHUNK_TARGET - HARD_CUT_WINDOW, CHUNK_TARGET]`` (exactly ``CHUNK_TARGET`` without one) while
    longer than ``CHUNK_MAX``."""
    while end - start > CHUNK_MAX:
        cut = start + CHUNK_TARGET
        for at in range(start + CHUNK_TARGET, start + CHUNK_TARGET - HARD_CUT_WINDOW - 1, -1):
            if _is_word_start(text, at):
                cut = at
                break
        yield start, cut
        start = cut
    yield start, end


def _atoms(text: str) -> Iterator[tuple[int, int]]:
    """Sentence spans covering ``text`` without gaps (the gap after a sentence belongs to it, the
    first starts at 0), each at most ``CHUNK_MAX`` long after ``_hard_cut``."""
    begin = None
    for start in _sentence_starts(text):
        if begin is None:
            begin = 0
            continue
        yield from _hard_cut(text, begin, start)
        begin = start
    yield from _hard_cut(text, begin or 0, len(text))


def split_large_unit(ordinal: int, text: str, start_ordinal: int) -> list[ChunkSpec]:
    """Pieces of one unit longer than ``CHUNK_MAX`` (design M2 §2.3): sentence spans of
    ``locator.sentences`` with the gap after a sentence attached to it, so the pieces cover ``text``
    without gaps; sentences gathered up to ``CHUNK_TARGET``; a sentence over ``CHUNK_MAX`` hard-cut
    every ``CHUNK_TARGET`` at the last word boundary in ``[CHUNK_TARGET - 100, CHUNK_TARGET]``; the
    previous piece's last sentence (<= ``CHUNK_OVERLAP``, not the whole piece, and only when the
    next piece stays <= ``CHUNK_MAX``) repeated at the head of the next. Every piece is
    ``ChunkSpec(n, ordinal, ordinal, s, e, text[s:e])`` numbered from ``start_ordinal``."""
    out: list[ChunkSpec] = []

    def emit(start, end):
        out.append(ChunkSpec(start_ordinal + len(out), ordinal, ordinal, start, end, text[start:end]))

    piece_start = last = None
    for start, end in _atoms(text):
        if piece_start is None:
            piece_start, last = start, (start, end)
            continue
        if end - piece_start <= CHUNK_TARGET:
            last = (start, end)
            continue
        emit(piece_start, last[1])
        last_start, last_end = last
        overlap = (last_start > piece_start and last_end - last_start <= CHUNK_OVERLAP
                   and end - last_start <= CHUNK_MAX)
        piece_start = last_start if overlap else start
        last = (start, end)
    if piece_start is not None:
        emit(piece_start, last[1])
    return out


def _pull_to_words(text: str, start: int, end: int, low: int, high: int) -> tuple[int, int]:
    """``[start, end)`` pulled inward so it neither starts nor ends inside a word (ends on the chunk
    range ``[low, high)`` stay) and without separators at its ends; the window itself when nothing
    would be left."""
    s, e = start, end
    if s > low and _SEPARATOR.match(text, s - 1) is None:
        while s < e and _SEPARATOR.match(text, s) is None:
            s += 1
    if e < high and _SEPARATOR.match(text, e) is None:
        while e > s and _SEPARATOR.match(text, e - 1) is None:
            e -= 1
    while s < e and _SEPARATOR.match(text, s):
        s += 1
    while e > s and _SEPARATOR.match(text, e - 1):
        e -= 1
    return (s, e) if s < e else (start, end)


def snippet_in_unit(units: Sequence[tuple[int, dict, str, tuple[int, int]]], terms: Sequence[str],
                    limit: int = SNIPPET_MAX) -> tuple[int, int, int]:
    """Snippet of a chunk, always inside one unit (design M2 §2.6).

    ``units``: ``(ordinal, unit dict, unit text, (cs, ce))`` for every unit the chunk covers, where
    ``[cs, ce)`` is the chunk's range inside that unit. ``terms``: query tokens. Returns
    ``(ordinal, start, end)`` in that unit's text: the unit where the longest term first appears
    (case-insensitive), else the first unit; window ``[m - 200, m + 400)`` clipped to ``[cs, ce)``
    and ``limit``, or the first ``limit`` characters of the range without a match; both ends pulled
    in to word boundaries (a range of at most ``limit`` characters is returned whole). The caller
    builds the locator with ``locator.make``.
    """
    if not units:
        raise ValueError('a chunk covers at least one unit')
    chosen, match_at = units[0], None
    for term in sorted((t for t in terms if t), key=len, reverse=True):
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        for unit in units:
            found = pattern.search(unit[2], unit[3][0], unit[3][1])
            if found:
                chosen, match_at = unit, found.start()
                break
        if match_at is not None:
            break
    ordinal, _, text, (low, high) = chosen
    if high - low <= limit:
        return ordinal, low, high
    if match_at is None:
        start, end = low, low + limit
    else:
        start = max(low, match_at - SNIPPET_BEFORE)
        end = min(high, match_at + SNIPPET_AFTER, start + limit)
    return (ordinal, *_pull_to_words(text, start, end, low, high))

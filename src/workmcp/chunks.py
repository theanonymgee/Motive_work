"""Unit-based chunk rules (design M2 §2). Standard library only: ``binding`` imports this module.

A chunk is a run of whole units of one version, joined with ``'\\n'``, that never crosses a page,
sheet, slide, mail part or section boundary (``group_key``); a unit longer than ``CHUNK_MAX`` is
cut at sentence boundaries into pieces that cover it without gaps. ``char_start``/``char_end``
index the joined text of units ``unit_first..unit_last``, so
``'\\n'.join(unit texts)[char_start:char_end] == text`` (the binding check re-derives it). The
lengths are characters (``len(str)``), not tokens, so the chunks do not depend on the model.

Status (M2 stage 0): constants, ``ChunkSpec`` and ``group_key`` are fixed; ``build_chunks``,
``split_large_unit``, ``carry`` and ``snippet_in_unit`` are signatures to be filled in stage 1.
Changing a constant or the algorithm means a new ``CHUNK_VERSION``.
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from . import locator

CHUNK_TARGET = 700      # characters; Korean ~1.79 chars/token measured -> ~391 tokens
CHUNK_MAX = 900         # ~503 tokens <= 512; a longer unit is split at sentences
CHUNK_OVERLAP = 140     # at most this many characters of whole trailing units carried forward
SNIPPET_MAX = 600       # design M0 §4.2
CHUNK_VERSION = 'units-700-900-140-v1'

# The pieces of locator.py the rules depend on; a change there means a new CHUNK_VERSION too.
COORD_VERSION = locator.COORD_VERSION

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


def build_chunks(units: Sequence[UnitRow]) -> list[ChunkSpec]:
    """Chunks of one version from its units in ordinal order. Pure and deterministic (design M2 §2.2).

    Whole units are grouped up to ``CHUNK_TARGET`` characters inside one ``group_key``; the next
    chunk starts with the trailing whole units of the previous one whose lengths sum to at most
    ``CHUNK_OVERLAP`` (never all of them; no overlap across a group change). Such chunks have
    ``char_start == 0`` and ``char_end == len(text)``. A unit longer than ``CHUNK_MAX`` is chunked
    alone by ``split_large_unit``.
    """
    raise NotImplementedError('chunks.build_chunks: M2 stage 1')


def carry(current: Sequence[tuple[int, str]]) -> list[tuple[int, str]]:
    """Longest suffix of ``(ordinal, text)`` pairs with total length <= ``CHUNK_OVERLAP`` that is
    shorter than ``current`` (empty when the last unit alone is longer)."""
    raise NotImplementedError('chunks.carry: M2 stage 1')


def split_large_unit(ordinal: int, text: str, start_ordinal: int) -> list[ChunkSpec]:
    """Pieces of one unit longer than ``CHUNK_MAX`` (design M2 §2.3): sentence spans from
    ``locator.sentences``/``anchor`` with the gap after a sentence attached to it, so the pieces
    cover ``text`` without gaps; sentences gathered up to ``CHUNK_TARGET``; a sentence over
    ``CHUNK_MAX`` hard-cut every ``CHUNK_TARGET`` at the last word boundary in
    ``[CHUNK_TARGET - 100, CHUNK_TARGET]``; the previous piece's last sentence (<= ``CHUNK_OVERLAP``)
    repeated at the head of the next. Every piece is ``ChunkSpec(n, ordinal, ordinal, s, e, text[s:e])``
    numbered from ``start_ordinal``."""
    raise NotImplementedError('chunks.split_large_unit: M2 stage 1')


def snippet_in_unit(units: Sequence[tuple[int, dict, str, tuple[int, int]]], terms: Sequence[str],
                    limit: int = SNIPPET_MAX) -> tuple[int, int, int]:
    """Snippet of a chunk, always inside one unit (design M2 §2.6).

    ``units``: ``(ordinal, unit dict, unit text, (cs, ce))`` for every unit the chunk covers, where
    ``[cs, ce)`` is the chunk's range inside that unit. ``terms``: query tokens. Returns
    ``(ordinal, start, end)`` in that unit's text: the unit where the longest term first appears
    (case-insensitive), else the first unit; window ``[m - 200, m + 400)`` clipped to ``[cs, ce)``
    and ``limit``, or the first ``limit`` characters of the range without a match; both ends pulled
    in to word boundaries. The caller builds the locator with ``locator.make``.
    """
    raise NotImplementedError('chunks.snippet_in_unit: M2 stage 1')

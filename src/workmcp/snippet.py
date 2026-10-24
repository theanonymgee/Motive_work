"""``get_snippet`` core: stored units of a chunk or a locator as pieces with exact locators (design M3 §2.3).

A piece is ``{'locator', 'text', 'truncated'}`` (``PIECE_KEYS``) where ``text`` is exactly the text the
locator names (``binding.validate_locator`` gives it back). Pieces come from the stored units of the
version (never the chunk text joined across units, design M2 §2.5); context pieces are whole units
before and after (``chars = [0, len]``). A piece over ``max_chars`` is cut at the last word boundary
inside the limit and its locator is rebuilt for the cut range.

Stage 0 (design M3 §14.1) fixes the signatures below; stage 1 (B) implements them. ``search._hits``
and ``by_chunk`` will share ``cover``.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence

PIECE_MAX_CHARS = 4_000         # one piece's text (design M3 §4); tools.PIECE_MAX_CHARS is the same
PIECE_KEYS = frozenset({'locator', 'text', 'truncated'})
RESULT_KEYS = frozenset({'doc', 'version', 'chunk', 'pieces', 'context_before', 'context_after'})
DOC_KEYS = frozenset({'id', 'path', 'ext'})
VERSION_KEYS = frozenset({'id', 'seq', 'sha256', 'observed_at', 'latest', 'latest_version_id'})
CHUNK_KEYS = frozenset({'id', 'ordinal', 'unit_first', 'unit_last', 'chunk_version'})

Covered = tuple[int, dict, str, tuple[int, int]]  # (ordinal, unit dict, unit text, (cs, ce))


class NotFound(LookupError):
    """No such chunk, version or unit; the message names ids, sha12 and unit labels only (no text)."""


def cover(units_rows: Sequence[tuple[int, str, str]], char_start: int, char_end: int) -> list[Covered]:
    """``(ordinal, unit, text, (cs, ce))`` for each stored unit row ``(ordinal, locator_json, text)`` (in
    ordinal order, units joined by one separator character) that the chunk range ``[char_start,
    char_end)`` covers, with the range inside that unit; the same computation as ``search._hits``."""
    raise NotImplementedError('snippet.cover: stage 1 (B)')


def by_chunk(db: sqlite3.Connection, chunk_id: int, *, context_units: int = 0,
             max_chars: int = PIECE_MAX_CHARS) -> dict:
    """``RESULT_KEYS`` for a stored chunk (any version, ``version.latest`` tells which): one piece per
    covered unit, ``context_units`` whole units before and after. Raises ``NotFound``."""
    raise NotImplementedError('snippet.by_chunk: stage 1 (B)')


def by_locator(db: sqlite3.Connection, obj: Mapping, *, context_units: int = 0,
               max_chars: int = PIECE_MAX_CHARS) -> dict:
    """``RESULT_KEYS`` for one locator (``chunk`` None, one piece). Raises ``locator.LocatorError`` for a
    bad shape or a ``text_sha256`` mismatch, ``NotFound`` when the version or unit is not stored."""
    raise NotImplementedError('snippet.by_locator: stage 1 (B)')


def context(db: sqlite3.Connection, version_id: int, first: int, last: int, n: int,
            max_chars: int = PIECE_MAX_CHARS) -> tuple[list[dict], list[dict]]:
    """``(before, after)``: up to ``n`` whole units of ``version_id`` before ordinal ``first`` and after
    ``last``, as pieces (``chars = [0, len]``, cut by ``trim`` when over ``max_chars``)."""
    raise NotImplementedError('snippet.context: stage 1 (B)')


def trim(piece: Mapping, max_chars: int) -> dict:
    """``piece`` unchanged when its text fits ``max_chars``; else cut at the last word boundary inside the
    limit (``chunks._pull_to_words``), ``truncated`` True and the locator's ``chars``/``text_sha256``
    rebuilt for the cut range (needs no database: the text is the locator's text)."""
    raise NotImplementedError('snippet.trim: stage 1 (B)')

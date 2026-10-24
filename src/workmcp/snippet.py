"""``get_snippet`` core: stored units of a chunk or a locator as pieces with exact locators (design M3 §2.3).

A piece is ``{'locator', 'text', 'truncated'}`` (``PIECE_KEYS``) where ``text`` is exactly the text the
locator names (``binding.validate_locator`` gives it back). Pieces come from the stored units of the
version (never the chunk text joined across units, design M2 §2.5); context pieces are whole units
before and after (``chars = [0, len]``). A piece over ``max_chars`` is cut at the last word boundary
inside the limit and its locator is rebuilt for the cut range.

Any version is answered, superseded or of a deleted document (its units are kept, design M2 §4.1):
``version.latest`` and ``version.latest_version_id`` say which, and ``notices`` words it. Only reads
the connection it is given; arguments are checked by the caller (``tools.get_snippet``). Messages
name ids, sha12 and unit labels only, never text.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence

from . import chunks, locator, parsers

PIECE_MAX_CHARS = 4_000         # one piece's text (design M3 §4); tools.PIECE_MAX_CHARS is the same
CONTEXT_UNITS_MAX = 5           # design M3 §2.3
PIECE_KEYS = frozenset({'locator', 'text', 'truncated'})
RESULT_KEYS = frozenset({'doc', 'version', 'chunk', 'pieces', 'context_before', 'context_after'})
DOC_KEYS = frozenset({'id', 'path', 'ext'})
VERSION_KEYS = frozenset({'id', 'seq', 'sha256', 'observed_at', 'latest', 'latest_version_id'})
CHUNK_KEYS = frozenset({'id', 'ordinal', 'unit_first', 'unit_last', 'chunk_version'})
OLD_VERSION_NOTICE = '이 조각은 옛 버전(seq {seq})의 것입니다; 현재 버전 id {latest}'
DELETED_NOTICE = '이 문서는 삭제됨(조각은 마지막으로 수집된 버전의 것)'
TRUNCATED_NOTICE = ('조각 {n}개를 {max_chars}자 안의 낱말 경계에서 잘랐습니다(locator의 chars도 잘린 범위) — '
                    '이어 읽으려면 locator의 chars를 바꿔 다시 부르세요')

Covered = tuple[int, dict, str, tuple[int, int]]  # (ordinal, unit dict, unit text, (cs, ce))

# Version and document facts behind every result (one row per version id).
_VERSION_SQL = ('SELECT v.id, v.seq, v.sha256, v.observed_at, v.superseded_at, d.id, d.path, d.ext, d.status '
                'FROM versions v JOIN documents d ON d.id = v.document_id WHERE v.id = ?')


class NotFound(LookupError):
    """No such chunk, version or unit; the message names ids, sha12 and unit labels only (no text)."""


def cover(units_rows: Sequence[tuple[int, str, str]], char_start: int, char_end: int) -> list[Covered]:
    """``(ordinal, unit, text, (cs, ce))`` for each stored unit row ``(ordinal, locator_json, text)`` (in
    ordinal order, units joined by one separator character) that the chunk range ``[char_start,
    char_end)`` covers, with the range inside that unit; the same computation as ``search._hits``."""
    covered, offset = [], 0
    for ordinal, locator_json, text in units_rows:
        start, end = max(char_start - offset, 0), min(char_end - offset, len(text))
        if start < end:
            covered.append((ordinal, json.loads(locator_json), text, (start, end)))
        offset += len(text) + 1
    return covered


def trim(piece: Mapping, max_chars: int) -> dict:
    """``piece`` unchanged when its text fits ``max_chars``; else cut at the last word boundary inside the
    limit (``chunks._pull_to_words``), ``truncated`` True and the locator's ``chars``/``text_sha256``
    rebuilt for the cut range (needs no database: the text is the locator's text)."""
    text = piece['text']
    if len(text) <= max_chars:
        return dict(piece)
    start, end = chunks._pull_to_words(text, 0, max_chars, 0, len(text))
    old = piece['locator']
    base = old['chars'][0]
    cut = {**old, 'unit': dict(old['unit']), 'chars': [base + start, base + end],
           'text_sha256': locator.text_sha256(text[start:end])}
    locator.check_shape(cut)
    return {'locator': cut, 'text': text[start:end], 'truncated': True}


def _piece(facts: tuple, unit: dict, unit_text: str, chars, max_chars: int) -> dict:
    doc_id, path, sha256 = facts
    obj = locator.make(doc_id, path, sha256, unit, unit_text, list(chars))
    return trim({'locator': obj, 'text': unit_text[chars[0]:chars[1]], 'truncated': False}, max_chars)


def _version(db: sqlite3.Connection, version_id: int) -> tuple[dict, dict, tuple]:
    """``(doc, version, (doc id, path, sha256))`` of a stored version."""
    row = db.execute(_VERSION_SQL, (version_id,)).fetchone()
    if row is None:
        raise NotFound(f'version {version_id} not found')
    version_id, seq, sha256, observed_at, superseded_at, doc_id, path, ext, status = row
    current = None
    if status != 'deleted':
        found = db.execute('SELECT id FROM versions WHERE document_id = ? AND superseded_at IS NULL '
                           'ORDER BY seq DESC LIMIT 1', (doc_id,)).fetchone()
        current = found[0] if found else None
    version = {'id': version_id, 'seq': seq, 'sha256': sha256, 'observed_at': observed_at,
               'latest': superseded_at is None and status == 'active', 'latest_version_id': current}
    return {'id': doc_id, 'path': path, 'ext': ext}, version, (doc_id, path, sha256)


def _context(db: sqlite3.Connection, version_id: int, first: int, last: int, n: int, max_chars: int,
             facts: tuple) -> tuple[list[dict], list[dict]]:
    if n <= 0:
        return [], []
    rows = db.execute('SELECT ordinal, locator_json, text FROM units WHERE version_id = ? AND '
                      '((ordinal >= ? AND ordinal < ?) OR (ordinal > ? AND ordinal <= ?)) ORDER BY ordinal',
                      (version_id, first - n, first, last, last + n))
    before, after = [], []
    for ordinal, locator_json, text in rows:
        if not text:
            continue  # an empty unit has no range to cite
        piece = _piece(facts, json.loads(locator_json), text, (0, len(text)), max_chars)
        (before if ordinal < first else after).append(piece)
    return before, after


def context(db: sqlite3.Connection, version_id: int, first: int, last: int, n: int,
            max_chars: int = PIECE_MAX_CHARS) -> tuple[list[dict], list[dict]]:
    """``(before, after)``: up to ``n`` whole units of ``version_id`` before ordinal ``first`` and after
    ``last``, as pieces (``chars = [0, len]``, cut by ``trim`` when over ``max_chars``)."""
    _, _, facts = _version(db, version_id)
    return _context(db, version_id, first, last, n, max_chars, facts)


def by_chunk(db: sqlite3.Connection, chunk_id: int, *, context_units: int = 0,
             max_chars: int = PIECE_MAX_CHARS) -> dict:
    """``RESULT_KEYS`` for a stored chunk (any version, ``version.latest`` tells which): one piece per
    covered unit, ``context_units`` whole units before and after. Raises ``NotFound``."""
    row = db.execute('SELECT id, version_id, ordinal, unit_first, unit_last, char_start, char_end, chunk_version '
                     'FROM chunks WHERE id = ?', (chunk_id,)).fetchone()
    if row is None:
        raise NotFound(f'chunk {chunk_id} not found')
    chunk_id, version_id, ordinal, first, last, char_start, char_end, chunk_version = row
    doc, version, facts = _version(db, version_id)
    units = db.execute('SELECT ordinal, locator_json, text FROM units WHERE version_id = ? '
                       'AND ordinal BETWEEN ? AND ? ORDER BY ordinal', (version_id, first, last)).fetchall()
    covered = cover(units, char_start, char_end)
    if not covered:
        raise NotFound(f'chunk {chunk_id}: no stored unit of version {version_id} (sha {facts[2][:12]})')
    pieces = [_piece(facts, unit, text, chars, max_chars) for _, unit, text, chars in covered]
    before, after = _context(db, version_id, covered[0][0], covered[-1][0], context_units, max_chars, facts)
    return {'doc': doc, 'version': version,
            'chunk': {'id': chunk_id, 'ordinal': ordinal, 'unit_first': first, 'unit_last': last,
                      'chunk_version': chunk_version},
            'context_before': before, 'pieces': pieces, 'context_after': after}  # reading order (tools.pieces_of)


def by_locator(db: sqlite3.Connection, obj: Mapping, *, context_units: int = 0,
               max_chars: int = PIECE_MAX_CHARS) -> dict:
    """``RESULT_KEYS`` for one locator (``chunk`` None, one piece). Raises ``locator.LocatorError`` for a
    bad shape or a ``text_sha256`` mismatch, ``NotFound`` when the version or unit is not stored."""
    locator.check_shape(obj)
    found = db.execute('SELECT id FROM versions WHERE document_id = ? AND sha256 = ? ORDER BY id DESC LIMIT 1',
                       (obj['doc'], obj['sha256'])).fetchone()      # the lookup of binding.validate_locator
    if found is None:
        raise NotFound(f"version not found: doc {obj['doc']} sha {obj['sha256'][:12]}")
    version_id = found[0]
    unit = db.execute('SELECT ordinal, text FROM units WHERE version_id = ? AND locator_json = ?',
                      (version_id, parsers.unit_json(obj['unit']))).fetchone()
    if unit is None:
        raise NotFound(f"unit not found: doc {obj['doc']} sha {obj['sha256'][:12]} {locator.unit_label(obj['unit'])}")
    ordinal, text = unit
    locator.check_text(obj, text)           # range inside the unit, text_sha256 matches
    doc, version, facts = _version(db, version_id)
    pieces = [_piece(facts, obj['unit'], text, obj['chars'], max_chars)]
    before, after = _context(db, version_id, ordinal, ordinal, context_units, max_chars, facts)
    return {'doc': doc, 'version': version, 'chunk': None, 'context_before': before, 'pieces': pieces,
            'context_after': after}


def notices(result: Mapping, max_chars: int = PIECE_MAX_CHARS) -> list[str]:
    """Notice lines for a ``by_chunk``/``by_locator`` result: an old version or a deleted document, and
    pieces cut to ``max_chars`` (design M3 §2.3). No text."""
    out = []
    version = result['version']
    if not version['latest'] and version['latest_version_id'] != version['id']:
        if version['latest_version_id'] is None:
            out.append(DELETED_NOTICE)
        else:
            out.append(OLD_VERSION_NOTICE.format(seq=version['seq'], latest=version['latest_version_id']))
    cut = sum(piece['truncated'] for key in ('context_before', 'pieces', 'context_after') for piece in result[key])
    if cut:
        out.append(TRUNCATED_NOTICE.format(n=cut, max_chars=max_chars))
    return out

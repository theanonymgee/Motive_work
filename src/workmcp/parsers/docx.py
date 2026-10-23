"""DOCX paragraphs with table cell coordinates (design M1 §5.8).

Every ``w:p`` of ``word/document.xml`` (body, table cells, text boxes) is one unit. Its text is what
belongs to it directly: a ``w:t`` is read by its closest ``w:p`` ancestor, so a paragraph nested in a
text box or a table inside a paragraph is not repeated in the outer one. ``index`` counts every
``w:p`` of the document in order (empty ones too); ``table``, ``row``, ``column`` are the position of
the closest table, row and cell. Headers, footers, footnotes and comments are not read. Text boxes
that Word writes twice (``mc:Choice`` and ``mc:Fallback``) are read once, from ``mc:Choice``.

The part is read as a stream of events (``_container.XmlStream``): no tree, no element objects and no
part held whole, so memory is the open paragraphs and the units found. Everything kept is charged to the
file's budget when it is kept (parser resource design §3.3): a text piece, tab or break counts as text
(``too_large/limit_chars``) and towards its paragraph's ``max_unit_chars`` (``too_large/unit_chars``),
and a paragraph with text counts as a unit as soon as it ends (``too_large/limit_units``). A Strict Open
XML document is ``unsupported/ooxml_strict``.
"""
from __future__ import annotations

from . import Limits, ParseFailure, Unit, _container
from ._container import START, TEXT

NAME = 'docx'
VERSION = 'docx-5'

_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
_MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'
_DOCUMENT, _P, _T = _W + 'document', _W + 'p', _W + 't'
_TBL, _TR, _TC = _W + 'tbl', _W + 'tr', _W + 'tc'
_FALLBACK = _MC + 'Fallback'
_BREAKS = {_W + 'tab': '\t', _W + 'br': '\n', _W + 'cr': '\n'}


def _keep(paragraph: list, piece: str, most: int, budget) -> None:
    """Add a text piece (or a tab or break) to an open paragraph, charging it as text."""
    budget.text(len(piece))
    paragraph[3] += len(piece)
    if paragraph[3] > most:
        raise ParseFailure('too_large', 'unit_chars')
    if paragraph[1] is None:
        paragraph[1] = _container.Pieces()
    paragraph[1].append(piece)


def parse(data: bytes, limits: Limits, budget: _container.Budget) -> tuple[list[Unit], str | None]:
    archive = _container.open_zip(data, limits)
    stream = _container.XmlStream(_container.part_chunks(archive, 'word/document.xml', limits), limits, budget)
    path, most = stream.path, limits.max_unit_chars
    found = []        # (index, unit, text) of the paragraphs with text, in the order they end
    paragraphs = []   # open w:p: [index, Pieces (None before the first, False inside mc:Fallback), cell, chars]
    cells = []        # open w:tc: its coordinates, or None when it is not in a w:tr of a w:tbl
    tables = []       # open w:tbl: [number, w:tr children so far]
    rows = []         # open w:tr: [position in its w:tbl or None, w:tc children so far]
    index = table_count = fallback = 0
    for kind, tag, value in stream:
        if kind is TEXT:
            # Text directly in a w:t belongs to the closest open paragraph (the innermost one).
            if path[-1] == _T and paragraphs and (paragraph := paragraphs[-1])[1] is not False:
                _keep(paragraph, value, most, budget)
            continue
        parent = path[-1] if path else None
        if kind is START:
            if parent is None:
                _container.check_root(tag, _DOCUMENT)
            if tag == _P:
                index += 1
                paragraphs.append([index, False if fallback else None, cells[-1] if cells else None, 0])
            elif tag in _BREAKS:
                if paragraphs and paragraphs[-1][1] is not False:
                    _keep(paragraphs[-1], _BREAKS[tag], most, budget)
            elif tag == _TBL:
                table_count += 1
                tables.append([table_count, 0])
            elif tag == _TR:
                position = None
                if parent == _TBL:  # the parent is the innermost open table
                    tables[-1][1] += 1
                    position = tables[-1][1]
                rows.append([position, 0])
            elif tag == _TC:
                coordinates = None
                if parent == _TR:
                    row = rows[-1]
                    row[1] += 1
                    if row[0] is not None and len(path) > 1 and path[-2] == _TBL:
                        coordinates = {'table': tables[-1][0], 'row': row[0], 'column': row[1]}
                cells.append(coordinates)
            elif tag == _FALLBACK:
                fallback += 1
            continue
        if tag == _P:
            number, pieces, coordinates, _ = paragraphs.pop()
            text = pieces.text().strip() if pieces else ''
            if text:
                budget.unit()
                found.append((number, {'kind': 'paragraph', 'index': number, **(coordinates or {})}, text))
        elif tag == _TBL:
            tables.pop()
        elif tag == _TR:
            rows.pop()
        elif tag == _TC:
            cells.pop()
        elif tag == _FALLBACK:
            fallback -= 1
    found.sort(key=lambda item: item[0])  # a nested paragraph ends before the one around it
    return [Unit(n, 'paragraph', unit, text) for n, (_, unit, text) in enumerate(found, 1)], None

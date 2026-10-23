"""DOCX paragraphs with table cell coordinates (design M1 §5.8).

Every ``w:p`` of ``word/document.xml`` (body, table cells, text boxes) is one unit. Its text is what
belongs to it directly: a ``w:t`` is read by its closest ``w:p`` ancestor, so a paragraph nested in a
text box or a table inside a paragraph is not repeated in the outer one. ``index`` counts every
``w:p`` of the document in order (empty ones too); ``table``, ``row``, ``column`` are the position of
the closest table, row and cell. Headers, footers, footnotes and comments are not read. Text boxes
that Word writes twice (``mc:Choice`` and ``mc:Fallback``) are read once, from ``mc:Choice``.

The part is read as a stream of events (``_container.XmlStream``): no tree, no element objects and no
part held whole, so memory is the open paragraphs and the units found. One paragraph's text is at most
``Limits.xml_max_text_chars`` (``too_large/xml_text``) and the text kept for the whole file at most
``max_text_chars`` (``too_large/limit_chars``, raised while reading). A Strict Open XML document is
``unsupported/ooxml_strict``.
"""
from __future__ import annotations

from . import Limits, ParseFailure, Unit, _container
from ._container import START, TEXT

NAME = 'docx'
VERSION = 'docx-3'

_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
_MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'
_DOCUMENT, _P, _T = _W + 'document', _W + 'p', _W + 't'
_TBL, _TR, _TC = _W + 'tbl', _W + 'tr', _W + 'tc'
_FALLBACK = _MC + 'Fallback'
_BREAKS = {_W + 'tab': '\t', _W + 'br': '\n', _W + 'cr': '\n'}


def parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    archive = _container.open_zip(data, limits)
    stream = _container.XmlStream(_container.part_chunks(archive, 'word/document.xml', limits), limits)
    path, budget, most = stream.path, _container.TextBudget(limits), limits.xml_max_text_chars
    found = []        # (index, unit, text) of the paragraphs with text, in the order they end
    paragraphs = []   # open w:p: [index, pieces or None inside mc:Fallback, coordinates of the closest cell, chars]
    cells = []        # open w:tc: its coordinates, or None when it is not in a w:tr of a w:tbl
    tables = []       # open w:tbl: [number, w:tr children so far]
    rows = []         # open w:tr: [position in its w:tbl or None, w:tc children so far]
    index = table_count = fallback = 0
    for kind, tag, value in stream:
        if kind is TEXT:
            # Text directly in a w:t belongs to the closest open paragraph (the innermost one).
            if path[-1] == _T and paragraphs and (paragraph := paragraphs[-1])[1] is not None:
                paragraph[1].append(value)
                paragraph[3] += len(value)
                if paragraph[3] > most:
                    raise ParseFailure('too_large', 'xml_text')
                budget.take(len(value))
            continue
        parent = path[-1] if path else None
        if kind is START:
            if parent is None:
                _container.check_root(tag, _DOCUMENT)
            if tag == _P:
                index += 1
                paragraphs.append([index, None if fallback else [], cells[-1] if cells else None, 0])
            elif tag in _BREAKS:
                if paragraphs and paragraphs[-1][1] is not None:
                    paragraphs[-1][1].append(_BREAKS[tag])
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
            text = ''.join(pieces).strip() if pieces else ''
            if text:
                found.append((number, {'kind': 'paragraph', 'index': number, **(coordinates or {})}, text))
                if len(found) > limits.max_units:
                    raise ParseFailure('too_large', 'limit_units')
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

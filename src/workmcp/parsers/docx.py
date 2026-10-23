"""DOCX paragraphs with table cell coordinates (design M1 §5.8).

Every ``w:p`` of ``word/document.xml`` (body, table cells, text boxes) is one unit. Its text is what
belongs to it directly: a ``w:t`` is read by its closest ``w:p`` ancestor, so a paragraph nested in a
text box or a table inside a paragraph is not repeated in the outer one. ``index`` counts every
``w:p`` of the document in order (empty ones too); ``table``, ``row``, ``column`` are the position of
the closest table, row and cell. Headers, footers, footnotes and comments are not read. Text boxes
that Word writes twice (``mc:Choice`` and ``mc:Fallback``) are read once, from ``mc:Choice``.

The part is read as a stream of element events (``_container.iter_xml``) with no tree and no parent
map, so memory stays near the part's size. A Strict Open XML document is ``unsupported/ooxml_strict``.
"""
from __future__ import annotations

from . import Limits, ParseFailure, Unit, _container

NAME = 'docx'
VERSION = 'docx-2'

_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
_MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'
_DOCUMENT, _P, _T = _W + 'document', _W + 'p', _W + 't'
_TBL, _TR, _TC = _W + 'tbl', _W + 'tr', _W + 'tc'
_FALLBACK = _MC + 'Fallback'
_BREAKS = {_W + 'tab': '\t', _W + 'br': '\n', _W + 'cr': '\n'}


def parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    archive = _container.open_zip(data, limits)
    part = _container.read_part(archive, 'word/document.xml', limits)
    found = []        # (index, unit, text) of the paragraphs with text, in the order they end
    paragraphs = []   # open w:p: [index, pieces, inside mc:Fallback, coordinates of the closest cell]
    cells = []        # open w:tc: its coordinates, or None when it is not in a w:tr of a w:tbl
    tables = {}       # open w:tbl -> [number, w:tr children so far]
    rows = {}         # open w:tr -> [position in its w:tbl or None, w:tc children so far]
    texts = {}        # open w:t -> (pieces of its paragraph, slot for its text)
    index = table_count = fallback = 0
    for event, element, path in _container.iter_xml(part):
        tag = element.tag
        parent = path[-1] if path else None
        if event == 'start':
            if parent is None:
                _container.check_root(element, _DOCUMENT)
            if tag == _P:
                index += 1
                paragraphs.append([index, [], fallback > 0, cells[-1] if cells else None])
            elif tag == _T:
                if paragraphs:
                    pieces = paragraphs[-1][1]
                    texts[element] = (pieces, len(pieces))
                    pieces.append('')
            elif tag in _BREAKS:
                if paragraphs:
                    paragraphs[-1][1].append(_BREAKS[tag])
            elif tag == _TBL:
                table_count += 1
                tables[element] = [table_count, 0]
            elif tag == _TR:
                position = None
                if parent is not None and parent.tag == _TBL:
                    tables[parent][1] += 1
                    position = tables[parent][1]
                rows[element] = [position, 0]
            elif tag == _TC:
                coordinates = None
                if parent is not None and parent.tag == _TR:
                    row = rows[parent]
                    row[1] += 1
                    table = path[-2] if len(path) > 1 else None
                    if row[0] is not None and table is not None and table.tag == _TBL:
                        coordinates = {'table': tables[table][0], 'row': row[0], 'column': row[1]}
                cells.append(coordinates)
            elif tag == _FALLBACK:
                fallback += 1
            continue
        if tag == _P:
            number, pieces, skipped, coordinates = paragraphs.pop()
            text = ''.join(pieces).strip()
            if text and not skipped:
                found.append((number, {'kind': 'paragraph', 'index': number, **(coordinates or {})}, text))
                if len(found) > limits.max_units:
                    raise ParseFailure('too_large', 'limit_units')
        elif tag == _T:
            if element in texts:
                pieces, slot = texts.pop(element)
                pieces[slot] = element.text or ''
        elif tag == _TBL:
            del tables[element]
        elif tag == _TR:
            del rows[element]
        elif tag == _TC:
            cells.pop()
        elif tag == _FALLBACK:
            fallback -= 1
    found.sort(key=lambda item: item[0])  # a nested paragraph ends before the one around it
    return [Unit(n, 'paragraph', unit, text) for n, (_, unit, text) in enumerate(found, 1)], None

"""DOCX paragraphs with table cell coordinates (design M1 §5.8).

Every ``w:p`` of ``word/document.xml`` (body, table cells, text boxes) is one unit. Its text is what
belongs to it directly: a ``w:t`` is read by its closest ``w:p`` ancestor, so a paragraph nested in a
text box or a table inside a paragraph is not repeated in the outer one. ``index`` counts every
``w:p`` of the document in order (empty ones too); ``table``, ``row``, ``column`` are the position of
the closest table, row and cell. Headers, footers, footnotes and comments are not read. Text boxes
that Word writes twice (``mc:Choice`` and ``mc:Fallback``) are read once, from ``mc:Choice``.
"""
from __future__ import annotations

from . import Limits, ParseFailure, Unit, _container

NAME = 'docx'
VERSION = 'docx-1'

_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
_MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'
_P, _T, _TAB, _BR, _CR = _W + 'p', _W + 't', _W + 'tab', _W + 'br', _W + 'cr'
_TBL, _TR, _TC = _W + 'tbl', _W + 'tr', _W + 'tc'
_FALLBACK = _MC + 'Fallback'
_PIECES = {_T: None, _TAB: '\t', _BR: '\n', _CR: '\n'}


def _position(element, parents, positions):
    """1-based position of a ``w:tr``/``w:tc`` among its siblings of the same tag."""
    parent = parents[element]
    if parent not in positions:
        counts, table = {}, {}
        for child in parent:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            table[child] = counts[child.tag]
        positions[parent] = table
    return positions[parent][element]


def parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    archive = _container.open_zip(data, limits)
    root = _container.parse_xml(_container.read_part(archive, 'word/document.xml', limits))
    parents = {child: parent for parent in root.iter() for child in parent}
    # Per element, in document order (a parent always comes before its children): the closest ancestor
    # paragraph and cell, and whether it sits inside mc:Fallback. Walking up per element would be quadratic
    # on deeply nested XML.
    paragraph_of, cell_of, fallback = {root: None}, {root: None}, {root: False}
    paragraphs, indexes, pieces, tables = [], {}, {}, {}
    for element in root.iter():
        tag = element.tag
        if element is not root:
            parent = parents[element]
            paragraph_of[element] = parent if parent.tag == _P else paragraph_of[parent]
            cell_of[element] = parent if parent.tag == _TC else cell_of[parent]
            fallback[element] = fallback[parent] or parent.tag == _FALLBACK
        if tag == _P:
            paragraphs.append(element)
            indexes[element] = len(paragraphs)
            pieces[element] = []
        elif tag == _TBL:
            tables[element] = len(tables) + 1
        elif tag in _PIECES and paragraph_of[element] is not None:
            pieces[paragraph_of[element]].append((element.text or '') if tag == _T else _PIECES[tag])
    units, positions = [], {}
    for paragraph in paragraphs:
        text = ''.join(pieces[paragraph]).strip()
        if not text or fallback[paragraph]:
            continue
        unit = {'kind': 'paragraph', 'index': indexes[paragraph]}
        cell = cell_of[paragraph]
        row = parents.get(cell) if cell is not None else None
        table = parents.get(row) if row is not None else None
        if row is not None and row.tag == _TR and table.tag == _TBL:
            unit.update(table=tables[table], row=_position(row, parents, positions),
                        column=_position(cell, parents, positions))
        units.append(Unit(len(units) + 1, 'paragraph', unit, text))
        if len(units) > limits.max_units:
            raise ParseFailure('too_large', 'limit_units')
    return units, None

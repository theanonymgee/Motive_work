"""PPTX slide shapes and table cells (design M1 §5.8).

Slides are read in ``p:sldIdLst`` order (not file-name order). In a slide, every ``p:sp``, ``p:grpSp``,
``p:graphicFrame``, ``p:pic`` and ``p:cxnSp`` under the first ``p:cSld/p:spTree`` gets a shape number
``k`` in depth-first document order (a group gets one and its children follow). A shape with text gives
one ``slide`` unit (the ``a:p`` paragraphs of its first ``p:txBody`` joined by a line break); a table
gives one ``slide_cell`` unit per non-empty cell (rows are the ``a:tr`` of the frame in document order,
columns the ``a:tc`` children of a row). Notes, masters and layouts are not read;
``mc:AlternateContent`` is read through its first ``mc:Choice`` only (the others and ``mc:Fallback``
are alternatives of the same content).

Every part is read as a stream of events (``_container.XmlStream``), so no slide becomes a tree and
memory does not depend on a part's size; the presentation relationships keep only the slides' targets.
A paragraph's text is read from the ``a:t`` and ``a:br`` inside it (a nested paragraph gives its own
text only). One shape's or cell's text is at most ``xml_max_text_chars`` (``too_large/xml_text``). A
Strict Open XML presentation is ``unsupported/ooxml_strict``.
"""
from __future__ import annotations

from . import Limits, ParseFailure, Unit, _container
from ._container import START, TEXT

NAME = 'pptx'
VERSION = 'pptx-3'

_P = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
_A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
_R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
_REL = '{http://schemas.openxmlformats.org/package/2006/relationships}'
_MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'
_SHAPES = frozenset({_P + 'sp', _P + 'grpSp', _P + 'graphicFrame', _P + 'pic', _P + 'cxnSp'})
_SP, _GROUP, _FRAME, _TXBODY = _P + 'sp', _P + 'grpSp', _P + 'graphicFrame', _P + 'txBody'
_CSLD, _SPTREE, _ALTERNATE, _CHOICE = _P + 'cSld', _P + 'spTree', _MC + 'AlternateContent', _MC + 'Choice'
_TR, _TC, _CELL_BODY, _PARAGRAPH, _T, _BR = _A + 'tr', _A + 'tc', _A + 'txBody', _A + 'p', _A + 't', _A + 'br'
# Roles of open elements towards the shape numbering.
_CONTAINER, _ALTERNATIVES, _SHAPE, _ROW, _CELL, _BODY = range(6)


def _resolve(base: str, target: str) -> str:
    """Zip member name of a relationship ``target`` relative to the folder ``base`` (``'ppt'``)."""
    if target.startswith('/'):
        parts = []
        target = target[1:]
    else:
        parts = [part for part in base.split('/') if part]
    for part in target.split('/'):
        if part == '..':
            if parts:
                parts.pop()
        elif part and part != '.':
            parts.append(part)
    return '/'.join(parts)


def _slide_names(archive, limits: Limits, clock) -> list[str]:
    ids, first = [], True
    for kind, tag, value in _container.XmlStream(
            _container.part_chunks(archive, 'ppt/presentation.xml', limits), limits, clock):
        if kind is START:
            if first:
                _container.check_root(tag, _P + 'presentation')
                first = False
            if tag == _P + 'sldId':
                ids.append(value.get(_R + 'id'))
                if len(ids) > limits.pptx_max_slides:
                    raise ParseFailure('too_large', 'pptx_count')
    wanted, targets = set(ids), {}
    for kind, tag, value in _container.XmlStream(
            _container.part_chunks(archive, 'ppt/_rels/presentation.xml.rels', limits), limits, clock):
        if kind is START and tag == _REL + 'Relationship' and value.get('Id') in wanted:
            targets[value.get('Id')] = value.get('Target')  # a repeated Id: the last one wins
    names = []
    for rel_id in ids:
        target = targets.get(rel_id)
        if target is None:
            raise ParseFailure('failed', 'missing_part')
        names.append(_resolve('ppt', target))
    return names


class _Body:
    """Text of one ``p:txBody`` being read: its paragraphs in order and the open ones."""
    __slots__ = ('unit', 'paragraphs', 'open', 'chars')

    def __init__(self, unit: dict):
        self.unit, self.paragraphs, self.open, self.chars = unit, [], [], 0

    def text(self) -> str:
        return '\n'.join(''.join(pieces) for pieces in self.paragraphs).strip()


def _slide_units(chunks, slide: int, units: list, limits: Limits, clock, budget) -> None:
    stream = _container.XmlStream(chunks, limits, clock)
    path, most = stream.path, limits.xml_max_text_chars
    roles = []          # one per open element: (role, state) or None
    shape_number = 0
    tree_seen = False   # the first cSld/spTree is the shape tree; later ones are ignored
    frame = None        # [rows so far, cell units] of the open graphicFrame
    body = None         # the open txBody being read
    for kind, tag, value in stream:
        if kind is TEXT:
            if body is not None and body.open and path[-1] == _T:
                body.open[-1].append(value)
                body.chars += len(value)
                if body.chars > most:
                    raise ParseFailure('too_large', 'xml_text')
                budget.take(len(value))
            continue
        if kind is START:
            parent = roles[-1] if roles else None
            role = None
            if body is not None:
                if tag == _PARAGRAPH:
                    pieces = []
                    body.paragraphs.append(pieces)
                    body.open.append(pieces)
                elif tag == _BR and body.open:
                    body.open[-1].append('\n')
            elif parent is None:
                if tag == _SPTREE and len(path) == 2 and path[-1] == _CSLD and not tree_seen:
                    tree_seen, role = True, (_CONTAINER, None)
            elif parent[0] is _CONTAINER:
                if tag in _SHAPES:
                    shape_number += 1
                    if tag == _GROUP:
                        role = (_CONTAINER, None)
                    elif tag == _SP:
                        role = (_SHAPE, [False])  # [first txBody seen]
                    elif tag == _FRAME:
                        frame = [0, []]
                        role = (_SHAPE, None)
                    else:
                        role = (_SHAPE, None)
                elif tag == _ALTERNATE:
                    role = (_ALTERNATIVES, [False])  # [first mc:Choice seen]
            elif parent[0] is _ALTERNATIVES:
                if tag == _CHOICE and not parent[1][0]:
                    parent[1][0] = True
                    role = (_CONTAINER, None)
            elif parent[0] is _SHAPE and tag == _TXBODY and parent[1] is not None and not parent[1][0]:
                parent[1][0] = True
                body = _Body({'kind': 'slide', 'slide': slide, 'shape': shape_number})
                role = (_BODY, None)
            elif parent[0] is _CELL and tag == _CELL_BODY and not parent[1][2]:
                parent[1][2] = True
                body = _Body({'kind': 'slide_cell', 'slide': slide, 'shape': shape_number,
                              'row': parent[1][0], 'column': parent[1][1]})
                role = (_BODY, None)
            if role is None and body is None and frame is not None:
                if tag == _TR:
                    frame[0] += 1
                    role = (_ROW, [frame[0], 0])  # [row number, a:tc children so far]
                elif tag == _TC and parent is not None and parent[0] is _ROW:
                    parent[1][1] += 1
                    role = (_CELL, [parent[1][0], parent[1][1], False])  # [row, column, first txBody seen]
            roles.append(role)
            continue
        role = roles.pop()
        if body is not None:
            if role is not None and role[0] is _BODY:
                text = body.text()
                if text:
                    if body.unit['kind'] == 'slide':
                        units.append(Unit(len(units) + 1, 'slide', body.unit, text))
                    else:
                        frame[1].append((body.unit['row'], body.unit['column'], body.unit, text))
                body = None
            elif tag == _PARAGRAPH and body.open:
                body.open.pop()
        elif tag == _FRAME and frame is not None and role is not None and role[0] is _SHAPE:
            for _, _, unit, text in sorted(frame[1], key=lambda item: (item[0], item[1])):
                units.append(Unit(len(units) + 1, 'slide_cell', unit, text))
            frame = None
        if len(units) > limits.max_units:
            raise ParseFailure('too_large', 'limit_units')


def parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    archive = _container.open_zip(data, limits)
    clock, budget = _container.Clock(limits), _container.TextBudget(limits)
    units = []
    for slide, name in enumerate(_slide_names(archive, limits, clock), 1):
        _slide_units(_container.part_chunks(archive, name, limits), slide, units, limits, clock, budget)
    return units, None

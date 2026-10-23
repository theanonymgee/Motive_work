"""PPTX slide shapes and table cells (design M1 §5.8).

Slides are read in ``p:sldIdLst`` order (not file-name order). In a slide, every ``p:sp``, ``p:grpSp``,
``p:graphicFrame``, ``p:pic`` and ``p:cxnSp`` under ``p:spTree`` gets a shape number ``k`` in
depth-first document order (a group gets one and its children follow). A shape with text gives one
``slide`` unit (paragraphs joined by a line break); a table gives one ``slide_cell`` unit per
non-empty cell. Notes, masters and layouts are not read; ``mc:AlternateContent`` is read through its
``mc:Choice``.
"""
from __future__ import annotations

from . import Limits, ParseFailure, Unit, _container

NAME = 'pptx'
VERSION = 'pptx-1'

_P = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
_A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
_R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
_REL = '{http://schemas.openxmlformats.org/package/2006/relationships}'
_MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'
_SHAPES = frozenset({_P + 'sp', _P + 'grpSp', _P + 'graphicFrame', _P + 'pic', _P + 'cxnSp'})


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


def _relationships(archive, name: str, limits: Limits) -> dict[str, str]:
    root = _container.parse_xml(_container.read_part(archive, name, limits))
    return {rel.get('Id'): rel.get('Target') for rel in root.iter(_REL + 'Relationship')}


def _slide_names(archive, limits: Limits) -> list[str]:
    root = _container.parse_xml(_container.read_part(archive, 'ppt/presentation.xml', limits))
    rels = _relationships(archive, 'ppt/_rels/presentation.xml.rels', limits)
    names = []
    for slide_id in root.iter(_P + 'sldId'):
        target = rels.get(slide_id.get(_R + 'id'))
        if target is None:
            raise ParseFailure('failed', 'missing_part')
        names.append(_resolve('ppt', target))
    return names


def _text(body) -> str:
    """Paragraphs of a ``txBody`` joined by line breaks (``a:t`` text, ``a:br`` as a line break)."""
    paragraphs = []
    for paragraph in body.iter(_A + 'p'):
        paragraphs.append(''.join(
            '\n' if element.tag == _A + 'br' else (element.text or '')
            for element in paragraph.iter() if element.tag in (_A + 't', _A + 'br')))
    return '\n'.join(paragraphs).strip()


def _shapes(parent):
    """Shape elements below ``parent`` in depth-first order, looking through ``mc:Choice``."""
    for child in parent:
        if child.tag in _SHAPES:
            yield child
            if child.tag == _P + 'grpSp':
                yield from _shapes(child)
        elif child.tag == _MC + 'AlternateContent':
            for choice in child:
                if choice.tag == _MC + 'Choice':
                    yield from _shapes(choice)


def _slide_units(root, slide: int, units: list, limits: Limits) -> None:
    tree = root.find(f'{_P}cSld/{_P}spTree')
    if tree is None:
        return
    for shape_number, shape in enumerate(_shapes(tree), 1):
        found = []
        if shape.tag == _P + 'sp':
            body = shape.find(_P + 'txBody')
            if body is not None and (text := _text(body)):
                found.append(({'kind': 'slide', 'slide': slide, 'shape': shape_number}, text))
        elif shape.tag == _P + 'graphicFrame':
            for row, tr in enumerate(shape.iter(_A + 'tr'), 1):
                for column, tc in enumerate(tr.findall(_A + 'tc'), 1):
                    body = tc.find(_A + 'txBody')
                    if body is not None and (text := _text(body)):
                        found.append(({'kind': 'slide_cell', 'slide': slide, 'shape': shape_number,
                                       'row': row, 'column': column}, text))
        for unit, text in found:
            units.append(Unit(len(units) + 1, unit['kind'], unit, text))
        if len(units) > limits.max_units:
            raise ParseFailure('too_large', 'limit_units')


def parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    archive = _container.open_zip(data, limits)
    names = _slide_names(archive, limits)
    if len(names) > limits.pptx_max_slides:
        raise ParseFailure('too_large', 'pptx_count')
    units = []
    for slide, name in enumerate(names, 1):
        root = _container.parse_xml(_container.read_part(archive, name, limits))
        _slide_units(root, slide, units, limits)
    return units, None

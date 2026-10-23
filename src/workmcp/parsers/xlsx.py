"""XLSX non-empty cells (design M1 §5.8).

Sheets come in ``xl/workbook.xml`` order (hidden ones too), found through
``xl/_rels/workbook.xml.rels``. A cell is one ``sheet_cell`` unit: shared and inline strings are read
without phonetic runs (``rPh``), numbers, errors and formula results as the stored ``v`` text (no
number formats: a date stays its serial number), booleans as ``TRUE``/``FALSE``. A formula without a
cached value and an empty cell give no unit. Sheets and shared strings are read as a stream of element
events (``_container.iter_xml``) that keeps only the text it needs, so a large sheet never becomes a
tree. Two sheets with one name or two cells with one address in a sheet would give two units with
the same locator: such a damaged workbook is ``failed/unit_duplicate``. A Strict Open XML workbook is
``unsupported/ooxml_strict``.
"""
from __future__ import annotations

import re

from . import Limits, ParseFailure, Unit, _container

NAME = 'xlsx'
VERSION = 'xlsx-2'

_S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
_R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
_REL = '{http://schemas.openxmlformats.org/package/2006/relationships}'
_CELL = re.compile(r'([A-Z]{1,3})([1-9][0-9]*)')  # same shape as locator._CELL
_BOOLEANS = {'1': 'TRUE', '0': 'FALSE'}
_C, _ROW, _V, _IS, _SI, _T, _RUN = _S + 'c', _S + 'row', _S + 'v', _S + 'is', _S + 'si', _S + 't', _S + 'r'


def _resolve(base: str, target: str) -> str:
    """Zip member name of a relationship ``target`` relative to the folder ``base`` (``'xl'``)."""
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


def _letters(number: int) -> str:
    """Column letters of a 1-based column number (1 -> 'A', 27 -> 'AA')."""
    letters = ''
    while number:
        number, rest = divmod(number - 1, 26)
        letters = chr(ord('A') + rest) + letters
    return letters


def _column(letters: str) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + ord(letter) - ord('A') + 1
    return number


def _rich_owner(path: list):
    """The ``si``/``is`` whose text a ``t`` with this ``path`` belongs to: its parent, or the parent of its run.

    The ``t`` of a phonetic run (``rPh``) or of anything else has no owner.
    """
    parent = path[-1] if path else None
    if parent is not None and parent.tag in (_SI, _IS):
        return parent
    if parent is not None and parent.tag == _RUN and len(path) > 1 and path[-2].tag in (_SI, _IS):
        return path[-2]
    return None


def _shared_strings(archive, limits: Limits) -> list[str]:
    part = _container.read_part(archive, 'xl/sharedStrings.xml', limits, required=False)
    strings = []
    if part is None:
        return strings
    texts = {}  # open si -> its text pieces
    for event, element, path in _container.iter_xml(part):
        tag = element.tag
        if event == 'start':
            if tag == _SI:
                texts[element] = []
        elif tag == _SI:
            strings.append(''.join(texts.pop(element)))
        elif tag == _T:
            owner = _rich_owner(path)
            if owner in texts:
                texts[owner].append(element.text or '')
    return strings


def _sheets(archive, limits: Limits) -> list[tuple[str, str]]:
    root = _container.parse_xml(_container.read_part(archive, 'xl/workbook.xml', limits), limits)
    _container.check_root(root, _S + 'workbook')
    rels = _container.parse_xml(_container.read_part(archive, 'xl/_rels/workbook.xml.rels', limits), limits)
    targets = {rel.get('Id'): rel.get('Target') for rel in rels.iter(_REL + 'Relationship')}
    sheets = []
    for sheet in root.iter(_S + 'sheet'):
        target = targets.get(sheet.get(_R + 'id'))
        if target is None or not sheet.get('name'):
            raise ParseFailure('failed', 'missing_part')
        sheets.append((sheet.get('name'), _resolve('xl', target)))
    if len({name for name, _ in sheets}) != len(sheets):
        raise ParseFailure('failed', 'unit_duplicate')
    return sheets


class _Cell:
    """What a ``c`` element gives while it is open: its attributes, first ``v`` and first ``is``."""
    __slots__ = ('element', 'address', 'kind', 'value', 'inline', 'inline_element')

    def __init__(self, element):
        self.element, self.address, self.kind = element, element.get('r'), element.get('t', 'n')
        self.value, self.inline, self.inline_element = None, None, None

    def text(self, strings: list[str]) -> str:
        if self.kind == 'inlineStr':
            return ''.join(self.inline) if self.inline is not None else ''
        value = self.value or ''
        if self.kind == 's':
            return strings[int(value)] if value.strip().isdigit() and int(value) < len(strings) else ''
        if self.kind == 'b':
            return _BOOLEANS.get(value.strip(), '')
        return value


def _sheet_units(part, name: str, strings: list[str], units: list, limits: Limits) -> None:
    pending, row_number, cells, seen = [], 0, [], set()
    for event, element, path in _container.iter_xml(part):
        tag = element.tag
        parent = path[-1] if path else None
        cell = cells[-1] if cells else None
        if event == 'start':
            if tag == _C:
                cells.append(_Cell(element))
            elif tag == _IS and cell is not None and parent is cell.element and cell.inline is None:
                cell.inline, cell.inline_element = [], element
            continue
        if tag == _C:
            cells.pop()
            pending.append((cell.address, cell.text(strings).strip()))
        elif tag == _V:
            if cell is not None and parent is cell.element and cell.value is None:
                cell.value = element.text or ''
        elif tag == _T:
            if cell is not None and cell.inline_element is not None and _rich_owner(path) is cell.inline_element:
                cell.inline.append(element.text or '')
        elif tag == _ROW:
            row = element.get('r')
            row_number = int(row) if row and row.isdigit() else row_number + 1
            column = 0
            for address, text in pending:
                match = _CELL.fullmatch(address) if address else None
                if match:
                    column = _column(match[1])
                else:
                    column += 1
                    match = _CELL.fullmatch(f'{_letters(column)}{row_number}') if address is None else None
                if text and match:
                    if match[0] in seen:
                        raise ParseFailure('failed', 'unit_duplicate')
                    seen.add(match[0])
                    unit = {'kind': 'sheet_cell', 'sheet': name, 'cell': match[0]}
                    units.append(Unit(len(units) + 1, 'sheet_cell', unit, text))
                    if len(units) > limits.max_units:
                        raise ParseFailure('too_large', 'limit_units')
            pending.clear()


def parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    archive = _container.open_zip(data, limits)
    sheets = _sheets(archive, limits)
    if len(sheets) > limits.xlsx_max_sheets:
        raise ParseFailure('too_large', 'xlsx_count')
    strings = _shared_strings(archive, limits)
    units = []
    for name, member in sheets:
        _sheet_units(_container.read_part(archive, member, limits), name, strings, units, limits)
    return units, None

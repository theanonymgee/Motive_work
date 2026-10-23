"""XLSX non-empty cells (design M1 §5.8).

Sheets come in ``xl/workbook.xml`` order (hidden ones too), found through
``xl/_rels/workbook.xml.rels``. A cell is one ``sheet_cell`` unit: shared and inline strings are read
without phonetic runs (``rPh``), numbers, errors and formula results as the stored ``v`` text (no
number formats: a date stays its serial number), booleans as ``TRUE``/``FALSE``. A formula without a
cached value and an empty cell give no unit. Sheet and shared strings are read in a streaming way and
handled elements are cleared, so a large sheet does not need its whole tree in memory.
"""
from __future__ import annotations

import re

from . import Limits, ParseFailure, Unit, _container

NAME = 'xlsx'
VERSION = 'xlsx-1'

_S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
_R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
_REL = '{http://schemas.openxmlformats.org/package/2006/relationships}'
_CELL = re.compile(r'([A-Z]{1,3})([1-9][0-9]*)')  # same shape as locator._CELL
_BOOLEANS = {'1': 'TRUE', '0': 'FALSE'}


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


def _rich_text(element) -> str:
    """Text of an ``si`` or ``is``: its ``t`` and the ``t`` of its runs, never the phonetic ``rPh``."""
    parts = []
    for child in element:
        if child.tag == _S + 't':
            parts.append(child.text or '')
        elif child.tag == _S + 'r':
            parts.extend(t.text or '' for t in child if t.tag == _S + 't')
    return ''.join(parts)


def _shared_strings(archive, limits: Limits) -> list[str]:
    part = _container.read_part(archive, 'xl/sharedStrings.xml', limits, required=False)
    strings = []
    if part is not None:
        for element in _container.iter_xml(part):
            if element.tag == _S + 'si':
                strings.append(_rich_text(element))
                element.clear()
    return strings


def _sheets(archive, limits: Limits) -> list[tuple[str, str]]:
    root = _container.parse_xml(_container.read_part(archive, 'xl/workbook.xml', limits))
    rels = _container.parse_xml(_container.read_part(archive, 'xl/_rels/workbook.xml.rels', limits))
    targets = {rel.get('Id'): rel.get('Target') for rel in rels.iter(_REL + 'Relationship')}
    sheets = []
    for sheet in root.iter(_S + 'sheet'):
        target = targets.get(sheet.get(_R + 'id'))
        if target is None or not sheet.get('name'):
            raise ParseFailure('failed', 'missing_part')
        sheets.append((sheet.get('name'), _resolve('xl', target)))
    return sheets


def _cell_text(cell, strings: list[str]) -> str:
    kind = cell.get('t', 'n')
    if kind == 'inlineStr':
        inline = cell.find(_S + 'is')
        return _rich_text(inline) if inline is not None else ''
    value = cell.find(_S + 'v')
    value = (value.text or '') if value is not None else ''
    if kind == 's':
        return strings[int(value)] if value.strip().isdigit() and int(value) < len(strings) else ''
    if kind == 'b':
        return _BOOLEANS.get(value.strip(), '')
    return value


def _sheet_units(part: bytes, name: str, strings: list[str], units: list, limits: Limits) -> None:
    pending, row_number = [], 0
    for element in _container.iter_xml(part):
        if element.tag == _S + 'c':
            pending.append((element.get('r'), _cell_text(element, strings).strip()))
            element.clear()
        elif element.tag == _S + 'row':
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
                    unit = {'kind': 'sheet_cell', 'sheet': name, 'cell': match[0]}
                    units.append(Unit(len(units) + 1, 'sheet_cell', unit, text))
                    if len(units) > limits.max_units:
                        raise ParseFailure('too_large', 'limit_units')
            pending.clear()
            element.clear()


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

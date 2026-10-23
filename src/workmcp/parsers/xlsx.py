"""XLSX non-empty cells (design M1 §5.8).

Sheets come in ``xl/workbook.xml`` order (hidden ones too), found through
``xl/_rels/workbook.xml.rels``. A cell is one ``sheet_cell`` unit: shared and inline strings are read
without phonetic runs (``rPh``), numbers, errors and formula results as the stored ``v`` text (no
number formats: a date stays its serial number), booleans as ``TRUE``/``FALSE``. A formula without a
cached value and an empty cell give no unit. Every part (workbook, relationships, shared strings,
sheets) is read as a stream of events (``_container.XmlStream``) that keeps only the text it needs, so
no part becomes a tree or is held whole. Two sheets with one name or two cells with one address in a
sheet would give two units with the same locator: such a damaged workbook is ``failed/unit_duplicate``.
A Strict Open XML workbook is ``unsupported/ooxml_strict``.

Resources (parser resource design §3.5): sheet names, ids and targets and the shared string table are
held memory (``too_large/limit_memory``); a shared string is not unit text by itself, but every cell
that uses one counts its characters as text (``limit_chars``), so a short table read by many cells
cannot make more text than ``max_text_chars``. The text of one cell or shared string is at most
``max_unit_chars`` (``unit_chars``).
"""
from __future__ import annotations

import re

from . import Limits, ParseFailure, Unit, _container
from ._container import START, TEXT

NAME = 'xlsx'
VERSION = 'xlsx-4'

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


def _rich_owner(path: list) -> int | None:
    """Position in ``path`` of the ``si``/``is`` that owns the text of the ``t`` ending ``path``: the
    ``t``'s parent, or the parent of its run. The ``t`` of a phonetic run (``rPh``) or of anything else
    has no owner."""
    if len(path) > 1 and path[-2] in (_SI, _IS):
        return len(path) - 2
    if len(path) > 2 and path[-2] == _RUN and path[-3] in (_SI, _IS):
        return len(path) - 3
    return None


def _shared_strings(archive, limits: Limits, budget) -> list[str]:
    """Shared strings in order, already stripped (a cell's text is its string stripped, so the cell
    makes no copy). More of them than ``max_units`` is ``too_large/limit_units`` (Excel keeps only the
    strings its cells use, so each would be at least one cell unit). The table is held memory: a
    string's characters are held as they are read, the string itself when it ends."""
    chunks = _container.part_chunks(archive, 'xl/sharedStrings.xml', limits, required=False)
    strings = []
    if chunks is None:
        return strings
    stream = _container.XmlStream(chunks, limits, budget)
    path, most = stream.path, limits.max_unit_chars
    items = []  # open si: [position in path, pieces, chars]
    for kind, tag, value in stream:
        if kind is TEXT:
            if path[-1] == _T and items and _rich_owner(path) == items[-1][0]:
                item = items[-1]
                budget.hold(0, len(value))
                item[2] += len(value)
                if item[2] > most:
                    raise ParseFailure('too_large', 'unit_chars')
                item[1].append(value)
        elif kind is START:
            if tag == _SI:
                items.append([len(path), _container.Pieces(), 0])
        elif tag == _SI:
            budget.hold(1)
            strings.append(items.pop()[1].text().strip())
            if len(strings) > limits.max_units:
                raise ParseFailure('too_large', 'limit_units')
    return strings


def _sheets(archive, limits: Limits, budget) -> list[tuple[str, str]]:
    """``(name, member)`` of every sheet in workbook order. The relationships part is read after the
    workbook and keeps only the targets of those sheets."""
    found, first = [], True
    for kind, tag, value in _container.XmlStream(_container.part_chunks(archive, 'xl/workbook.xml', limits),
                                                 limits, budget):
        if kind is START:
            if first:
                _container.check_root(tag, _S + 'workbook')
                first = False
            if tag == _S + 'sheet':
                name, rel_id = value.get('name'), value.get(_R + 'id')
                budget.hold(1, len(name or '') + len(rel_id or ''))
                found.append((name, rel_id))
                if len(found) > limits.xlsx_max_sheets:
                    raise ParseFailure('too_large', 'xlsx_count')
    wanted = {rel_id for _, rel_id in found}
    targets = {}
    for kind, tag, value in _container.XmlStream(
            _container.part_chunks(archive, 'xl/_rels/workbook.xml.rels', limits), limits, budget):
        if kind is START and tag == _REL + 'Relationship' and value.get('Id') in wanted:
            target = value.get('Target')
            budget.hold(1, len(target or ''))
            targets[value.get('Id')] = target  # a repeated Id: the last one wins
    sheets = []
    for name, rel_id in found:
        target = targets.get(rel_id)
        if target is None or not name:
            raise ParseFailure('failed', 'missing_part')
        sheets.append((name, _resolve('xl', target)))
    if len({name for name, _ in sheets}) != len(sheets):
        raise ParseFailure('failed', 'unit_duplicate')
    return sheets


class _Cell:
    """What a ``c`` element gives while it is open: its attributes, first ``v`` and first ``is``.

    ``value_at``/``inline_at`` are the path positions of that ``v``/``is`` while it is open; ``chars``
    counts what was gathered, all of it charged as text when it was gathered."""
    __slots__ = ('address', 'kind', 'value', 'value_at', 'inline', 'inline_at', 'chars')

    def __init__(self, attributes):
        self.address, self.kind = attributes.get('r'), attributes.get('t', 'n')
        self.value = self.value_at = self.inline = self.inline_at = None
        self.chars = 0

    def text(self, strings: list[str]) -> str:
        if self.kind == 'inlineStr':
            return self.inline.text() if self.inline is not None else ''
        value = self.value.text() if self.value is not None else ''
        if self.kind == 's':
            index = value.strip()
            usable = index.isascii() and index.isdigit() and len(index) < 12 and int(index) < len(strings)
            return strings[int(index)] if usable else ''
        if self.kind == 'b':
            return _BOOLEANS.get(value.strip(), '')
        return value


def _sheet_units(chunks, name: str, strings: list[str], units: list, limits: Limits, budget) -> None:
    stream = _container.XmlStream(chunks, limits, budget)
    path, most = stream.path, limits.max_unit_chars
    row_number, column, cells, seen = 0, 0, [], set()
    for kind, tag, value in stream:
        cell = cells[-1] if cells else None
        if kind is TEXT:
            if cell is None:
                continue
            if len(path) - 1 == cell.value_at:
                target = cell.value
            elif path[-1] == _T and cell.inline_at is not None and _rich_owner(path) == cell.inline_at:
                target = cell.inline
            else:
                continue
            budget.text(len(value))
            cell.chars += len(value)
            if cell.chars > most:
                raise ParseFailure('too_large', 'unit_chars')
            target.append(value)
            continue
        parent = path[-1] if path else None
        if kind is START:
            if tag == _C:
                cells.append(_Cell(value))
            elif tag == _ROW:
                row = value.get('r')
                row_number = int(row) if row and row.isdigit() else row_number + 1
                column = 0
            elif cell is not None and parent == _C:  # a direct child of the innermost open cell
                if tag == _V and cell.value is None:
                    cell.value, cell.value_at = _container.Pieces(), len(path)
                elif tag == _IS and cell.inline is None:
                    cell.inline, cell.inline_at = _container.Pieces(), len(path)
            continue
        if tag == _C:
            # A cell becomes a unit when it ends; one without an address takes the next column of the
            # current row (the last row started), one with a malformed address gives none.
            cells.pop()
            address, text = cell.address, cell.text(strings).strip()
            match = _CELL.fullmatch(address) if address else None
            if match:
                column = _column(match[1])
            else:
                column += 1
                if text and address is None:  # only a cell with text needs the address it is given
                    match = _CELL.fullmatch(f'{_letters(column)}{row_number}')
            if text and match:
                if match[0] in seen:
                    raise ParseFailure('failed', 'unit_duplicate')
                if cell.kind in ('s', 'b') and len(text) > cell.chars:
                    # A shared string (or TRUE/FALSE) becomes this cell's text: charge what its index did not.
                    budget.text(len(text) - cell.chars)
                budget.unit()
                seen.add(match[0])
                units.append(Unit(len(units) + 1, 'sheet_cell', {'kind': 'sheet_cell', 'sheet': name,
                                                                 'cell': match[0]}, text))
        elif tag == _V and cell is not None and cell.value_at == len(path):
            cell.value_at = None
        elif tag == _IS and cell is not None and cell.inline_at == len(path):
            cell.inline_at = None


def parse(data: bytes, limits: Limits, budget: _container.Budget) -> tuple[list[Unit], str | None]:
    archive = _container.open_zip(data, limits)
    sheets = _sheets(archive, limits, budget)
    strings = _shared_strings(archive, limits, budget)
    units = []
    for name, member in sheets:
        _sheet_units(_container.part_chunks(archive, member, limits), name, strings, units, limits, budget)
    return units, None

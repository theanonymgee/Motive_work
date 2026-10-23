"""TXT/MD lines and CSV rows with the encoding ladder (design M1 §5.7, §5.8; parser resource design §3.9).

The decoded text is read a block of about 64 Ki characters at a time (``lines``), never split into a
list of every line: each line is one event of the file's budget (``too_large/limit_events``), one line
over ``max_unit_chars`` is ``too_large/unit_chars``, and every unit's text and the unit itself are
charged when it is made. CSV rows are read from the same lines, so the text is not copied into a
second buffer; one row gathered from more than ``max_unit_chars`` of lines is ``unit_chars`` too.
"""
from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterator

from . import Limits, ParseFailure, Unit, _container

NAME = 'text'
VERSION = 'text-3'
CSV_NAME = 'csv'
CSV_VERSION = 'csv-3'

_LINE_BREAK = re.compile(r'\r\n|\r|\n')  # not str.splitlines(): it also splits on \x0b, \x1c, U+2028 ...
_LINE_END = re.compile(r'(\r\n|\r|\n)')
_UTF16_BOMS = (b'\xff\xfe', b'\xfe\xff')
_BLOCK_CHARS = 1 << 16  # characters of text split into lines at a time (a list of at most 64 Ki lines)


def decode(data: bytes) -> tuple[str, str]:
    """``(text, encoding)`` by the ladder of design §5.7; ``ParseFailure`` when nothing fits."""
    if b'\x00' in data and data[:2] not in _UTF16_BOMS:
        raise ParseFailure('failed', 'binary_content')
    try:
        text = data.decode('utf-8-sig')
        return text, 'utf-8-sig' if data.startswith(b'\xef\xbb\xbf') else 'utf-8'
    except UnicodeDecodeError:
        pass
    if data[:2] in _UTF16_BOMS:
        try:
            return data.decode('utf-16'), 'utf-16'
        except UnicodeDecodeError:
            pass
    try:
        return data.decode('cp949'), 'cp949'
    except UnicodeDecodeError:
        raise ParseFailure('failed', 'encoding_unknown') from None


def _blocks(text: str, limits: Limits) -> Iterator[tuple[str, bool]]:
    """``(block, last)``: ``text`` in order, cut after a line break about every 64 Ki characters. A line
    that runs more than ``max_unit_chars`` past a cut is ``unit_chars`` before it is copied."""
    pos, size = 0, len(text)
    while True:
        cut = pos + _BLOCK_CHARS
        match = _LINE_BREAK.search(text, cut) if cut < size else None
        if match is None:
            if size - cut > limits.max_unit_chars:
                raise ParseFailure('too_large', 'unit_chars')
            yield text[pos:], True
            return
        if match.start() - cut > limits.max_unit_chars:
            raise ParseFailure('too_large', 'unit_chars')
        yield text[pos:match.end()], False
        pos = match.end()


def line_blocks(text: str, limits: Limits, budget) -> Iterator[tuple[int, list[str]]]:
    """``(number of the first line, lines)`` of one block at a time: every line (``\\r\\n``, ``\\r`` or
    ``\\n`` ends one) without its end, numbered from 1. The block's lines are charged as events and
    checked against ``max_unit_chars`` before they are handed out."""
    number = 1
    for block, last in _blocks(text, limits):
        found = _LINE_BREAK.split(block)
        if not last:
            found.pop()  # the empty rest after the block's final line break
        budget.event(len(found))
        if max(map(len, found), default=0) > limits.max_unit_chars:
            raise ParseFailure('too_large', 'unit_chars')
        yield number, found
        number += len(found)


def lines(text: str, limits: Limits, budget) -> Iterator[tuple[int, str]]:
    """``(number, line)`` of every line, as ``line_blocks`` reads them."""
    for first, found in line_blocks(text, limits, budget):
        yield from enumerate(found, first)


def _lines_with_ends(text: str, limits: Limits, budget, pending: list) -> Iterator[str]:
    """Every line with its line break, as ``io.StringIO(text, newline='')`` gives them. ``pending[0]``
    counts the characters handed out since the caller last reset it (one CSV row being gathered)."""
    for block, last in _blocks(text, limits):
        parts = _LINE_END.split(block)  # line, break, line, break, ..., rest
        rest = parts.pop()
        budget.event(len(parts) // 2 + (1 if rest else 0))
        for at in range(0, len(parts), 2):
            line = parts[at] + parts[at + 1]
            pending[0] += len(line)
            if pending[0] > limits.max_unit_chars:
                raise ParseFailure('too_large', 'unit_chars')
            yield line
        if rest:  # only the last block can end without a line break
            pending[0] += len(rest)
            if pending[0] > limits.max_unit_chars:
                raise ParseFailure('too_large', 'unit_chars')
            yield rest


def parse(data: bytes, limits: Limits, budget: _container.Budget) -> tuple[list[Unit], str | None]:
    text, encoding = decode(data)
    units = []
    for first, found in line_blocks(text, limits, budget):
        for number, line in enumerate(found, first):
            if line and (line := line.strip()):
                budget.text(len(line))
                budget.unit()
                units.append(Unit(len(units) + 1, 'line', {'kind': 'line', 'line': number}, line))
    return units, encoding


def parse_csv(data: bytes, limits: Limits, budget: _container.Budget) -> tuple[list[Unit], str | None]:
    text, encoding = decode(data)
    units, columns_of, pending = [], {}, [0]
    for number, row in enumerate(csv.reader(_lines_with_ends(text, limits, budget, pending)), 1):
        pending[0] = 0
        if len(row) > limits.csv_max_columns:
            raise ParseFailure('failed', 'csv_columns')
        if not any(cell.strip() for cell in row):
            continue
        out = io.StringIO()
        csv.writer(out, lineterminator='').writerow(row)
        cell_text = out.getvalue().strip()
        budget.text(len(cell_text))
        budget.unit()
        columns = columns_of.get(len(row))
        if columns is None:  # one list per row width, shared by every row of that width (never changed)
            columns = columns_of[len(row)] = list(range(1, len(row) + 1))
        units.append(Unit(len(units) + 1, 'cell', {'kind': 'cell', 'row': number, 'columns': columns}, cell_text))
    return units, encoding

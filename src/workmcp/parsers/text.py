"""TXT/MD lines and CSV rows with the encoding ladder (design M1 §5.7, §5.8)."""
from __future__ import annotations

import csv
import io
import re

from . import Limits, ParseFailure, Unit

NAME = 'text'
VERSION = 'text-1'
CSV_NAME = 'csv'
CSV_VERSION = 'csv-1'

_LINE_BREAK = re.compile(r'\r\n|\r|\n')  # not str.splitlines(): it also splits on \x0b, \x1c, U+2028 ...
_UTF16_BOMS = (b'\xff\xfe', b'\xfe\xff')


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


def _check_count(count: int, limits: Limits) -> None:
    if count > limits.max_units:  # the dispatcher applies the same limit; stop early on huge inputs
        raise ParseFailure('too_large', 'limit_units')


def parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    text, encoding = decode(data)
    units = []
    for number, line in enumerate(_LINE_BREAK.split(text), 1):
        line = line.strip()
        if line:
            units.append(Unit(len(units) + 1, 'line', {'kind': 'line', 'line': number}, line))
            _check_count(len(units), limits)
    return units, encoding


def parse_csv(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    text, encoding = decode(data)
    units = []
    for number, row in enumerate(csv.reader(io.StringIO(text, newline='')), 1):
        if len(row) > limits.csv_max_columns:
            raise ParseFailure('failed', 'csv_columns')
        if not any(cell.strip() for cell in row):
            continue
        out = io.StringIO()
        csv.writer(out, lineterminator='').writerow(row)
        units.append(Unit(len(units) + 1, 'cell', {'kind': 'cell', 'row': number, 'columns': list(range(1, len(row) + 1))},
                          out.getvalue().strip()))
        _check_count(len(units), limits)
    return units, encoding

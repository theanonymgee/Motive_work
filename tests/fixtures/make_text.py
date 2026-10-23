"""Synthetic TXT, MD and CSV documents (design M1 §7.3). Standard library only.

``build()`` returns ``(Expected, bytes)`` pairs and ``write(root)`` writes them below ``root``
(``docs/`` for the readable cases, ``edge/`` for empty and unreadable ones) and returns the
``Expected`` list. Every case is parsed with the default ``Limits``; cases that need a small limit
(CSV column limit) are built by ``csv_wide_row`` and checked by the parser tests only.

Content comes from ``fixtures.LINES_KO`` (see the content rules in ``fixtures/__init__.py``).
CSV rows are numbered by record (a quoted cell with a line break is one row).
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from . import LINES_KO, SENTINEL, SENTINEL_ASCII, Expected

LONG_LINE_CHARS = 20_000


def _units_for_lines(raw_lines) -> dict[int, tuple[str, dict, str]]:
    """Expected units of ``raw_lines`` (one entry per physical line, '' for a blank line)."""
    sample, ordinal = {}, 0
    for number, line in enumerate(raw_lines, 1):
        if line.strip():
            ordinal += 1
            sample[ordinal] = ('line', {'kind': 'line', 'line': number}, line.strip())
    return sample


def _joined(raw_lines, eols) -> str:
    """``raw_lines`` joined with the line endings of ``eols`` in turn (the last line has none)."""
    out = []
    for number, line in enumerate(raw_lines):
        out.append(line)
        if number < len(raw_lines) - 1:
            out.append(eols[number % len(eols)])
    return ''.join(out)


def _text_case(rel_path, raw_lines, encoding_name, codec, eols=('\n',), bom=b'') -> tuple[Expected, bytes]:
    data = bom + _joined(raw_lines, eols).encode(codec)
    sample = _units_for_lines(raw_lines)
    return Expected(rel_path, 'text', 'ok', None, len(sample), sample, encoding_name), data


def csv_bytes(rows, codec='utf-8', bom=b'') -> bytes:
    out = io.StringIO()
    csv.writer(out, lineterminator='\r\n').writerows(rows)
    return bom + out.getvalue().encode(codec)


def csv_rows() -> list[list[str]]:
    return [
        ['항목', '금액', '비고'],
        ['견적', '1,234,000', '가상 거래처 A'],
        ['메모', '두 줄\n메모', ''],
        [SENTINEL, '', ''],
        [],
        ['  끝 행  ', 'x'],
    ]


def csv_expected(rel_path, encoding_name) -> Expected:
    sample = {
        1: ('cell', {'kind': 'cell', 'row': 1, 'columns': [1, 2, 3]}, '항목,금액,비고'),
        2: ('cell', {'kind': 'cell', 'row': 2, 'columns': [1, 2, 3]}, '견적,"1,234,000",가상 거래처 A'),
        3: ('cell', {'kind': 'cell', 'row': 3, 'columns': [1, 2, 3]}, '메모,"두 줄\n메모",'),
        4: ('cell', {'kind': 'cell', 'row': 4, 'columns': [1, 2, 3]}, f'{SENTINEL},,'),
        5: ('cell', {'kind': 'cell', 'row': 6, 'columns': [1, 2]}, '끝 행  ,x'),
    }
    return Expected(rel_path, 'csv', 'ok', None, len(sample), sample, encoding_name)


def csv_wide_row(columns: int) -> bytes:
    """One row of ``columns`` cells (for ``Limits(csv_max_columns=...)``)."""
    return csv_bytes([[f'열{n}' for n in range(1, columns + 1)]])


def build() -> list[tuple[Expected, bytes]]:
    mixed = [f'  {LINES_KO[0]}  ', '', LINES_KO[1], '\t', LINES_KO[2], LINES_KO[3], '', '', LINES_KO[4]]
    cases = [
        _text_case('docs/notes_utf8.txt', list(LINES_KO), 'utf-8', 'utf-8'),
        _text_case('docs/notes_bom.md', list(LINES_KO), 'utf-8-sig', 'utf-8', eols=('\r\n',), bom=b'\xef\xbb\xbf'),
        _text_case('docs/notes_cp949.txt', list(LINES_KO), 'cp949', 'cp949'),
        _text_case('docs/notes_utf16.txt', list(LINES_KO), 'utf-16', 'utf-16-le', bom=b'\xff\xfe'),
        _text_case('docs/notes_crlf.txt', list(LINES_KO), 'utf-8', 'utf-8', eols=('\r\n',)),
        # Never CR, empty line, LF in a row: that would be one CRLF.
        _text_case('docs/notes_mixed.md', mixed, 'utf-8', 'utf-8', eols=('\n', '\r\n', '\r', '\r\n')),
        _text_case('docs/long_line.txt', [LINES_KO[0] + '가' * LONG_LINE_CHARS, LINES_KO[1]], 'utf-8', 'utf-8'),
        _text_case('docs/ascii.txt', [f'{SENTINEL_ASCII} plain ascii line', '', 'second line'], 'utf-8', 'utf-8'),
        (csv_expected('docs/table_utf8.csv', 'utf-8'), csv_bytes(csv_rows())),
        (csv_expected('docs/table_cp949.csv', 'cp949'), csv_bytes(csv_rows(), codec='cp949')),
        # Edge cases: an empty file is a valid observation; binary and unknown encodings are failures.
        (Expected('edge/empty.txt', 'text', 'ok'), b''),
        (Expected('edge/binary.txt', 'text', 'failed', 'binary_content'), b'synthetic\x00binary\x00data'),
        (Expected('edge/unknown_encoding.txt', 'text', 'failed', 'encoding_unknown'), b'\x81\x20unknown'),
    ]
    return cases


def write(root: Path) -> list[Expected]:
    expected = []
    for item, data in build():
        target = Path(root) / item.rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        expected.append(item)
    return expected

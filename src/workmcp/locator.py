"""Source locator format and checks (design §3.3).

A locator is a JSON object naming a document, the SHA-256 of one file version, one parser
unit inside it, a ``[start, end)`` character range inside that unit's text and the SHA-256
of ``text[start:end]``. It is stored as JSON only; ``display`` gives the one-line form.

The word/sentence tokenizer and ``anchor`` are ported from ``services/source_coordinates.py``
(Motive_vari, tag kakao-p1-complete-20261022): a word range ``[first, last]`` (1-based,
inclusive) becomes a unit-relative character range plus its hash.

M0 defines the format and the checks that need no database. Checking a locator against
stored versions and units comes with the collector (M1).
"""
from __future__ import annotations

import hashlib
import re

LOCATOR_VERSION = 1
COORD_VERSION = 'unit-words-sentences-v1'
# A word ending with terminal punctuation (optionally closed by quotes or
# brackets) ends a sentence; so does the last word of a line.
_SENTENCE_END = re.compile(
    r'[.!?…。！？]'
    r'["\'”’)\]》〉】」』）]*$')
# Line breaks include the Unicode line/paragraph separators, VT, FF and NEL.
_LINE_BREAK = re.compile('[\r\n\x0b\x0c\x85  ]')
# Words split on whitespace and on zero-width spaces.
_WORD = re.compile('[^\\s​]+')
_HEX64 = re.compile(r'[0-9a-f]{64}')
_CELL = re.compile(r'[A-Z]{1,3}[1-9][0-9]*')

# unit kind -> (required integer fields, required other fields, optional integer fields)
UNIT_KINDS: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    'line': (('line',), (), ()),
    'cell': (('row',), ('columns',), ()),
    'page': (('page',), (), ()),
    'paragraph': (('index',), (), ('table', 'row', 'column')),
    'slide': (('slide', 'shape'), (), ()),
    'slide_cell': (('slide', 'shape', 'row', 'column'), (), ()),
    'sheet_cell': ((), ('sheet', 'cell'), ()),
    'mail_header': ((), (), ()),
    'mail': (('part', 'line'), (), ()),
    'section_para': (('section', 'index'), (), ()),
}
LOCATOR_KEYS = frozenset({'v', 'doc', 'path', 'sha256', 'unit', 'chars', 'text_sha256'})


class CoordinateError(ValueError):
    def __init__(self, code):
        super().__init__(f'Invalid source coordinate: {code}')
        self.code = code


class LocatorError(CoordinateError):
    """Locator check failure; ``code`` is fixed (coordinate codes are reused for ``chars``)."""


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def words(text):
    """Whitespace/zero-width-space delimited words as character spans; numbers are 1-based in ranges."""
    return [(match.start(), match.end()) for match in _WORD.finditer(text or '')]


def sentences(text):
    """Inclusive 1-based word ranges; boundaries always coincide with word boundaries."""
    spans, result, start = words(text), [], None
    for number, (begin, end) in enumerate(spans, 1):
        start = start or number
        last = number == len(spans)
        if (last or _SENTENCE_END.search(text[begin:end])
                or _LINE_BREAK.search(text[end:spans[number][0]])):
            result.append([start, number])
            start = None
    return result


def check(value, count):
    """A validated [first, last] word range, or a fixed coordinate code."""
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(item) is not int for item in value)):
        raise CoordinateError('coordinate_shape_invalid')
    first, last = value
    if first > last:
        raise CoordinateError('coordinate_reversed')
    if not 1 <= first <= last <= count:
        raise CoordinateError('coordinate_out_of_range')
    return [first, last]


def anchor(text, value):
    """Unit-relative character anchor of a word range; the literal is text[start:end]."""
    spans = words(text)
    first, last = check(value, len(spans))
    start, end = spans[first - 1][0], spans[last - 1][1]
    return {'w': [first, last], 'start': start, 'end': end, 'text_sha256': text_sha256(text[start:end])}


def literal(text, value):
    item = anchor(text, value)
    return text[item['start']:item['end']]


def within(inner, outer):
    return outer['w'][0] <= inner['w'][0] and inner['w'][1] <= outer['w'][1]


def sentence_table(text):
    """Stable sentence IDs for later human confirmation; never a verdict."""
    return [{'s': number, **anchor(text, value)} for number, value in enumerate(sentences(text), 1)]


def quote_range(text: str, quote: str) -> list[int]:
    """``[start, end)`` of the first exact occurrence of ``quote`` in the unit text."""
    if not quote:
        raise LocatorError('quote_empty')
    start = text.find(quote)
    if start < 0:
        raise LocatorError('evidence_quote_not_found')
    return [start, start + len(quote)]


def _positive_int(value) -> bool:
    return type(value) is int and value >= 1


def check_unit(unit) -> dict:
    if not isinstance(unit, dict) or unit.get('kind') not in UNIT_KINDS:
        raise LocatorError('unit_kind_invalid')
    ints, others, optional = UNIT_KINDS[unit['kind']]
    allowed = {'kind', *ints, *others, *optional}
    if set(unit) - allowed or any(not _positive_int(unit.get(name)) for name in ints):
        raise LocatorError('unit_shape_invalid')
    if any(name in unit and not _positive_int(unit[name]) for name in optional):
        raise LocatorError('unit_shape_invalid')
    if 'columns' in others and (not isinstance(unit.get('columns'), list) or not unit['columns']
                                or not all(_positive_int(c) for c in unit['columns'])):
        raise LocatorError('unit_shape_invalid')
    if 'sheet' in others and (not isinstance(unit.get('sheet'), str) or not unit['sheet']
                              or not isinstance(unit.get('cell'), str) or not _CELL.fullmatch(unit['cell'])):
        raise LocatorError('unit_shape_invalid')
    return unit


def check_chars(value) -> list[int]:
    """``[start, end)`` with ``0 <= start < end``; the upper bound is checked against the unit text."""
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(item) is not int for item in value)):
        raise LocatorError('coordinate_shape_invalid')
    start, end = value
    if start >= end:
        raise LocatorError('coordinate_reversed')
    if start < 0:
        raise LocatorError('coordinate_out_of_range')
    return [start, end]


def check_shape(obj) -> dict:
    """Fields and formats of a locator object (no database, no unit text)."""
    if not isinstance(obj, dict) or set(obj) != LOCATOR_KEYS:
        raise LocatorError('locator_shape_invalid')
    if type(obj['v']) is not int or obj['v'] != LOCATOR_VERSION:
        raise LocatorError('locator_version_unsupported')
    if not _positive_int(obj['doc']) or not isinstance(obj['path'], str) or not obj['path']:
        raise LocatorError('locator_shape_invalid')
    for key in ('sha256', 'text_sha256'):
        if not isinstance(obj[key], str) or not _HEX64.fullmatch(obj[key]):
            raise LocatorError('locator_hash_invalid')
    check_unit(obj['unit'])
    check_chars(obj['chars'])
    return obj


def check_text(obj: dict, unit_text: str) -> str:
    """Range inside ``unit_text`` and quote hash match; returns the quoted text."""
    start, end = check_shape(obj)['chars']
    if end > len(unit_text):
        raise LocatorError('coordinate_out_of_range')
    quoted = unit_text[start:end]
    if text_sha256(quoted) != obj['text_sha256']:
        raise LocatorError('locator_text_mismatch')
    return quoted


def make(doc: int, path: str, sha256: str, unit: dict, unit_text: str, chars) -> dict:
    """Locator for ``unit_text[chars[0]:chars[1]]``; checked before it is returned."""
    start, end = check_chars(chars)
    obj = {'v': LOCATOR_VERSION, 'doc': doc, 'path': path, 'sha256': sha256, 'unit': dict(unit),
           'chars': [start, end], 'text_sha256': text_sha256(unit_text[start:end])}
    check_text(obj, unit_text)
    return obj


def unit_label(unit: dict) -> str:
    kind = unit['kind']
    if kind == 'line':
        return f"줄 {unit['line']}"
    if kind == 'cell':
        return f"행 {unit['row']} 열 {','.join(map(str, unit['columns']))}"
    if kind == 'page':
        return f"쪽 {unit['page']}"
    if kind == 'paragraph':
        if 'table' in unit:
            return f"표 {unit['table']} 행 {unit.get('row', '?')} 열 {unit.get('column', '?')} (문단 {unit['index']})"
        return f"문단 {unit['index']}"
    if kind == 'slide':
        return f"슬라이드 {unit['slide']} 도형 {unit['shape']}"
    if kind == 'slide_cell':
        return f"슬라이드 {unit['slide']} 도형 {unit['shape']} 행 {unit['row']} 열 {unit['column']}"
    if kind == 'sheet_cell':
        return f"시트 {unit['sheet']} {unit['cell']}"
    if kind == 'mail_header':
        return '메일 헤더'
    if kind == 'mail':
        return f"메일 파트 {unit['part']} 줄 {unit['line']}"
    return f"섹션 {unit['section']} 문단 {unit['index']}"


def display(obj: dict) -> str:
    """``{path} @ {sha256[:12]} / {unit_label} / chars {start}-{end}``."""
    check_shape(obj)
    start, end = obj['chars']
    return f"{obj['path']} @ {obj['sha256'][:12]} / {unit_label(obj['unit'])} / chars {start}-{end}"

"""Parser dispatch and the types every parser shares (design M1 §5.1-§5.6).

Contract of a parser module (``text``, ``eml``, ``docx``, ``pptx``, ``xlsx``, ``pdf``, ``hwp``):

- ``NAME: str`` and ``VERSION: str`` (``"<name>-<n>"``, plus ``"/<lib>-<ver>"`` when a third-party
  library decides the output, design §5.5). A module that reads two formats also has
  ``CSV_NAME``/``CSV_VERSION``/``parse_csv`` (``text``) or ``HWPX_NAME``/``HWPX_VERSION``/``parse_hwpx``
  (``hwp``).
- ``parse(data: bytes, limits: Limits, budget: _container.Budget) -> tuple[list[Unit], str | None]``:
  the units in document order and the encoding (``None`` when the format declares its own). It gets
  bytes only, never a path, and raises only ``ParseFailure``. It charges ``budget`` for everything it
  keeps while it reads (parser resource design §2, §3). Any other exception becomes
  ``failed/exception:<Class>``; its message is never stored or logged (it may carry document text).

``parse(data, ext, limits)`` here never raises for bad content: it checks the signature
(``sniff``), makes the file's budget, calls the module, renumbers ordinals 1..N, checks every unit
with ``locator.check_unit``, refuses two units with the same locator in one version
(``failed/unit_duplicate``: binding and locator checks look a unit up by its locator) and applies
the unit and text limits again. The checks after the parser run on the same clock. Same bytes, same
limits and same parser version give the same result (binding depends on it).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace

from .. import locator

SUPPORTED_EXTENSIONS = frozenset({
    '.txt', '.md', '.csv', '.pdf', '.docx', '.pptx', '.xlsx', '.eml', '.hwp', '.hwpx'})
ZIP_EXTENSIONS = frozenset({'.docx', '.pptx', '.xlsx', '.hwpx'})
ZIP_SIGNATURES = (b'PK\x03\x04', b'PK\x05\x06')
OLE_SIGNATURE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
PDF_SIGNATURE = b'%PDF-'

# versions.parse_status values (store.py CHECK). 'locked' is set by the collector, 'pending' is reserved.
PARSE_STATUSES = ('ok', 'failed', 'locked', 'unsupported', 'too_large', 'encrypted', 'pending')
# Fixed parse_error codes per status (design §5.6, §5.3); no free text. 'failed' also takes
# 'exception:<ClassName>'. 'not_implemented' is used by the stage-0 parser stubs only.
ERROR_CODES = {
    'failed': frozenset({
        'encoding_unknown', 'binary_content', 'csv_columns', 'bad_zip', 'missing_part', 'xml_dtd', 'xml_invalid',
        'pdf_no_text', 'pdf_read_error', 'hwp_bad_record', 'hwp_bad_stream', 'ole_invalid', 'eml_no_headers',
        'unit_shape_invalid', 'unit_duplicate', 'not_implemented'}),
    'unsupported': frozenset({
        'pdf_bad_signature', 'bad_signature', 'legacy_ole', 'hwp_bad_header', 'hwp_version', 'ooxml_strict'}),
    'too_large': frozenset({
        'file_bytes', 'limit_units', 'limit_chars', 'limit_events', 'limit_memory', 'unit_chars',
        'zip_entries', 'zip_total', 'zip_part', 'hwp_para', 'xml_depth', 'xml_attributes', 'xml_markup',
        'xml_names', 'parse_time', 'pdf_count', 'pdf_stream', 'pptx_count', 'xlsx_count', 'eml_count',
        'hwp_count', 'ole_entries',
        # No longer raised, kept so that stored rows stay valid codes: xml_tree (pptx-2, xlsx-2 trees) and
        # xml_text (one XML text node; unit_chars in every format now).
        'xml_tree', 'xml_text'}),
    'encrypted': frozenset({'pdf_encrypted', 'hwp_encrypted', 'ooxml_encrypted'}),
}
EXCEPTION_PREFIX = 'exception:'


def is_error_code(status: str, code: str) -> bool:
    if status not in ERROR_CODES or not isinstance(code, str):
        return False
    if status == 'failed' and code.startswith(EXCEPTION_PREFIX):
        return code[len(EXCEPTION_PREFIX):].isidentifier()
    return code in ERROR_CODES[status]


@dataclass(frozen=True, slots=True)  # no per-unit __dict__: up to 200,000 of them are kept
class Unit:
    ordinal: int  # 1-based document order (the dispatcher renumbers 1..N)
    kind: str     # a locator.UNIT_KINDS key, equal to unit['kind']
    unit: dict    # the locator 'unit' part, kind included; stored as unit_json(unit)
    text: str     # non-empty and already stripped; inner whitespace and line breaks kept


@dataclass(frozen=True)
class Limits:
    """Per-version limits (design §5.3). ``config.[scan]`` and tests override the defaults."""
    max_file_bytes: int = 100 * 2**20
    max_units: int = 200_000
    max_text_chars: int = 50_000_000
    csv_max_columns: int = 1_000
    pdf_max_pages: int = 2_000
    pptx_max_slides: int = 2_000
    xlsx_max_sheets: int = 1_000
    eml_max_parts: int = 1_000
    hwp_max_sections: int = 1_000
    zip_max_entries: int = 10_000
    zip_max_part_bytes: int = 128 * 2**20
    zip_max_total_bytes: int = 512 * 2**20
    # Every XML part is streamed (_container.XmlStream, no tree); these bound its structure (design M1 §15):
    # nesting depth (Office and HWPX documents stay well under 100; libxml2 refuses more than 256 by
    # default), attributes plus namespace declarations of one element (a Word root carries about 40), bytes
    # of one unfinished start tag, comment or processing instruction (1 MiB; measured in 64 KiB pieces, so
    # one between 1 MiB and 1.125 MiB may pass depending on where it starts) and distinct element,
    # attribute and namespace names in one part (a few hundred in real parts).
    xml_max_depth: int = 256
    xml_max_attributes: int = 1_000
    xml_max_markup_bytes: int = 2**20
    xml_max_names: int = 10_000
    # The text of one unit in every format (paragraph, cell, shape, page, line) and of one XML text node:
    # 8 Mi characters (too_large/unit_chars).
    max_unit_chars: int = 8 * 2**20
    # One HWP PARA_TEXT record (UTF-16LE, 2 bytes per character): 8M characters.
    hwp_max_para_bytes: int = 16 * 2**20
    # One file's budget besides text and units (parser resource design §2.3, M1 design §15.2):
    # - events (XML events, HWP records, lines, PDF operators): the slowest kind measured here is an
    #   empty XLSX cell, 0.78 us an event, so 30 M events take about 23 s, under half of parse_seconds; a
    #   PC twice as slow still refuses such a file with limit_events, the same code, before the clock;
    # - bytes of the tables kept until the end of the parse (shared strings at their real str size,
    #   relationship targets and names at 256 bytes an object and 4 a character). 256 MiB holds every
    #   shared string a workbook within max_units and max_text_chars can use (4 bytes a character at most,
    #   about 84 bytes a string), so this limit is never reached before those two.
    max_events: int = 30_000_000
    max_held_bytes: int = 256 * 2**20
    # Wall-clock seconds for one file's parse, the dispatcher's checks included; over it is
    # too_large/parse_time. Unlike every other limit it depends on the machine (see binding 'timeout').
    parse_seconds: float = 60.0


class ParseFailure(Exception):
    """A parse outcome other than ok, with a fixed code. Raising an unknown code is a bug (ValueError)."""

    def __init__(self, status: str, code: str):
        if not is_error_code(status, code):
            raise ValueError(f'unknown parse status/code {status!r}/{code!r}')
        super().__init__(f'{status}/{code}')
        self.status = status
        self.code = code


@dataclass(frozen=True)
class ParseResult:
    parser: str             # 'text'|'csv'|'pdf'|'docx'|'pptx'|'xlsx'|'eml'|'hwp'|'hwpx'
    parser_version: str     # design §5.5
    encoding: str | None
    status: str             # a versions.parse_status value
    error: str | None       # a parse_error code; None when status is 'ok'
    units: tuple[Unit, ...]


@dataclass(frozen=True)
class Parser:
    name: str
    version: str
    parse: Callable[..., tuple[list[Unit], str | None]]  # (data, limits, budget)


def unit_json(unit: dict) -> str:
    """The stored form of a unit locator (``units.locator_json``); compare locators in this form."""
    return json.dumps(unit, sort_keys=True, ensure_ascii=False)


def parser_for(ext: str) -> Parser:
    """Parser of a supported extension (case-insensitive); ValueError for any other."""
    from . import docx, eml, hwp, pdf, pptx, text, xlsx
    ext = ext.lower()
    if ext in ('.txt', '.md'):
        return Parser(text.NAME, text.VERSION, text.parse)
    if ext == '.csv':
        return Parser(text.CSV_NAME, text.CSV_VERSION, text.parse_csv)
    if ext == '.hwpx':
        return Parser(hwp.HWPX_NAME, hwp.HWPX_VERSION, hwp.parse_hwpx)
    modules = {'.pdf': pdf, '.docx': docx, '.pptx': pptx, '.xlsx': xlsx, '.eml': eml, '.hwp': hwp}
    if ext not in modules:
        raise ValueError(f'unsupported extension {ext!r}')
    module = modules[ext]
    return Parser(module.NAME, module.VERSION, module.parse)


def sniff(data: bytes, ext: str) -> tuple[str, str] | None:
    """``(status, code)`` when the bytes do not match the extension (design §5.2), else None."""
    ext = ext.lower()
    if ext == '.pdf':
        return None if data.startswith(PDF_SIGNATURE) else ('unsupported', 'pdf_bad_signature')
    if ext in ZIP_EXTENSIONS:
        if data[:4] in ZIP_SIGNATURES:
            return None
        if data.startswith(OLE_SIGNATURE):
            from . import _container
            streams = _container.ole_streams(data)
            if streams is None:
                return ('failed', 'ole_invalid')
            return ('encrypted', 'ooxml_encrypted') if 'EncryptedPackage' in streams else ('unsupported', 'legacy_ole')
        return ('unsupported', 'bad_signature')
    if ext == '.hwp':
        # The 32-byte 'HWP Document File' signature inside FileHeader is checked by the hwp parser.
        if data.startswith(OLE_SIGNATURE):
            from . import _container
            streams = _container.ole_streams(data)
            if streams is not None and 'FileHeader' in streams:
                return None
        return ('unsupported', 'hwp_bad_header')
    return None


def _unit_key(unit) -> str | None:
    """``unit_json`` of a well-formed unit, None for a malformed one."""
    if not isinstance(unit, Unit) or not isinstance(unit.text, str) or not unit.text \
            or unit.text != unit.text.strip() or not isinstance(unit.unit, dict) or unit.unit.get('kind') != unit.kind:
        return None
    try:
        locator.check_unit(unit.unit)
        unit.text.encode('utf-8')  # a lone surrogate cannot be stored or hashed
        return unit_json(unit.unit)
    except (locator.LocatorError, TypeError, ValueError):
        return None


def parse(data: bytes, ext: str, limits: Limits) -> ParseResult:
    """Parse one version's bytes; every content problem is a status, never an exception."""
    parser = parser_for(ext)

    def result(status, error=None, units=(), encoding=None):
        return ParseResult(parser.name, parser.version, encoding, status, error, tuple(units))

    if not data:
        return result('ok')  # an empty file is a valid observation with nothing to read
    if len(data) > limits.max_file_bytes:
        return result('too_large', 'file_bytes')
    from . import _container
    if (ext.lower() == '.hwp' or ext.lower() in ZIP_EXTENSIONS) and data.startswith(OLE_SIGNATURE):
        # olefile makes an object per directory entry before anything can be checked: count them first.
        entries = _container.ole_entries(data, limits.zip_max_entries)
        if entries == _container.BAD_SECTOR_SIZE:
            return result('failed', 'ole_invalid')
        if entries is not None and entries > limits.zip_max_entries:
            return result('too_large', 'ole_entries')
    if mismatch := sniff(data, ext):
        return result(*mismatch)
    budget = _container.Budget(limits)
    try:
        units, encoding = parser.parse(data, limits, budget)
        units = list(units)
        problem = _check(units, encoding, limits, budget)
    except ParseFailure as failure:
        return result(failure.status, failure.code)
    except Exception as exc:  # message dropped on purpose: it may quote document text
        return result('failed', EXCEPTION_PREFIX + type(exc).__name__)
    if problem:
        return result(*problem)
    units = [replace(unit, ordinal=number) for number, unit in enumerate(units, 1)]
    return result('ok', units=units, encoding=encoding)


def _check(units: list, encoding, limits: Limits, budget) -> tuple[str, str] | None:
    """The dispatcher's checks of a parser's result, on the parse's clock (read every 1,024 units).
    Locators are compared by their digest, so no second copy of every locator is kept."""
    seen, shape_ok, duplicate, chars = set(), encoding is None or isinstance(encoding, str), False, 0
    for number, unit in enumerate(units, 1):
        if not number & 1023:
            budget.clock()
        key = _unit_key(unit)
        if key is None:
            shape_ok = False
            continue
        digest = hashlib.sha256(key.encode('utf-8')).digest()
        duplicate = duplicate or digest in seen
        seen.add(digest)
        chars += len(unit.text)
    if not shape_ok:
        return 'failed', 'unit_shape_invalid'
    if duplicate:
        return 'failed', 'unit_duplicate'
    # A second line of defence: every parser charges the budget, so these hold already.
    if len(units) > limits.max_units:
        return 'too_large', 'limit_units'
    if chars > limits.max_text_chars:
        return 'too_large', 'limit_chars'
    return None

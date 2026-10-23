"""Parser dispatch and the types every parser shares (design M1 §5.1-§5.6).

Contract of a parser module (``text``, ``eml``, ``docx``, ``pptx``, ``xlsx``, ``pdf``, ``hwp``):

- ``NAME: str`` and ``VERSION: str`` (``"<name>-<n>"``, plus ``"/<lib>-<ver>"`` when a third-party
  library decides the output, design §5.5). A module that reads two formats also has
  ``CSV_NAME``/``CSV_VERSION``/``parse_csv`` (``text``) or ``HWPX_NAME``/``HWPX_VERSION``/``parse_hwpx``
  (``hwp``).
- ``parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]``: the units in document
  order and the encoding (``None`` when the format declares its own). It gets bytes only, never a
  path, and raises only ``ParseFailure``. Any other exception becomes ``failed/exception:<Class>``;
  its message is never stored or logged (it may carry document text).

``parse(data, ext, limits)`` here never raises for bad content: it checks the signature
(``sniff``), calls the module, renumbers ordinals 1..N, checks every unit with
``locator.check_unit``, refuses two units with the same locator in one version
(``failed/unit_duplicate``: binding and locator checks look a unit up by its locator) and applies
the unit and text limits. Same bytes, same limits and same parser version give the same result
(binding depends on it).
"""
from __future__ import annotations

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
        'file_bytes', 'limit_units', 'limit_chars', 'zip_entries', 'zip_total', 'zip_part', 'xml_tree', 'hwp_para',
        'pdf_count', 'pptx_count', 'xlsx_count', 'eml_count', 'hwp_count'}),
    'encrypted': frozenset({'pdf_encrypted', 'hwp_encrypted', 'ooxml_encrypted'}),
}
EXCEPTION_PREFIX = 'exception:'


def is_error_code(status: str, code: str) -> bool:
    if status not in ERROR_CODES or not isinstance(code, str):
        return False
    if status == 'failed' and code.startswith(EXCEPTION_PREFIX):
        return code[len(EXCEPTION_PREFIX):].isidentifier()
    return code in ERROR_CODES[status]


@dataclass(frozen=True)
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
    # An XML part read as a whole tree (PPTX slides, the workbook and relationship parts) takes about 20 times
    # its size in memory, so it has its own small limit. The large parts (DOCX body, HWPX sections, XLSX
    # sheets and shared strings) are streamed and only bounded by zip_max_part_bytes.
    xml_max_tree_bytes: int = 16 * 2**20
    # One HWP PARA_TEXT record (UTF-16LE, 2 bytes per character): 8M characters.
    hwp_max_para_bytes: int = 16 * 2**20


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
    parse: Callable[[bytes, Limits], tuple[list[Unit], str | None]]


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
    if mismatch := sniff(data, ext):
        return result(*mismatch)
    try:
        units, encoding = parser.parse(data, limits)
        units = list(units)
    except ParseFailure as failure:
        return result(failure.status, failure.code)
    except Exception as exc:  # message dropped on purpose: it may quote document text
        return result('failed', EXCEPTION_PREFIX + type(exc).__name__)
    keys = [_unit_key(unit) for unit in units]
    if None in keys or not (encoding is None or isinstance(encoding, str)):
        return result('failed', 'unit_shape_invalid')
    if len(set(keys)) != len(keys):
        return result('failed', 'unit_duplicate')
    if len(units) > limits.max_units:
        return result('too_large', 'limit_units')
    if sum(len(unit.text) for unit in units) > limits.max_text_chars:
        return result('too_large', 'limit_chars')
    units = [replace(unit, ordinal=number) for number, unit in enumerate(units, 1)]
    return result('ok', units=units, encoding=encoding)

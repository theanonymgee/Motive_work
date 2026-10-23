"""Parser dispatch, shared types and the fixed status/code sets (design M1 §5.1-§5.6).

The format parsers are tested by their own files (``test_parsers_*.py``); here only the
dispatcher contract is checked, with a fake parser standing in where a case needs one.
"""
import importlib.metadata
import os
import re
from dataclasses import replace

import pytest

from fixtures import SENTINEL, bundle, cfb
from workmcp import parsers, store
from workmcp.parsers import _container
from workmcp.parsers import Limits, ParseFailure, Parser, ParseResult, Unit

PYPDF = importlib.metadata.version('pypdf')
OLEFILE = importlib.metadata.version('olefile')
EXPECTED_PARSERS = {
    '.txt': ('text', 'text-3'), '.md': ('text', 'text-3'), '.csv': ('csv', 'csv-3'),
    '.pdf': ('pdf', f'pdf-4/pypdf-{PYPDF}'), '.docx': ('docx', 'docx-5'), '.pptx': ('pptx', 'pptx-5'),
    '.xlsx': ('xlsx', 'xlsx-5'), '.eml': ('eml', 'eml-4'), '.hwp': ('hwp', f'hwp-5/olefile-{OLEFILE}'),
    '.hwpx': ('hwpx', 'hwpx-5')}
SIGNED = {'.pdf': b'%PDF-1.4\n', '.docx': b'PK\x03\x04', '.pptx': b'PK\x03\x04', '.xlsx': b'PK\x05\x06',
          '.hwpx': b'PK\x03\x04'}


def test_every_supported_extension_has_a_parser_named_by_the_design():
    assert set(EXPECTED_PARSERS) == parsers.SUPPORTED_EXTENSIONS
    for ext, (name, version) in EXPECTED_PARSERS.items():
        parser = parsers.parser_for(ext)
        assert (parser.name, parser.version) == (name, version)
        assert parsers.parser_for(ext.upper()) == parser
    assert PYPDF == '6.10.0' and OLEFILE == '0.47'  # the pinned versions (pyproject.toml)


@pytest.mark.parametrize('ext', ['.doc', '.xls', '.msg', '.png', '', 'txt'])
def test_unsupported_extension_is_a_caller_error(ext):
    with pytest.raises(ValueError):
        parsers.parser_for(ext)


@pytest.mark.parametrize('ext', sorted(EXPECTED_PARSERS))
def test_every_parser_turns_junk_into_a_fixed_status(ext):
    """Signed but truncated or random bytes: a ParseResult with a fixed code, never an exception."""
    result = parsers.parse(SIGNED.get(ext, b'synthetic \xff\xfe junk'), ext, Limits())
    name, version = EXPECTED_PARSERS[ext]
    assert (result.parser, result.parser_version) == (name, version)
    assert result.status in parsers.PARSE_STATUSES
    assert result.error is None if result.status == 'ok' else parsers.is_error_code(result.status, result.error)
    assert result.error != 'not_implemented'


@pytest.mark.parametrize('ext', sorted(EXPECTED_PARSERS))
def test_empty_file_is_ok_with_no_units(ext):
    result = parsers.parse(b'', ext, Limits())
    assert (result.status, result.error, result.units) == ('ok', None, ())


def test_file_size_limit():
    result = parsers.parse(b'12345', '.txt', Limits(max_file_bytes=4))
    assert (result.status, result.error, result.units) == ('too_large', 'file_bytes', ())
    at_limit = parsers.parse(b'1234', '.txt', Limits(max_file_bytes=4))
    assert (at_limit.status, [unit.text for unit in at_limit.units]) == ('ok', ['1234'])


OLE_JUNK = parsers.OLE_SIGNATURE + b'\x00' * 1024


@pytest.mark.parametrize('ext, data, expected', [
    ('.pdf', b'%PDF-1.7', None),
    ('.pdf', b'PK\x03\x04', ('unsupported', 'pdf_bad_signature')),
    ('.pdf', b' %PDF-1.7', ('unsupported', 'pdf_bad_signature')),
    ('.docx', b'PK\x03\x04rest', None),
    ('.xlsx', b'PK\x05\x06', None),
    ('.pptx', b'{\\rtf1 synthetic}', ('unsupported', 'bad_signature')),
    ('.hwpx', b'%PDF-1.4', ('unsupported', 'bad_signature')),
    ('.docx', OLE_JUNK, ('failed', 'ole_invalid')),
    ('.hwp', b'PK\x03\x04', ('unsupported', 'hwp_bad_header')),
    ('.hwp', OLE_JUNK, ('unsupported', 'hwp_bad_header')),
    ('.txt', b'\x00\x01', None), ('.md', b'%PDF-', None), ('.csv', b'PK\x03\x04', None), ('.eml', b'', None),
    ('.PDF', b'x', ('unsupported', 'pdf_bad_signature')),
])
def test_sniff(ext, data, expected):
    assert parsers.sniff(data, ext) == expected


OLE_CASES = bundle.ole_cases()


@pytest.mark.parametrize('item, data', OLE_CASES, ids=[item.rel_path for item, _ in OLE_CASES])
def test_ole_container_behind_an_ooxml_extension(item, data, monkeypatch):
    """Encrypted OOXML packages and old binary Office files renamed to .docx/.xlsx/.pptx/.hwpx (design §5.2)
    are judged by the dispatcher from the OLE streams; the format parser is never called."""
    ext = os.path.splitext(item.rel_path)[1]
    assert parsers.sniff(data, ext) == (item.status, item.error)
    result = parsers.parse(data, ext, Limits())
    assert (result.parser, result.status, result.error, result.units, result.encoding) == (
        item.parser, item.status, item.error, (), None)
    real = parsers.parser_for(ext)
    monkeypatch.setattr(parsers, 'parser_for', lambda e: Parser(real.name, real.version, lambda d, l: 1 / 0))
    assert parsers.parse(data, ext, Limits()).error == item.error  # not 'exception:ZeroDivisionError'


def test_ole_cases_are_real_compound_files():
    data = {item.rel_path: payload for item, payload in OLE_CASES}
    assert _container.ole_streams(data['edge/ooxml_encrypted.docx']) == {'EncryptionInfo', 'EncryptedPackage'}
    assert _container.ole_streams(data['edge/legacy_word.docx']) == {'WordDocument', '1Table', 'CompObj'}
    assert _container.ole_streams(data['edge/legacy_powerpoint.pptx']) == {'PowerPoint Document', 'Current User'}
    assert _container.ole_streams(data['edge/ole_damaged.docx']) is None
    assert all(payload.startswith(cfb.SIGNATURE) for payload in data.values())


def test_ooxml_encryption_is_judged_by_the_stream_whatever_the_extension():
    encrypted = cfb.write_cfb({'EncryptionInfo': b'\x04\x00synthetic', 'EncryptedPackage': b'synthetic' * 600})
    for ext in ('.docx', '.pptx', '.xlsx', '.hwpx', '.DOCX'):
        assert parsers.sniff(encrypted, ext) == ('encrypted', 'ooxml_encrypted')
    assert parsers.sniff(encrypted, '.hwp') == ('unsupported', 'hwp_bad_header')   # OLE, but no FileHeader
    assert parsers.sniff(encrypted, '.pdf') == ('unsupported', 'pdf_bad_signature')
    nested = cfb.write_cfb({'Storage/EncryptedPackage': b'synthetic'})
    assert parsers.sniff(nested, '.docx') == ('unsupported', 'legacy_ole')  # only a root stream marks encryption
    empty_stream = cfb.write_cfb({'WordDocument': b''})
    assert parsers.sniff(empty_stream, '.docx') == ('unsupported', 'legacy_ole')


def test_signature_mismatch_is_reported_without_calling_the_parser(monkeypatch):
    called = []
    fake(monkeypatch, lambda data, limits, budget: called.append(1) or ([], None))
    result = parsers.parse(b'not a pdf', '.pdf', Limits())
    assert (result.status, result.error, called) == ('unsupported', 'pdf_bad_signature', [])


def line(n, text):
    return Unit(n, 'line', {'kind': 'line', 'line': n}, text)


def fake(monkeypatch, parse, name='fake', version='fake-1'):
    monkeypatch.setattr(parsers, 'parser_for', lambda ext: Parser(name, version, parse))


def test_ordinals_are_renumbered_and_units_kept(monkeypatch):
    fake(monkeypatch, lambda data, limits, budget: ([line(7, '첫 줄'), line(9, 'second')], 'utf-8'))
    result = parsers.parse(b'synthetic', '.txt', Limits())
    assert result.status == 'ok' and result.error is None and result.encoding == 'utf-8'
    assert result.units == (Unit(1, 'line', {'kind': 'line', 'line': 7}, '첫 줄'),
                            Unit(2, 'line', {'kind': 'line', 'line': 9}, 'second'))
    assert (result.parser, result.parser_version) == ('fake', 'fake-1')


@pytest.mark.parametrize('bad', [
    Unit(1, 'line', {'kind': 'line', 'line': 1}, ''),
    Unit(1, 'line', {'kind': 'line', 'line': 1}, ' padded '),
    Unit(1, 'line', {'kind': 'line', 'line': 1}, 'lone \ud800 surrogate'),
    Unit(1, 'page', {'kind': 'line', 'line': 1}, 'kind mismatch'),
    Unit(1, 'line', {'kind': 'line', 'line': 0}, 'zero line'),
    Unit(1, 'line', {'kind': 'line'}, 'missing field'),
    Unit(1, 'line', {'kind': 'line', 'line': 1, 'extra': 2}, 'extra field'),
    Unit(1, 'sheet_cell', {'kind': 'sheet_cell', 'sheet': '시트1', 'cell': 'b12'}, 'bad cell address'),
    Unit(1, 'nope', {'kind': 'nope'}, 'unknown kind'),
    Unit(1, 'line', ['kind', 'line'], 'not a dict'),
    ('line', 1, 'not a Unit'),
], ids=lambda value: repr(value)[:40])
def test_bad_units_are_never_returned(monkeypatch, bad):
    fake(monkeypatch, lambda data, limits, budget: ([line(1, 'good'), bad], None))
    result = parsers.parse(b'synthetic', '.txt', Limits())
    assert (result.status, result.error, result.units) == ('failed', 'unit_shape_invalid', ())


def test_two_units_with_one_locator_are_refused(monkeypatch):
    """Review M1-L8: binding and the locator check find a unit by its locator, so one version must not repeat one."""
    twice = [Unit(1, 'section_para', {'kind': 'section_para', 'section': 1, 'index': 1}, 'first'),
             Unit(2, 'section_para', {'index': 1, 'section': 1, 'kind': 'section_para'}, 'second')]  # same unit_json
    fake(monkeypatch, lambda data, limits, budget: (twice, None))
    result = parsers.parse(b'synthetic', '.txt', Limits())
    assert (result.status, result.error, result.units) == ('failed', 'unit_duplicate', ())
    fake(monkeypatch, lambda data, limits, budget: ([twice[0], replace(twice[1], unit={**twice[1].unit, 'index': 2})], None))
    assert parsers.parse(b'synthetic', '.txt', Limits()).status == 'ok'


def test_dispatcher_post_processing_is_inside_the_clock(monkeypatch):
    """R2 (review v3 M1-5): the checks after the parser ran with no clock (250 s for 199,999 units of one
    huge shared string). Now they read the parse's clock every 1,024 units."""
    units = [line(n, 'x') for n in range(1, 10_001)]
    fake(monkeypatch, lambda data, limits, budget: (units, None))
    assert parsers.parse(b'synthetic', '.txt', Limits()).status == 'ok'
    reads = []

    def clock(self):
        reads.append(1)
        raise ParseFailure('too_large', 'parse_time')
    monkeypatch.setattr(_container.Budget, 'clock', clock)
    result = parsers.parse(b'synthetic', '.txt', Limits())
    assert (result.status, result.error, result.units) == ('too_large', 'parse_time', ()) and reads == [1]


def test_dispatcher_dedupes_by_digest(monkeypatch):
    """R3: duplicate locators are found by their digest; no list or set of locator strings is kept
    (1,000-column CSV locators are about 5 KB each)."""
    import tracemalloc
    columns = list(range(1, 1001))
    units = [Unit(n, 'cell', {'kind': 'cell', 'row': n, 'columns': columns}, 'x') for n in range(1, 5_001)]
    fake(monkeypatch, lambda data, limits, budget: (units + [units[0]], None))
    parsers.parse(b'warm up', '.md', Limits())
    tracemalloc.start()
    try:
        result = parsers.parse(b'synthetic', '.txt', Limits())
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert (result.status, result.error) == ('failed', 'unit_duplicate') and peak < len(units) * 300  # about 200 B a unit, was 5 KB
    fake(monkeypatch, lambda data, limits, budget: (units, None))
    assert parsers.parse(b'synthetic', '.txt', Limits()).status == 'ok'


def test_encoding_must_be_text_or_none(monkeypatch):
    fake(monkeypatch, lambda data, limits, budget: ([line(1, 'good')], b'utf-8'))
    assert parsers.parse(b'synthetic', '.txt', Limits()).error == 'unit_shape_invalid'


def test_unit_and_text_limits(monkeypatch):
    fake(monkeypatch, lambda data, limits, budget: ([line(n, 'x' * 10) for n in range(1, 4)], None))
    assert parsers.parse(b'synthetic', '.txt', Limits(max_units=3)).status == 'ok'
    over_units = parsers.parse(b'synthetic', '.txt', Limits(max_units=2))
    over_chars = parsers.parse(b'synthetic', '.txt', Limits(max_text_chars=29))
    assert (over_units.status, over_units.error, over_units.units) == ('too_large', 'limit_units', ())
    assert (over_chars.status, over_chars.error) == ('too_large', 'limit_chars')
    assert parsers.parse(b'synthetic', '.txt', Limits(max_text_chars=30)).status == 'ok'


def test_parse_failure_passes_through(monkeypatch):
    def parse(data, limits, budget):
        raise ParseFailure('encrypted', 'pdf_encrypted')
    fake(monkeypatch, parse)
    result = parsers.parse(b'%PDF-1.4', '.pdf', Limits())
    assert (result.status, result.error, result.units, result.encoding) == ('encrypted', 'pdf_encrypted', (), None)


def test_other_exceptions_keep_only_the_class_name(monkeypatch):
    """Library messages may quote document text; only the class name is kept."""
    def parse(data, limits, budget):
        raise RuntimeError(f'cannot read {SENTINEL} near offset 12')
    fake(monkeypatch, parse)
    result = parsers.parse(b'synthetic', '.txt', Limits())
    assert (result.status, result.error) == ('failed', 'exception:RuntimeError')
    assert SENTINEL not in repr(result)


def test_parser_returning_the_wrong_shape_is_a_failure(monkeypatch):
    fake(monkeypatch, lambda data, limits, budget: [line(1, 'units without encoding')])
    assert parsers.parse(b'synthetic', '.txt', Limits()).error.startswith('exception:')


def test_dispatch_is_deterministic(monkeypatch):
    fake(monkeypatch, lambda data, limits, budget: ([line(n, f'{SENTINEL} {n}') for n in (3, 1, 2)], 'cp949'))
    first, second = (parsers.parse(b'synthetic', '.txt', Limits()) for _ in range(2))
    assert first == second


@pytest.mark.parametrize('status, code', [
    ('failed', 'free text message'), ('failed', 'exception:'), ('failed', 'exception:not a name'),
    ('ok', 'not_implemented'), ('locked', 'x'), ('too_large', 'pdf_encrypted'), ('encrypted', 'zip_part'),
    ('unsupported', None)])
def test_parse_failure_refuses_codes_outside_the_fixed_set(status, code):
    with pytest.raises(ValueError):
        ParseFailure(status, code)


def test_fixed_code_sets():
    assert ParseFailure('failed', 'exception:KeyError').code == 'exception:KeyError'
    all_codes = [code for codes in parsers.ERROR_CODES.values() for code in codes]
    assert len(all_codes) == len(set(all_codes))  # a code belongs to one status
    assert all(re.fullmatch(r'[a-z][a-z0-9_]*', code) for code in all_codes)
    assert set(parsers.ERROR_CODES) <= set(parsers.PARSE_STATUSES)


def test_parse_statuses_match_the_store_check():
    check = re.search(r"parse_status TEXT NOT NULL CHECK\(parse_status IN \(([^)]*)\)\)", store.SCHEMA_V1)
    assert tuple(value.strip("'") for value in check.group(1).split(',')) == parsers.PARSE_STATUSES


def test_unit_json_is_the_canonical_stored_form():
    unit = {'line': 3, 'kind': 'line'}
    assert parsers.unit_json(unit) == '{"kind": "line", "line": 3}'
    assert parsers.unit_json({'kind': 'sheet_cell', 'sheet': '시트1', 'cell': 'B2'}) == \
        '{"cell": "B2", "kind": "sheet_cell", "sheet": "시트1"}'


def test_limits_defaults_follow_the_design():
    assert Limits() == Limits(max_file_bytes=100 * 2**20, max_units=200_000, max_text_chars=50_000_000,
                              csv_max_columns=1_000, pdf_max_pages=2_000, pptx_max_slides=2_000,
                              xlsx_max_sheets=1_000, eml_max_parts=1_000, hwp_max_sections=1_000,
                              zip_max_entries=10_000, zip_max_part_bytes=128 * 2**20,
                              zip_max_total_bytes=512 * 2**20, xml_max_depth=256, xml_max_attributes=1_000,
                              xml_max_markup_bytes=2**20, xml_max_names=10_000, max_unit_chars=8 * 2**20,
                              hwp_max_para_bytes=16 * 2**20, max_events=30_000_000,
                              max_held_bytes=256 * 2**20, parse_seconds=60.0)
    assert Limits().max_unit_chars == Limits().hwp_max_para_bytes // 2  # one unit's text, the same in every format
    with pytest.raises(AttributeError):
        Limits().max_units = 1  # frozen

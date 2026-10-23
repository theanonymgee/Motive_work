"""PDF, HWP 5.x and HWPX parsers, their synthetic generators and the CFB writer (design M1 §5.8, §7.3, §7.4).

Every document is generated here at test time (``tests/fixtures/make_pdf.py``, ``make_hwp.py``,
``cfb.py``); nothing binary is committed.
"""
import gc
import io
import logging
import random
import re
import struct
import time
import tracemalloc
import types
import warnings
import zipfile
from pathlib import Path

import olefile
import pytest

import fixtures
from fixtures import cfb, make_hwp, make_pdf
from workmcp import locator, parsers
from workmcp.parsers import Limits, ParseFailure, _container, hwp, pdf

CASES = {**make_pdf.cases(), **make_hwp.cases()}
DEFAULT_LIMITS = Limits()
BUDGET = _container.Budget(Limits(max_events=2**62, parse_seconds=1e9))  # for direct calls of helpers


def run(data: bytes, ext: str, limits: Limits = DEFAULT_LIMITS):
    return parsers.parse(data, ext, limits)


def outcome(result) -> tuple[str, str | None]:
    return result.status, result.error


def traced_peak(data: bytes, ext: str, limits: Limits):
    """``(status, error, unit count, peak bytes)``: tracemalloc peak of one dispatcher call.

    The input is built and the parser modules are imported before tracing starts, so the peak is what
    parsing allocates (streams, decompressed sections, events, text, units).
    """
    run(b'warm up', '.txt')
    gc.collect()
    tracemalloc.start()
    try:
        result = run(data, ext, limits)
        return result.status, result.error, len(result.units), tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def texts(result) -> list[str]:
    return [unit.text for unit in result.units]


def read_cfb(data: bytes) -> dict[str, bytes]:
    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        return {'/'.join(path): ole.openstream(path).read() for path in ole.listdir(streams=True, storages=False)}


# CFB writer
BIG = bytes(random.Random(1).randrange(256) for _ in range(150_000))
CFB_LAYOUTS = {
    'one_small_stream': {'a': b'hello'},
    'empty_stream': {'empty': b'', 'a': b'x'},
    'nested_storages': {'BodyText/Section0': b'0' * 100, 'BodyText/Section1': b'1' * 5000, 'Info': {'deep': {'x': b'y'}}},
    'mini_boundary': {'m4095': b'a' * 4095, 'r4096': b'b' * 4096, 'm1': b'c', 'm64': b'd' * 64, 'm65': b'e' * 65},
    'spans_many_fat_sectors': {'big': BIG, 'small': b'tail'},
    'many_entries': {f'stream{n:03d}': bytes([n]) * (n * 37) for n in range(60)},
    'name_order': {'B': b'1', 'aa': b'2', 'A': b'3', 'ab': b'4', 'Z' * 31: b'5', '한글': b'6'},
}


def flatten(entries: dict, prefix: str = '') -> dict[str, bytes]:
    flat = {}
    for key, value in entries.items():
        if isinstance(value, dict):
            flat.update(flatten(value, f'{prefix}{key}/'))
        else:
            flat[prefix + key] = value
    return flat


@pytest.mark.parametrize('layout', sorted(CFB_LAYOUTS))
def test_cfb_writer_roundtrips_through_olefile(layout):
    entries = CFB_LAYOUTS[layout]
    data = cfb.write_cfb(entries)
    assert data.startswith(parsers.OLE_SIGNATURE) and len(data) % cfb.SECTOR_SIZE == 0
    assert read_cfb(data) == flatten(entries)
    assert _container.ole_streams(data) == set(flatten(entries))


def test_cfb_writer_puts_small_streams_in_the_mini_stream():
    data = cfb.write_cfb({'small': b'a' * 100, 'large': b'b' * 5000})
    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        assert ole.get_size('small') == 100 and ole.get_size('large') == 5000
        assert ole.root.size > 0 and ole.root.size % cfb.MINI_SECTOR_SIZE == 0  # the mini stream container
        assert ole.minisectorcutoff == cfb.MINI_CUTOFF


@pytest.mark.parametrize('entries', [{'': b'x'}, {'a' * 32: b'x'}, {'a/b': b'x', 'a': b'y'}, {'a:b': b'x'}])
def test_cfb_writer_refuses_bad_names_and_conflicts(entries):
    with pytest.raises(ValueError):
        cfb.write_cfb(entries)


def test_cfb_writer_refuses_more_data_than_one_difat_header_holds():
    with pytest.raises(ValueError):
        cfb.write_cfb({'x': bytes(8 * 2**20)})


# generated documents, exactly as the bundle and the parsers see them
@pytest.mark.parametrize('rel', sorted(CASES))
def test_generated_documents_parse_as_expected(rel):
    data, expected = CASES[rel]
    assert expected.rel_path == rel
    result = run(data, Path(rel).suffix)
    assert (result.parser, result.status, result.error) == (expected.parser, expected.status, expected.error)
    assert len(result.units) == expected.unit_count and result.encoding == expected.encoding
    assert [unit.ordinal for unit in result.units] == list(range(1, expected.unit_count + 1))
    for ordinal, (kind, unit, text) in expected.units_sample.items():
        got = result.units[ordinal - 1]
        assert (got.kind, got.unit, got.text) == (kind, unit, text), ordinal
    if expected.status != 'ok':
        assert result.units == ()


@pytest.mark.parametrize('rel', sorted(CASES))
def test_parsers_are_deterministic(rel):
    data, _ = CASES[rel]
    ext = Path(rel).suffix
    assert run(data, ext) == run(bytes(data), ext)


def test_generators_are_deterministic():
    again = {**make_pdf.cases(), **make_hwp.cases()}
    assert {rel: data for rel, (data, _) in again.items()} == {rel: data for rel, (data, _) in CASES.items()}


@pytest.mark.parametrize('rel', sorted(CASES))
def test_every_unit_passes_locator_check_unit(rel):
    data, _ = CASES[rel]
    for unit in run(data, Path(rel).suffix).units:
        assert locator.check_unit(unit.unit) is unit.unit
        assert unit.text == unit.text.strip() and unit.text and unit.kind == unit.unit['kind']


def test_parser_versions_follow_the_format():
    assert re.fullmatch(r'pdf-4/pypdf-\d+\.\d+\.\d+', pdf.VERSION) and pdf.NAME == 'pdf'
    assert re.fullmatch(r'hwp-5/olefile-\d+\.\d+', hwp.VERSION) and hwp.NAME == 'hwp'
    assert (hwp.HWPX_NAME, hwp.HWPX_VERSION) == ('hwpx', 'hwpx-5')
    assert parsers.parser_for('.pdf').version == pdf.VERSION
    assert parsers.parser_for('.hwp').version == hwp.VERSION
    assert parsers.parser_for('.HWPX').version == hwp.HWPX_VERSION


def test_no_document_text_reaches_stdout_stderr_or_logs(capsys, caplog):
    caplog.set_level(logging.DEBUG)
    broken_xref = wrong_pointer(make_pdf.latin_pdf([[fixtures.SENTINEL_ASCII]]))
    for rel, (data, _) in CASES.items():
        run(data, Path(rel).suffix)
    run(broken_xref, '.pdf')
    run(make_pdf.latin_pdf([[fixtures.SENTINEL_ASCII]])[:-120], '.pdf')
    captured = capsys.readouterr()
    everything = captured.out + captured.err + caplog.text
    assert fixtures.SENTINEL not in everything and fixtures.SENTINEL_ASCII not in everything
    assert not caplog.records


# PDF
def wrong_pointer(data: bytes) -> bytes:
    """Point the xref entry of object 1 at the wrong offset: pypdf recovers, and logs a warning about it."""
    assert b'0000000009 00000 n' in data
    return data.replace(b'0000000009 00000 n', b'0000000999 00000 n')


def test_pdf_pages_skip_empty_and_signature_encrypted_no_text():
    result = run(make_pdf.latin_pdf([['first page'], [], ['third page', 'second line']]), '.pdf')
    assert outcome(result) == ('ok', None)
    assert [(u.unit, u.text) for u in result.units] == [
        ({'kind': 'page', 'page': 1}, 'first page'), ({'kind': 'page', 'page': 3}, 'third page\nsecond line')]
    assert outcome(run(b'Not a PDF', '.pdf')) == ('unsupported', 'pdf_bad_signature')
    assert outcome(run(b'%PDF-1.4\n', '.pdf')) == ('failed', 'pdf_read_error')
    assert outcome(run(make_pdf.latin_pdf([[], []]), '.pdf')) == ('failed', 'pdf_no_text')
    assert outcome(run(make_pdf.latin_pdf([]), '.pdf')) == ('failed', 'pdf_no_text')
    assert outcome(run(make_pdf.latin_pdf([['secret words']], encrypt=True), '.pdf')) == ('encrypted', 'pdf_encrypted')


def test_pdf_escaped_characters_and_digits():
    result = run(make_pdf.latin_pdf([['a (b) \\ c', 'Total 12345.67 / 2031-03-04']]), '.pdf')
    assert texts(result) == ['a (b) \\ c\nTotal 12345.67 / 2031-03-04']


def test_pdf_korean_text_is_recovered_through_tounicode():
    lines = [list(fixtures.LINES_KO[:2]), [], list(fixtures.LINES_KO[2:])]
    result = run(make_pdf.korean_pdf(lines), '.pdf')
    assert outcome(result) == ('ok', None)
    assert [u.unit['page'] for u in result.units] == [1, 3]
    assert texts(result) == ['\n'.join(lines[0]), '\n'.join(lines[2])]


def test_pdf_page_limit():
    data = make_pdf.latin_pdf([['one'], ['two'], ['three']])
    assert outcome(run(data, '.pdf', Limits(pdf_max_pages=2))) == ('too_large', 'pdf_count')
    assert outcome(run(data, '.pdf', Limits(pdf_max_pages=3))) == ('ok', None)


def test_pdf_truncated_file_is_a_read_error():
    data = make_pdf.latin_pdf([['one'], ['two']])
    for cut in (20, 300, len(data) // 2, len(data) - 30):
        assert outcome(run(data[:cut], '.pdf')) == ('failed', 'pdf_read_error'), cut


def test_pdf_with_a_wrong_object_offset_is_still_read():
    data = wrong_pointer(make_pdf.latin_pdf([['recovered text']]))
    assert texts(run(data, '.pdf')) == ['recovered text']


def test_pypdf_warnings_are_kept_out_of_logs_and_the_control_does_log(monkeypatch, caplog):
    data = wrong_pointer(make_pdf.latin_pdf([['recovered text']]))
    caplog.set_level(logging.DEBUG)
    assert texts(run(data, '.pdf')) == ['recovered text']
    assert not caplog.records
    logger = logging.getLogger('pypdf')
    monkeypatch.setattr(logger, 'propagate', True)  # control: with the default settings pypdf does log
    logger.setLevel(logging.WARNING)
    try:
        assert texts(run(data, '.pdf')) == ['recovered text']
    finally:
        logger.setLevel(logging.CRITICAL)
    assert caplog.records and all(record.name.startswith('pypdf') for record in caplog.records)


# HWP 5.x
def test_hwp_paragraphs_sections_compressed_and_flags():
    sections = [['첫 문단', 'second paragraph'], ['셋째 문단']]
    for compressed in (True, False):
        result = run(make_hwp.hwp_bytes(sections, compressed=compressed), '.hwp')
        assert outcome(result) == ('ok', None)
        assert [(u.unit['section'], u.unit['index'], u.text) for u in result.units] == [
            (1, 1, '첫 문단'), (1, 2, 'second paragraph'), (2, 1, '셋째 문단')]
    assert outcome(run(make_hwp.hwp_bytes(sections, flags=make_hwp.FLAG_ENCRYPTED), '.hwp')) == (
        'encrypted', 'hwp_encrypted')
    assert outcome(run(make_hwp.hwp_bytes(sections, flags=make_hwp.FLAG_DISTRIBUTION), '.hwp')) == (
        'encrypted', 'hwp_encrypted')
    both = make_hwp.FLAG_ENCRYPTED | make_hwp.FLAG_DISTRIBUTION
    assert outcome(run(make_hwp.hwp_bytes(sections, flags=both, compressed=False), '.hwp')) == (
        'encrypted', 'hwp_encrypted')
    assert outcome(run(make_hwp.hwp_bytes([]), '.hwp')) == ('failed', 'hwp_bad_stream')  # no BodyText section at all


def test_hwp_text_rules_controls_tab_newline_and_surrogates():
    result = run(make_hwp.hwp_bytes([['항목\t값', '줄 하나\n줄 둘', '  ', '이모지 \U0001F600 끝', 'A\x18B']]), '.hwp')
    assert [(u.unit['index'], u.text) for u in result.units] == [
        (1, '항목값'), (2, '줄 하나\n줄 둘'), (4, '이모지 \U0001F600 끝'), (5, 'AB')]


def test_hwp_extended_record_size_and_large_streams():
    long_text = ('가나다라마바사 ' * 600).strip()
    assert len(long_text.encode('utf-16-le')) > 0xFFF
    result = run(make_hwp.hwp_bytes([[long_text, 'after']], compressed=False), '.hwp')
    assert texts(result) == [long_text, 'after']
    many = [f'{n:04d} {fixtures.LINES_KO[n % 5]}' for n in range(400)]
    plain = make_hwp.hwp_bytes([many], compressed=False)  # BodyText stream well over 4096 bytes: regular sectors
    noise = random.Random(7)
    scattered = [''.join(chr(noise.randrange(0xAC00, 0xD7A4)) for _ in range(40)) for _ in range(300)]
    packed = make_hwp.hwp_bytes([scattered])  # still over 4096 bytes after deflate
    for data, expected in ((plain, many), (packed, scattered)):
        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            assert ole.get_size('BodyText/Section0') > cfb.MINI_CUTOFF
        assert texts(run(data, '.hwp')) == expected


def test_hwp_sections_are_read_in_numeric_order_and_empty_text_keeps_the_index():
    def stream(texts_):
        return make_hwp.deflate(make_hwp.section_stream(texts_))
    streams = {'FileHeader': make_hwp.file_header(make_hwp.FLAG_COMPRESSED),
               'BodyText/Section10': stream(['ten']), 'BodyText/Section2': stream(['two', '']),
               'BodyText/Section0': stream(['zero', None, 'zero b']), 'BodyText/Notes': b'ignored',
               'Scripts/Section0': b'ignored', 'BodyText/Section3x': b'ignored'}
    result = run(cfb.write_cfb(streams), '.hwp')
    assert [(u.unit['section'], u.unit['index'], u.text) for u in result.units] == [
        (1, 1, 'zero'), (1, 2, 'zero b'), (3, 1, 'two'), (11, 1, 'ten')]


def test_hwp_only_plain_decimal_stream_names_are_sections():
    """Review M1-L8: ``Section0`` and ``Section00`` would both be section 1 and repeat its locators."""
    def stream(texts_):
        return make_hwp.deflate(make_hwp.section_stream(texts_))
    streams = {'FileHeader': make_hwp.file_header(make_hwp.FLAG_COMPRESSED), 'BodyText/Section0': stream(['zero']),
               'BodyText/Section00': stream(['not a section']), 'BodyText/Section01': stream(['not either'])}
    result = run(cfb.write_cfb(streams), '.hwp')
    assert [(u.unit, u.text) for u in result.units] == [({'kind': 'section_para', 'section': 1, 'index': 1}, 'zero')]


def reference_para_text(raw: bytes) -> str:
    """The hwp-1 rule, one integer per WCHAR: the bytes-based reader must give the same text."""
    chars = struct.unpack('<%dH' % (len(raw) // 2), raw[:len(raw) // 2 * 2])
    units, i = [], 0
    while i < len(chars):
        c = chars[i]
        if c in hwp._LONG_CONTROLS:
            i += 8
            continue
        if c in (10, 13):
            units.append(10)
        elif c >= 32:
            units.append(c)
        i += 1
    return struct.pack('<%dH' % len(units), *units).decode('utf-16-le', errors='replace')


def test_hwp_para_text_matches_the_per_character_rule():
    # U+0400 then U+AC00 is 00 04 00 AC: the bytes 04 00 look like a control when read from an odd offset.
    assert hwp._para_text('\u0400\uac00'.encode('utf-16-le'), BUDGET) == '\u0400\uac00'
    noise = random.Random(11)
    alphabet = [0, 1, 2, 9, 10, 13, 24, 31, 32, 0x41, 0x400, 0x1F00, 0xAC00, 0xD800, 0xDC00, 0xD83D, 0xDE00, 0xFFFF]
    for _ in range(400):
        raw = struct.pack('<%dH' % (n := noise.randrange(0, 60)), *(noise.choice(alphabet) for _ in range(n)))
        raw += b'\x07' * noise.randrange(0, 2)  # an odd trailing byte is ignored
        assert hwp._para_text(raw, BUDGET) == reference_para_text(raw) == hwp._para_text(memoryview(raw), BUDGET), raw


def test_hwp_paragraph_limit_and_early_unit_and_text_limits():
    text = '가' * 500  # 1,000 bytes of PARA_TEXT plus the closing 0x000D
    data = make_hwp.hwp_bytes([[text, 'b']])
    assert outcome(run(data, '.hwp', Limits(hwp_max_para_bytes=1002))) == ('ok', None)
    assert outcome(run(data, '.hwp', Limits(hwp_max_para_bytes=1001))) == ('too_large', 'hwp_para')
    many = make_hwp.hwp_bytes([['a', 'b', 'c'], ['d']])
    assert outcome(run(many, '.hwp', Limits(max_units=3))) == ('too_large', 'limit_units')
    assert outcome(run(many, '.hwp', Limits(max_text_chars=3))) == ('too_large', 'limit_chars')


def test_hwp_memory_is_bounded_by_the_section_and_paragraph_limits():
    """Review M1-1 (P4): hwp-1 turned a paragraph into one Python integer per character (about 35 times its
    size). A 1 MiB paragraph (a few KiB compressed) now takes <= 5 x its size (k = 5, about 3.6 measured:
    the gathered record, the decoded string and the dispatcher's UTF-8 check). A 4 MiB paragraph over
    ``hwp_max_para_bytes`` is skipped without being kept, then refused: peak <= 2 MiB (hwp-2: 3 x the
    inflated section)."""
    size = 2**20
    data = make_hwp.hwp_bytes([['가' * (size // 2)]])
    assert len(data) < 16 * 2**10
    status, error, units, peak = traced_peak(data, '.hwp', Limits())
    assert (status, error, units) == ('ok', None, 1) and peak <= 5 * size
    big = make_hwp.hwp_bytes([['가' * (2 * size)]])
    status, error, units, peak = traced_peak(big, '.hwp', Limits(hwp_max_para_bytes=size // 4))
    assert (status, error) == ('too_large', 'hwp_para') and peak <= 2 * size


def fake_clock(monkeypatch) -> list:
    """Every look at the parse clock is one second later; returns the counter."""
    looks = [0]

    def monotonic():
        looks[0] += 1
        return float(looks[0])
    monkeypatch.setattr(_container, 'time', types.SimpleNamespace(monotonic=monotonic))
    return looks


PIECE = 2**18  # hwp._PIECE_BYTES: decompressed section bytes handled at a time


def test_hwp_records_across_section_pieces():
    """hwp-3 inflates and cuts records 256 KiB at a time: a paragraph, an extended header and a skipped
    record that cross piece boundaries give the same paragraphs as one whole stream."""
    assert hwp._PIECE_BYTES == PIECE
    filler = make_hwp.record(99, 0, b'\x00' * (PIECE - 40))           # ends just before the first boundary
    across = make_hwp.para('경계를 넘는 문단 ' * 20)                        # PARA_HEADER + PARA_TEXT across it
    big_other = make_hwp.record(99, 0, b'\x07' * (2 * PIECE + 123))    # a skipped record over two pieces
    long_text = '긴 문단 ' * (PIECE // 8)                                 # one PARA_TEXT over several pieces
    raw = filler + across + big_other + make_hwp.para('뒤 문단') + make_hwp.para(long_text) + make_hwp.para('끝')
    padding = PIECE - len(raw) % PIECE - 6                               # the last header straddles a boundary
    raw = raw[:-len(make_hwp.para('끝'))] + make_hwp.record(99, 0, b'\x00' * padding) + make_hwp.para('끝')
    for compressed in (True,):
        data = make_hwp.hwp_bytes([['x']], compressed=compressed, raw_sections={0: raw})
        result = run(data, '.hwp')
        assert outcome(result) == ('ok', None)
        assert texts(result) == [('경계를 넘는 문단 ' * 20).strip(), '뒤 문단', long_text.strip(), '끝']
        assert [u.unit['index'] for u in result.units] == [1, 2, 3, 4]
    oversized = filler + make_hwp.record(make_hwp.TAG_PARA_TEXT, 1, b'A\x00' * (PIECE // 2 + 10))
    data = make_hwp.hwp_bytes([['x']], raw_sections={0: oversized})
    assert outcome(run(data, '.hwp', Limits(hwp_max_para_bytes=PIECE))) == ('too_large', 'hwp_para')
    assert outcome(run(make_hwp.hwp_bytes([['x']], raw_sections={0: oversized[:-1]}), '.hwp',
                       Limits(hwp_max_para_bytes=PIECE))) == ('failed', 'hwp_bad_record')  # cut short: damage first
    cut = filler + make_hwp.para('x' * 400_000)[:-2]  # a gathered paragraph that never ends
    assert outcome(run(make_hwp.hwp_bytes([['x']], raw_sections={0: cut}), '.hwp')) == ('failed', 'hwp_bad_record')


def test_hwp_section_memory_does_not_grow_with_the_section():
    """Review M1 B15 (``hwp_records``): hwp-2 inflated a whole section at once (about twice its size). hwp-3
    holds one 256 KiB piece: a 4 MiB section of other records and 65,536 empty paragraphs peaks <= 2 MiB."""
    raw = (make_hwp.record(99, 0, bytes(4000)) * 1000 + make_hwp.record(make_hwp.TAG_PARA_TEXT, 1, b'') * 65_536
           + make_hwp.para('끝'))
    data = make_hwp.hwp_bytes([['x']], raw_sections={0: raw})
    assert len(raw) > 4 * 2**20 and len(data) < 64 * 2**10
    status, error, units, peak = traced_peak(data, '.hwp', Limits())
    assert (status, error, units) == ('ok', None, 1) and peak <= 2 * 2**20
    assert run(data, '.hwp').units[0].unit == {'kind': 'section_para', 'section': 1, 'index': 65_537}


def test_hwp_time_limit_is_checked_per_piece(monkeypatch):
    raw = make_hwp.para('a') + make_hwp.record(99, 0, b'\x00' * (3 * PIECE)) + make_hwp.para('b')  # 4 pieces
    data = make_hwp.hwp_bytes([['x']], raw_sections={0: raw})
    assert outcome(run(data, '.hwp', Limits(parse_seconds=-1))) == ('too_large', 'parse_time')
    looks = fake_clock(monkeypatch)
    assert texts(run(data, '.hwp', Limits(parse_seconds=100))) == ['a', 'b']
    assert looks[0] == 1 + 4  # the start, then one look per 256 KiB piece
    looks[0] = 0
    assert outcome(run(data, '.hwp', Limits(parse_seconds=3.5))) == ('too_large', 'parse_time')


def test_pdf_time_limit_is_checked_before_each_page(monkeypatch):
    data = make_pdf.latin_pdf([['one'], ['two'], ['three']])
    assert outcome(run(data, '.pdf', Limits(parse_seconds=-1))) == ('too_large', 'parse_time')
    looks = fake_clock(monkeypatch)
    # The budget is made, the clock is read before and after opening the file, then before each page and
    # when its content stream is measured.
    assert texts(run(data, '.pdf', Limits(parse_seconds=100))) == ['one', 'two', 'three'] and looks[0] == 1 + 2 + 3 * 2
    looks[0] = 0
    assert outcome(run(data, '.pdf', Limits(parse_seconds=6.5))) == ('too_large', 'parse_time')  # at the third page


def test_pdf_text_limit_stops_while_reading():
    data = make_pdf.latin_pdf([['one'], ['two'], ['three']])
    assert outcome(run(data, '.pdf', Limits(max_text_chars=11))) == ('ok', None)
    assert outcome(run(data, '.pdf', Limits(max_text_chars=10))) == ('too_large', 'limit_chars')


def test_hwp_other_records_are_skipped_and_trailing_deflate_data_is_ignored():
    raw = (make_hwp.record(make_hwp.TAG_DOCUMENT_PROPERTIES, 0, b'\x07' * 30)
           + make_hwp.para('keep me') + make_hwp.record(99, 2, b'\xff' * 10))
    streams = {'FileHeader': make_hwp.file_header(make_hwp.FLAG_COMPRESSED),
               'BodyText/Section0': make_hwp.deflate(raw) + b'\x00' * 40}
    assert texts(run(cfb.write_cfb(streams), '.hwp')) == ['keep me']


def test_hwp_header_checks_bad_signature_version_and_missing_header():
    one = [['text']]
    assert outcome(run(make_hwp.hwp_bytes(one, signature=b'HWP Document Fil'), '.hwp')) == (
        'unsupported', 'hwp_bad_header')
    assert outcome(run(make_hwp.hwp_bytes(one, version=(4, 0, 0, 0)), '.hwp')) == ('unsupported', 'hwp_version')
    assert outcome(run(make_hwp.hwp_bytes(one, version=(6, 0, 0, 0)), '.hwp')) == ('unsupported', 'hwp_version')
    short = cfb.write_cfb({'FileHeader': make_hwp.SIGNATURE.ljust(32, b'\x00') + b'\x00\x03\x00', 'BodyText/Section0': b''})
    assert outcome(run(short, '.hwp')) == ('unsupported', 'hwp_bad_header')
    no_header = cfb.write_cfb({'BodyText/Section0': b'x'})
    assert outcome(run(no_header, '.hwp')) == ('unsupported', 'hwp_bad_header')
    assert outcome(run(b'PK\x03\x04 not an ole file', '.hwp')) == ('unsupported', 'hwp_bad_header')
    with pytest.raises(ParseFailure) as caught:
        hwp.parse(parsers.OLE_SIGNATURE + b'\x00' * 2048, Limits(), _container.Budget(Limits()))
    assert (caught.value.status, caught.value.code) == ('failed', 'ole_invalid')


@pytest.mark.parametrize('raw', [
    make_hwp.record(make_hwp.TAG_PARA_TEXT, 1, b'\x41\x00')[:-1],       # size runs past the end
    make_hwp.para('ok')[:-3],                                           # cut inside a payload
    make_hwp.para('ok') + b'\x43\x00',                                   # half a record header
    struct.pack('<I', make_hwp.TAG_PARA_TEXT | (0xFFF << 20)) + b'\x01', # extended size cut short
    struct.pack('<II', make_hwp.TAG_PARA_TEXT | (0xFFF << 20), 2**31),   # absurd extended size
])
def test_hwp_bad_records(raw):
    for compressed in (True, False):
        assert outcome(run(make_hwp.hwp_bytes([['x']], compressed=compressed, raw_sections={0: raw}), '.hwp')) == (
            'failed', 'hwp_bad_record')


def test_hwp_bad_compressed_stream_and_truncated_deflate():
    good = make_hwp.deflate(make_hwp.section_stream(['text ' * 200]))
    for body in (b'\xff\xfe not deflate data at all ' * 5, good[:len(good) // 2], good[:5]):
        streams = {'FileHeader': make_hwp.file_header(make_hwp.FLAG_COMPRESSED), 'BodyText/Section0': body}
        assert outcome(run(cfb.write_cfb(streams), '.hwp')) == ('failed', 'hwp_bad_stream')


def test_hwp_limits_sections_and_decompressed_size():
    data = make_hwp.hwp_bytes([['a'], ['b'], ['c']])
    assert outcome(run(data, '.hwp', Limits(hwp_max_sections=2))) == ('too_large', 'hwp_count')
    assert outcome(run(data, '.hwp', Limits(hwp_max_sections=3))) == ('ok', None)
    bomb = make_hwp.hwp_bytes([['x']], raw_sections={0: make_hwp.para('a') + b'\x00' * 2_000_000})
    assert len(bomb) < 20_000
    assert outcome(run(bomb, '.hwp', Limits(zip_max_part_bytes=100_000))) == ('too_large', 'zip_part')
    two = make_hwp.hwp_bytes([['x'], ['y']], raw_sections={0: make_hwp.para('a') * 400, 1: make_hwp.para('b') * 400})
    assert outcome(run(two, '.hwp', Limits(zip_max_total_bytes=16_000))) == ('too_large', 'zip_total')
    assert outcome(run(two, '.hwp', Limits(zip_max_total_bytes=30_000))) == ('ok', None)
    assert outcome(run(make_hwp.hwp_bytes([['x' * 100]], compressed=False), '.hwp', Limits(zip_max_part_bytes=50))) == (
        'too_large', 'zip_part')


# HWPX
def hwpx(*sections: list[str], **kwargs) -> bytes:
    return make_hwp.hwpx_bytes([make_hwp.hwpx_section(list(paragraphs)) for paragraphs in sections], **kwargs)


def test_hwpx_paragraphs_without_table_duplication():
    outer = make_hwp.hp_para('앞 문장', table=[['셀 A', '셀 B']])
    data = hwpx([outer, make_hwp.hp_para('마지막')])
    result = run(data, '.hwpx')
    assert outcome(result) == ('ok', None)
    assert [(u.unit['index'], u.text) for u in result.units] == [(1, '앞 문장'), (2, '셀 A'), (3, '셀 B'), (4, '마지막')]
    assert sum(u.text.count('셀 A') for u in result.units) == 1 and '셀' not in result.units[0].text


def test_hwpx_text_after_a_nested_table_stays_with_the_outer_paragraph():
    xml = (f'<?xml version="1.0"?><hs:sec xmlns:hs="{make_hwp.HS_NS}" xmlns:hp="{make_hwp.HP_NS}">'
           '<hp:p><hp:run><hp:t>앞</hp:t></hp:run><hp:run><hp:tbl><hp:tr><hp:tc><hp:subList><hp:p><hp:run>'
           '<hp:t>셀 <hp:tab/>안</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr></hp:tbl></hp:run>'
           '<hp:run><hp:t>뒤</hp:t><hp:lineBreak/><hp:t>끝</hp:t></hp:run></hp:p></hs:sec>').encode('utf-8')
    result = run(make_hwp.hwpx_bytes([xml]), '.hwpx')
    assert [(u.unit['index'], u.text) for u in result.units] == [(1, '앞뒤\n끝'), (2, '셀 \t안')]


def test_hwpx_sections_numeric_order_empty_paragraphs_and_other_parts_ignored():
    def part(text):
        return make_hwp.hwpx_section([make_hwp.hp_para(''), make_hwp.hp_para(text)])
    archive = make_hwp.hwpx_bytes([], extra={
        'Contents/section10.xml': part('ten'), 'Contents/section2.xml': part('two'),
        'Contents/section0.xml': part('zero'), 'Contents/sectionX.xml': b'<broken', 'Contents/section1.xml.bak': b'<broken',
        'BinData/section3.xml': b'<broken'})
    result = run(archive, '.hwpx')
    assert [(u.unit['section'], u.unit['index'], u.text) for u in result.units] == [
        (1, 2, 'zero'), (3, 2, 'two'), (11, 2, 'ten')]


def test_hwpx_only_plain_decimal_part_names_are_sections_and_repeated_names_are_bad_zip():
    archive = make_hwp.hwpx_bytes([], extra={
        'Contents/section0.xml': make_hwp.hwpx_section([make_hwp.hp_para('zero')]),
        'Contents/section00.xml': make_hwp.hwpx_section([make_hwp.hp_para('not a section')])})
    assert [(u.unit['section'], u.text) for u in run(archive, '.hwpx').units] == [(1, 'zero')]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as twice, warnings.catch_warnings():
        warnings.simplefilter('ignore')  # zipfile warns about the repeated name
        for text in ('first', 'second'):
            twice.writestr('Contents/section0.xml', make_hwp.hwpx_section([make_hwp.hp_para(text)]))
    assert outcome(run(buffer.getvalue(), '.hwpx')) == ('failed', 'bad_zip')


def test_hwpx_memory_does_not_grow_with_the_part():
    """1 MiB of empty ``hp:p``: hwpx-1 built a tree (about 16 times the part), hwpx-2 kept the part and one
    chunk of elements; hwpx-3 streams the member into expat: peak <= 2 MiB whatever the size."""
    part = 2**20
    data = make_hwp.hwpx_bytes([make_hwp.hwpx_section(['<hp:p/>' * (part // 7)])])
    status, error, units, peak = traced_peak(data, '.hwpx', Limits())
    assert (status, error, units) == ('ok', None, 0) and peak <= 2 * 2**20


def test_hwpx_structure_text_and_time_limits(monkeypatch):
    """Review M1-3 for HWPX: deep nesting is ``xml_depth`` (hwpx-2 kept every open element and walked the
    open path after each chunk); one paragraph's text over ``max_unit_chars`` is ``unit_chars``."""
    deep = make_hwp.hwpx_bytes([make_hwp.hwpx_section(['<hp:p>' * 100_000 + '</hp:p>' * 100_000])])
    started = time.perf_counter()
    status, error, units, peak = traced_peak(deep, '.hwpx', Limits())
    assert (status, error) == ('too_large', 'xml_depth') and peak <= 2**20 and time.perf_counter() - started < 2
    two = make_hwp.hwpx_bytes([make_hwp.hwpx_section([make_hwp.hp_para('x' * 500) + make_hwp.hp_para('y' * 500)])])
    assert outcome(run(two, '.hwpx', Limits(max_unit_chars=500))) == ('ok', None)
    split = make_hwp.hwpx_bytes([make_hwp.hwpx_section(['<hp:p><hp:run><hp:t>' + 'x' * 300 + '</hp:t><hp:t>'
                                                          + 'y' * 300 + '</hp:t></hp:run></hp:p>'])])
    assert outcome(run(split, '.hwpx', Limits(max_unit_chars=600))) == ('ok', None)
    assert outcome(run(split, '.hwpx', Limits(max_unit_chars=599))) == ('too_large', 'unit_chars')
    assert outcome(run(split, '.hwpx', Limits(max_text_chars=599))) == ('too_large', 'limit_chars')
    sections = hwpx([make_hwp.hp_para('a')], [make_hwp.hp_para('b')], [make_hwp.hp_para('c')])
    looks = fake_clock(monkeypatch)
    assert texts(run(sections, '.hwpx', Limits(parse_seconds=100))) == ['a', 'b', 'c']
    used = looks[0] - 1
    looks[0] = 0
    assert outcome(run(sections, '.hwpx', Limits(parse_seconds=used - 0.5))) == ('too_large', 'parse_time')


def test_hwpx_inline_children_of_a_text_and_odd_nesting():
    xml = (f'<?xml version="1.0"?><hs:sec xmlns:hs="{make_hwp.HS_NS}" xmlns:hp="{make_hwp.HP_NS}"><hp:p><hp:run>'
           '<hp:t>a<hp:tab/>b<hp:lineBreak/>c<hp:x>ignored</hp:x>d</hp:t>tail ignored<hp:tab/>'
           '<hp:t>e<hp:p><hp:run><hp:t>inner</hp:t></hp:run></hp:p>f</hp:t></hp:run></hp:p></hs:sec>').encode('utf-8')
    result = run(make_hwp.hwpx_bytes([xml]), '.hwpx')
    assert [(u.unit['index'], u.text) for u in result.units] == [(1, 'a\tb\ncd\tef'), (2, 'inner')]


def test_hwpx_rejects_missing_section_bad_zip_dtd_invalid_xml_and_limits():
    assert outcome(run(make_hwp.hwpx_bytes([]), '.hwpx')) == ('failed', 'missing_part')
    assert outcome(run(b'PK\x03\x04' + b'\x00' * 60, '.hwpx')) == ('failed', 'bad_zip')
    assert outcome(run(b'plain text', '.hwpx')) == ('unsupported', 'bad_signature')
    assert outcome(run(make_hwp.hwpx_bytes([b'<hs:sec xmlns:hs="x"><hp:p']), '.hwpx')) == ('failed', 'xml_invalid')
    dtd = b'<?xml version="1.0"?><!DOCTYPE hs:sec [<!ENTITY e "x">]><hs:sec xmlns:hs="x"/>'
    assert outcome(run(make_hwp.hwpx_bytes([dtd]), '.hwpx')) == ('failed', 'xml_dtd')
    three = hwpx(['a'.join(['', ''])], [make_hwp.hp_para('b')], [make_hwp.hp_para('c')])
    assert outcome(run(three, '.hwpx', Limits(hwp_max_sections=2))) == ('too_large', 'hwp_count')
    big = make_hwp.hwpx_bytes([make_hwp.hwpx_section([make_hwp.hp_para('x' * 3000)])])
    assert outcome(run(big, '.hwpx', Limits(zip_max_part_bytes=1000))) == ('too_large', 'zip_part')
    assert outcome(run(make_hwp.hwpx_bytes([make_hwp.hwpx_section([])]), '.hwpx')) == ('ok', None)


def test_unit_text_works_as_a_locator_quote():
    result = run(CASES['docs/hwpx_tables.hwpx'][0], '.hwpx')
    unit = result.units[6]
    anchor = locator.anchor(unit.text, locator.sentences(unit.text)[0])
    quoted = locator.make(1, 'docs/x.hwpx', '0' * 64, unit.unit, unit.text, [anchor['start'], anchor['end']])
    assert locator.check_text(quoted, unit.text) == unit.text[anchor['start']:anchor['end']]


# Parser resource design §3.7, §3.8 (budget charges)
def test_hwp_control_characters_do_not_make_pieces():
    """R11 (review v3 M1-6): every control character cut the text into another memoryview piece (a
    16 MiB paragraph of controls: 8.4 M pieces, +2.3 to +3.0 GiB). The text now goes into one buffer:
    a 2 MiB paragraph of 'a' and control 31 alternating peaks under 3 times the paragraph."""
    payload = ('a\x1f' * 2**19).encode('utf-16-le')
    assert len(payload) == 2 * 2**20
    raw = make_hwp.record(make_hwp.TAG_PARA_HEADER, 0, bytes(22)) + make_hwp.record(make_hwp.TAG_PARA_TEXT, 1, payload)
    data = make_hwp.hwp_bytes([['x']], raw_sections={0: raw})
    status, error, units, peak = traced_peak(data, '.hwp', Limits())
    assert (status, error, units) == ('ok', None, 1) and peak < 3 * len(payload)
    assert run(data, '.hwp').units[0].text == 'a' * 2**19
    only_controls = make_hwp.record(make_hwp.TAG_PARA_TEXT, 1, '\x00\x0a'.encode('utf-16-le') * 2**18)
    result = run(make_hwp.hwp_bytes([['x']], raw_sections={0: only_controls}), '.hwp')
    assert outcome(result) == ('ok', None) and result.units == ()  # line breaks only: nothing to keep
    assert outcome(run(data, '.hwp', Limits(max_events=2**18))) == ('too_large', 'limit_events')  # 512 Ki controls


def test_hwp_records_are_events():
    """R12: every record is one event of the file's budget, empty ones included."""
    raw = make_hwp.record(make_hwp.TAG_PARA_TEXT, 1, b'') * 10_000 + make_hwp.para('끝')
    data = make_hwp.hwp_bytes([['x']], raw_sections={0: raw})
    assert outcome(run(data, '.hwp', Limits(max_events=5_000))) == ('too_large', 'limit_events')
    assert texts(run(data, '.hwp', Limits(max_events=20_000))) == ['끝']


def test_hwpx_inline_breaks_count_as_text_and_other_children_are_not_kept():
    """HX2: a tab or line break is one character of its paragraph; any other child of ``hp:t`` adds no piece."""
    breaks = make_hwp.hwpx_bytes([make_hwp.hwpx_section(['<hp:p><hp:run><hp:t>a' + '<hp:lineBreak/>' * 10_000
                                                         + 'b</hp:t></hp:run></hp:p>'])])
    assert outcome(run(breaks, '.hwpx', Limits(max_unit_chars=1_000))) == ('too_large', 'unit_chars')
    assert outcome(run(breaks, '.hwpx', Limits(max_text_chars=1_000))) == ('too_large', 'limit_chars')
    assert outcome(run(breaks, '.hwpx', Limits(max_unit_chars=10_002))) == ('ok', None)
    others = make_hwp.hwpx_bytes([make_hwp.hwpx_section(['<hp:p><hp:run><hp:t>a' + '<hp:x/>' * 10_000
                                                         + 'b</hp:t></hp:run></hp:p>'])])
    assert texts(run(others, '.hwpx', Limits(max_unit_chars=2))) == ['ab']


# Parser resource design §4 (PDF)
def readers(monkeypatch) -> list:
    """The PdfReader objects ``pdf.parse`` makes, kept for inspection."""
    made = []

    class Reader(pdf.PdfReader):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)
    monkeypatch.setattr(pdf, 'PdfReader', Reader)
    return made


def test_pdf_visitor_stops_nested_forms():
    """R13 (review v3 M1-9): a form drawn ten times per level, five levels deep, is 90 M characters; pypdf
    swallows any error inside a form, so the old limit (checked after the page) let it all be built. The
    visitors stop it once the budget's text runs out, at every level, within two seconds."""
    data = make_pdf.form_chain_pdf(5)
    started = time.perf_counter()
    status, error, units, peak = traced_peak(data, '.pdf', Limits(max_text_chars=10_000))
    assert (status, error) == ('too_large', 'limit_chars') and time.perf_counter() - started < 2 and peak < 32 * 2**20
    assert outcome(run(data, '.pdf', Limits(max_unit_chars=10_000))) == ('too_large', 'unit_chars')
    assert outcome(run(make_pdf.form_chain_pdf(2), '.pdf')) == ('ok', None)
    assert outcome(run(make_pdf.form_chain_pdf(5), '.pdf', Limits(max_events=1_000))) == ('too_large', 'limit_events')


def test_pdf_visitor_failure_at_page_level_is_the_same_code():
    """R14: a page without forms stops the same way (the visitor's error is not swallowed there)."""
    data = make_pdf.long_page_pdf(5_000)
    assert outcome(run(data, '.pdf', Limits(max_unit_chars=100))) == ('too_large', 'unit_chars')
    assert outcome(run(data, '.pdf', Limits(max_text_chars=100))) == ('too_large', 'limit_chars')
    assert len(texts(run(data, '.pdf'))[0]) == 5_000 + 4  # the line breaks between the five lines


def test_pdf_kids_are_counted_before_flattening(monkeypatch):
    """R15 (N1): pypdf makes one page object per ``/Kids`` reference and trusts ``/Count``; the tree is
    counted first, so 50 references to one page are over 10 pages before pypdf flattens anything. A
    cycle between two ``/Pages`` nodes is a read error."""
    made = readers(monkeypatch)
    assert outcome(run(make_pdf.kids_pdf(50), '.pdf', Limits(pdf_max_pages=10))) == ('too_large', 'pdf_count')
    assert made[-1].flattened_pages is None
    assert texts(run(make_pdf.kids_pdf(10), '.pdf', Limits(pdf_max_pages=10))) == ['repeated page'] * 10
    assert outcome(run(make_pdf.pages_cycle_pdf(), '.pdf')) == ('failed', 'pdf_read_error')
    assert made[-1].flattened_pages is None
    assert texts(run(make_pdf.kids_pdf(3, count=100_000), '.pdf')) == ['repeated page'] * 3  # N13: /Count ignored


def test_pdf_parent_loop_is_skipped():
    """R16 (N2): pypdf looks for inherited ``/Resources`` up the ``/Parent`` links with no limit; a page,
    or a form it draws, on a ``/Parent`` cycle is read as a page without text instead of hanging."""
    for in_form in (False, True):
        result = run(make_pdf.parent_loop_pdf(in_form=in_form), '.pdf')
        assert [(u.unit['page'], u.text) for u in result.units] == [(2, 'kept page')]


def test_pdf_decoded_cache_is_dropped_between_pages(monkeypatch):
    """R17 (N3): pypdf keeps the decoded copy of every stream it read for the reader's life (200 pages of
    20 MiB content streams would keep 4 GiB). After each page its content streams' copies are dropped."""
    made = readers(monkeypatch)
    data = make_pdf.big_stream_pdf(3, 2**20)
    assert texts(run(data, '.pdf')) == ['page 1', 'page 2', 'page 3']
    reader = made[-1]
    assert all(page['/Contents'].decoded_self is None for page in reader.pages)
    status, error, units, peak = traced_peak(data, '.pdf', Limits())
    one = traced_peak(make_pdf.big_stream_pdf(1, 2**20), '.pdf', Limits())[3]
    assert (status, error, units) == ('ok', None, 3) and peak < one + 2**20  # flat in the number of pages


def test_hwp_control_runs_are_one_step():
    """Probe hwp_ctrl0_x6: seven 16 MiB paragraphs of control 0 were 58.7 M loop steps (31 s, limit_events).
    Up to 4,096 controls in a row are passed in one match: one step, one event. A match keeps
    regular-expression state for every repetition, so it stops at 4,096 (unbounded runs took +2 GiB)."""
    raw = make_hwp.record(make_hwp.TAG_PARA_HEADER, 0, bytes(22)) + make_hwp.record(
        make_hwp.TAG_PARA_TEXT, 1, b'\x00\x00' * 2**19 + 'a'.encode('utf-16-le') + b'\x0a\x00' * 2**18 + b'b\x00')
    data = make_hwp.hwp_bytes([['x']], raw_sections={0: raw})
    result = run(data, '.hwp', Limits(max_events=1_000))
    status, error, units, peak = traced_peak(data, '.hwp', Limits())
    assert (status, error, units) == ('ok', None, 1) and peak < 6 * 2**20  # 1.5 MiB payload: buffer and text
    assert outcome(result) == ('ok', None) and texts(result) == ['a' + '\n' * 2**18 + 'b']


def test_pdf_page_tree_objects_are_measured_before_pypdf_parses_them(monkeypatch):
    """Probe N1: 16 M references in one ``/Kids`` (96 MB) made pypdf build 1.7 GiB of objects before the
    references were counted. The tree objects' raw sizes are read from the cross-reference table first."""
    made = readers(monkeypatch)
    data = make_pdf.kids_pdf(1_000)
    assert texts(run(data, '.pdf', Limits(pdf_max_pages=1_000))) == ['repeated page'] * 1_000
    monkeypatch.setattr(pdf, '_TREE_BYTES', 4_000)  # the /Kids object is about 6,000 bytes
    assert outcome(run(data, '.pdf', Limits(pdf_max_pages=1_000))) == ('too_large', 'pdf_count')
    assert made[-1].flattened_pages is None and 2 not in {key[1] for key in made[-1].resolved_objects}


def test_pdf_object_streams_are_measured_before_pypdf_parses_them(monkeypatch):
    """Review v4 B-1: pypdf parses a whole object stream as soon as one object in it is needed and keeps
    every object, 35 to 120 bytes a token (a page tree of 12 M references in a 300 KiB file: +1,275 MiB).
    The decoded object streams are measured in tokens before anything is resolved."""
    made = readers(monkeypatch)
    data = make_pdf.object_stream_pdf(100)
    assert texts(run(data, '.pdf')) == ['packed page'] * 100
    monkeypatch.setattr(pdf, '_OBJECT_STREAM_TOKENS', 200)  # 100 references are about 300 tokens
    assert outcome(run(data, '.pdf')) == ('too_large', 'pdf_stream')
    assert 2 not in {key[1] for key in made[-1].resolved_objects}  # the tree was never parsed


def test_pdf_page_content_is_measured_before_pypdf_parses_it(monkeypatch):
    """Review v4 B-1: pypdf turns a whole content stream into operations before any visitor runs (72 MB of
    numbers: +2.5 GiB). A page's content and the forms it can draw are measured in tokens first."""
    data = make_pdf.operands_pdf(10_000)
    assert texts(run(data, '.pdf')) == ['operand page']
    monkeypatch.setattr(pdf, '_PAGE_TOKENS', 5_000)
    assert outcome(run(data, '.pdf')) == ('too_large', 'pdf_stream')
    forms = make_pdf.form_chain_pdf(3, leaf_chars=10)  # ten draws a level: each form is counted once
    assert outcome(run(forms, '.pdf')) == ('ok', None)
    monkeypatch.setattr(pdf, '_PAGE_TOKENS', 60)
    assert outcome(run(forms, '.pdf')) == ('too_large', 'pdf_stream')
    assert pdf._TOKEN.subn(b'', b'1 2  3\n/a/b [(x) <00>]')[1] == 10  # 5 white space runs, 5 starting delimiters

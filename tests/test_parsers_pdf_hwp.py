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
import tracemalloc
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
    assert re.fullmatch(r'pdf-1/pypdf-\d+\.\d+\.\d+', pdf.VERSION) and pdf.NAME == 'pdf'
    assert re.fullmatch(r'hwp-2/olefile-\d+\.\d+', hwp.VERSION) and hwp.NAME == 'hwp'
    assert (hwp.HWPX_NAME, hwp.HWPX_VERSION) == ('hwpx', 'hwpx-2')
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
    assert hwp._para_text('\u0400\uac00'.encode('utf-16-le')) == '\u0400\uac00'
    noise = random.Random(11)
    alphabet = [0, 1, 2, 9, 10, 13, 24, 31, 32, 0x41, 0x400, 0x1F00, 0xAC00, 0xD800, 0xDC00, 0xD83D, 0xDE00, 0xFFFF]
    for _ in range(400):
        raw = struct.pack('<%dH' % (n := noise.randrange(0, 60)), *(noise.choice(alphabet) for _ in range(n)))
        raw += b'\x07' * noise.randrange(0, 2)  # an odd trailing byte is ignored
        assert hwp._para_text(raw) == reference_para_text(raw) == hwp._para_text(memoryview(raw)), raw


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
    size). A 1 MiB paragraph (a few KiB compressed) now takes <= 5 x its size (k = 5, about 3.5 measured:
    the inflated section, the cut-out text and the decoded string). A 4 MiB paragraph over
    ``hwp_max_para_bytes`` is refused before decoding: peak <= 3 x the inflated 4 MiB section."""
    size = 2**20
    data = make_hwp.hwp_bytes([['가' * (size // 2)]])
    assert len(data) < 16 * 2**10
    status, error, units, peak = traced_peak(data, '.hwp', Limits())
    assert (status, error, units) == ('ok', None, 1) and peak <= 5 * size
    big = make_hwp.hwp_bytes([['가' * (2 * size)]])
    status, error, units, peak = traced_peak(big, '.hwp', Limits(hwp_max_para_bytes=size // 4))
    assert (status, error) == ('too_large', 'hwp_para') and peak <= 3 * 4 * size


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
        hwp.parse(parsers.OLE_SIGNATURE + b'\x00' * 2048, Limits())
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


def test_hwpx_memory_stays_near_the_part_size():
    """1 MiB of empty ``hp:p``: hwpx-1 built a tree (about 16 times the part); the stream keeps the part and
    one chunk of events: peak <= 3 x the part (k = 3)."""
    part = 2**20
    data = make_hwp.hwpx_bytes([make_hwp.hwpx_section(['<hp:p/>' * (part // 7)])])
    status, error, units, peak = traced_peak(data, '.hwpx', Limits())
    assert (status, error, units) == ('ok', None, 0) and peak <= 3 * part


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

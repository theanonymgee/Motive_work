"""DOCX, XLSX and PPTX parsers (design M1 §5.3, §5.4, §5.8, §11 ③). Packages are generated, never committed."""
import gc
import io
import re
import time
import tracemalloc
import types
import zipfile

import pytest

from fixtures import SENTINEL, make_ooxml
from workmcp import locator, parsers
from workmcp.parsers import Limits, _container, docx, pptx, xlsx

CASES = {item.rel_path: (item, data) for item, data in make_ooxml.build()}
MIB = 2**20


def ext_of(rel_path: str) -> str:
    return '.' + rel_path.rsplit('.', 1)[1]


def check(item, data, limits=None):
    """Parse ``data`` like the collector and compare with what the generator promised."""
    result = parsers.parse(data, ext_of(item.rel_path), limits or Limits())
    assert (result.parser, result.status, result.error) == (item.parser, item.status, item.error)
    assert result.encoding is None
    assert len(result.units) == item.unit_count
    assert [unit.ordinal for unit in result.units] == list(range(1, len(result.units) + 1))
    for ordinal, (kind, unit, unit_text) in item.units_sample.items():
        got = result.units[ordinal - 1]
        assert (got.kind, got.unit, got.text) == (kind, unit, unit_text)
    return result


def outcome(data, ext, limits=None):
    result = parsers.parse(data, ext, limits or Limits())
    return result.status, result.error, len(result.units)


def traced_peak(data, ext, limits):
    """``(status, error, unit count, peak bytes)``: tracemalloc peak of one dispatcher call.

    The input is built and the parser modules are imported before tracing starts, so the peak is what
    parsing allocates (part bytes, events, tree, units).
    """
    parsers.parse(b'warm up', '.txt', Limits())
    gc.collect()
    tracemalloc.start()
    try:
        result = parsers.parse(data, ext, limits)
        return result.status, result.error, len(result.units), tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def encrypt_flag(data: bytes) -> bytes:
    """Set the 'encrypted' general-purpose flag bit in every local and central zip header."""
    out = bytearray(data)
    for signature, offset in ((b'PK\x03\x04', 6), (b'PK\x01\x02', 8)):
        start = out.find(signature)
        while start >= 0:
            out[start + offset] |= 0x01
            start = out.find(signature, start + 4)
    return bytes(out)


# ---- DOCX ----------------------------------------------------------------------------------------

def test_docx_paragraphs_tables_and_nested_textbox():
    item, data = CASES['docs/report.docx']
    result = check(item, data)
    assert result.parser_version == docx.VERSION
    texts = [u.text for u in result.units]
    assert '담당자 갑\t가상 거래처 A\n다음 줄' in texts          # w:tab and w:br
    assert texts.count('글상자 안 문장') == 1                   # the text box paragraph is not repeated ...
    assert not any('글상자 안 문장' in t and t != '글상자 안 문장' for t in texts)  # ... in its outer paragraph
    assert texts.count('대체 콘텐츠 상자') == 1                  # mc:Choice read, mc:Fallback skipped
    assert not any('삭제된' in t for t in texts)                # w:delText is not text
    assert all(u.unit.get('table') == 1 for u in result.units if 'row' in u.unit)


def test_docx_index_counts_empty_paragraphs_and_cells_use_the_closest_table():
    result = parsers.parse(make_ooxml.docx_nested_tables(), '.docx', Limits())
    assert [(u.unit, u.text) for u in result.units] == [
        ({'kind': 'paragraph', 'index': 1, 'table': 1, 'row': 1, 'column': 1}, '바깥 칸'),
        ({'kind': 'paragraph', 'index': 2, 'table': 2, 'row': 1, 'column': 1}, '안쪽 칸'),
        ({'kind': 'paragraph', 'index': 3, 'table': 1, 'row': 1, 'column': 2}, '바깥 둘째'),
        ({'kind': 'paragraph', 'index': 4}, SENTINEL)]


def test_docx_rejects_bad_zip_missing_part_dtd_invalid_xml_and_oversized_member():
    for rel_path in ('edge/bad_zip.docx', 'edge/missing_part.docx', 'edge/dtd.docx', 'edge/invalid_xml.docx'):
        check(*CASES[rel_path])
    assert outcome(make_ooxml.docx_with_external_entity(), '.docx') == ('failed', 'xml_dtd', 0)
    big = make_ooxml.docx_big_member(2 * MIB)
    assert len(big) < MIB  # spaces compress well: only the bounded read can see the real size
    assert outcome(big, '.docx', Limits(zip_max_part_bytes=MIB)) == ('too_large', 'zip_part', 0)
    assert outcome(big, '.docx', Limits(zip_max_part_bytes=3 * MIB)) == ('ok', None, 1)


def test_docx_memory_does_not_grow_with_the_part():
    """Review M1-1 (P2): 2 MiB of empty ``w:p`` packs into a few KiB. docx-1 built a tree plus four maps
    (about 88 times the part), docx-2 held the part and one chunk of elements (about 1.8 times). docx-3
    streams the member in 64 KiB pieces into expat and builds no element: peak <= 2 MiB whatever the
    part's size (about 1.3 measured for 1 and 4 MiB parts; docx-2 about 3.6 for this 2 MiB part), also when
    the read stops at ``zip_max_part_bytes``."""
    part = 2 * 2**20
    data = make_ooxml.docx_package('<w:p/>' * (part // 6))
    assert len(data) < 16 * 2**10
    status, error, units, peak = traced_peak(data, '.docx', Limits())
    assert (status, error, units) == ('ok', None, 0) and peak <= 2 * 2**20
    status, error, units, peak = traced_peak(data, '.docx', Limits(zip_max_part_bytes=part // 2))
    assert (status, error) == ('too_large', 'zip_part') and peak <= 2 * 2**20


def test_docx_structure_limits_reach_the_dispatcher():
    """Review M1-3 and M1-4 through ``parsers.parse``: deep nesting (100,000 ``w:p``; docx-2: ok after a time
    that grows with the depth squared), an attribute bomb, a name bomb and one long text node."""
    deep = make_ooxml.docx_package('<w:p>' * 100_000 + '</w:p>' * 100_000)
    attributes = make_ooxml.docx_package('<w:p ' + ' '.join(f'a{n:06d}=""' for n in range(60_000)) + '/>')
    names = make_ooxml.docx_package(''.join(f'<x{n:06d}/>' for n in range(20_000)))
    text = make_ooxml.docx_package(make_ooxml.w_p(make_ooxml.w_run('x' * (8 * 2**20 + 1))))
    started = time.perf_counter()
    assert outcome(deep, '.docx') == ('too_large', 'xml_depth', 0)
    assert outcome(attributes, '.docx') == ('too_large', 'xml_attributes', 0)
    assert outcome(names, '.docx') == ('too_large', 'xml_names', 0)
    assert outcome(text, '.docx') == ('too_large', 'xml_text', 0)
    assert time.perf_counter() - started < 5
    assert traced_peak(deep, '.docx', Limits())[3] <= 2**20
    assert traced_peak(names, '.docx', Limits())[3] <= 6 * 2**20  # expat and the name table hold 10,000 names


def test_docx_paragraph_text_and_file_text_are_limited_while_reading():
    two_runs = make_ooxml.docx_package(make_ooxml.w_p(make_ooxml.w_run('x' * 600), make_ooxml.w_run('y' * 600)))
    assert outcome(two_runs, '.docx', Limits(xml_max_text_chars=1200)) == ('ok', None, 1)
    assert outcome(two_runs, '.docx', Limits(xml_max_text_chars=1199)) == ('too_large', 'xml_text', 0)
    many = make_ooxml.docx_package(make_ooxml.w_p(make_ooxml.w_run('x' * 600)) * 10)
    assert outcome(many, '.docx', Limits(max_text_chars=6000)) == ('ok', None, 10)
    assert outcome(many, '.docx', Limits(max_text_chars=5999)) == ('too_large', 'limit_chars', 0)


def test_docx_time_limit(monkeypatch):
    """Every fed piece checks the clock; a parse that runs past ``parse_seconds`` is ``too_large/parse_time``."""
    data = make_ooxml.docx_package('<w:p/>' * 100_000)  # about 10 pieces
    assert outcome(data, '.docx', Limits(parse_seconds=-1)) == ('too_large', 'parse_time', 0)
    now = [0.0]

    def monotonic():
        now[0] += 1.0  # every look at the clock is one second later
        return now[0]
    monkeypatch.setattr(_container, 'time', types.SimpleNamespace(monotonic=monotonic))
    assert outcome(data, '.docx', Limits(parse_seconds=100)) == ('ok', None, 0)
    assert outcome(data, '.docx', Limits(parse_seconds=5)) == ('too_large', 'parse_time', 0)


def test_docx_strict_and_foreign_roots_are_unsupported():
    assert outcome(make_ooxml.to_strict(CASES['docs/report.docx'][1]), '.docx') == ('unsupported', 'ooxml_strict', 0)
    foreign = make_ooxml.docx_package('', extra={'word/document.xml': '<?xml version="1.0"?><document><p/></document>'})
    assert outcome(foreign, '.docx') == ('unsupported', 'bad_signature', 0)


def test_zip_entry_and_total_size_limits_apply_to_every_zip_format():
    docx_data = make_ooxml.docx_package('')
    assert outcome(docx_data, '.docx', Limits(zip_max_entries=2)) == ('too_large', 'zip_entries', 0)
    assert outcome(make_ooxml.docx_big_member(MIB), '.docx', Limits(zip_max_total_bytes=MIB)) == ('too_large', 'zip_total', 0)
    assert outcome(CASES['sheets/book.xlsx'][1], '.xlsx', Limits(zip_max_entries=3))[:2] == ('too_large', 'zip_entries')
    assert outcome(CASES['docs/deck.pptx'][1], '.pptx', Limits(zip_max_entries=3))[:2] == ('too_large', 'zip_entries')


def test_password_protected_zip_member_is_a_failure_not_an_exception():
    for ext, data in (('.docx', make_ooxml.docx_package('')), ('.xlsx', CASES['sheets/book.xlsx'][1]),
                      ('.pptx', CASES['docs/deck.pptx'][1])):
        status, error, count = outcome(encrypt_flag(data), ext)
        assert (status, error, count) == ('failed', 'bad_zip', 0), ext


def test_ooxml_signature_mismatches_reach_the_container_checks():
    # Only a non-zip, non-OLE file is rejected by the signature check; a zip of the wrong kind has no part.
    assert outcome(b'not a zip', '.docx') == ('unsupported', 'bad_signature', 0)
    assert outcome(CASES['sheets/book.xlsx'][1], '.docx') == ('failed', 'missing_part', 0)
    assert outcome(CASES['docs/report.docx'][1], '.pptx') == ('failed', 'missing_part', 0)
    assert outcome(CASES['docs/deck.pptx'][1], '.xlsx') == ('failed', 'missing_part', 0)


# ---- PPTX ----------------------------------------------------------------------------------------

def test_pptx_slide_order_shapes_groups_and_tables():
    item, data = CASES['docs/deck.pptx']
    result = check(item, data)
    assert result.parser_version == pptx.VERSION
    # sldIdLst puts slide2.xml first; shapes are numbered depth first and a group takes a number.
    assert [(u.unit['slide'], u.unit['shape']) for u in result.units] == [
        (1, 1), (1, 2), (1, 5), (2, 1), (2, 1), (2, 1), (2, 1), (2, 3), (2, 5), (2, 6)]
    assert result.units[1].text.count('\n') == 2   # two paragraphs, one a:br
    assert not any(u.text == '' for u in result.units)  # the empty table row gave no cell units


def test_pptx_empty_slide_has_no_units_and_limits_and_damage_are_coded():
    check(*CASES['edge/empty_slide.pptx'])
    assert outcome(make_ooxml.pptx_slides(2), '.pptx') == ('ok', None, 2)
    assert outcome(make_ooxml.pptx_slides(2), '.pptx', Limits(pptx_max_slides=1)) == ('too_large', 'pptx_count', 0)
    assert outcome(make_ooxml.pptx_slides(2), '.pptx', Limits(pptx_max_slides=2)) == ('ok', None, 2)
    assert outcome(make_ooxml.pptx_missing_slide(), '.pptx') == ('failed', 'missing_part', 0)
    assert outcome(make_ooxml.pptx_with_dtd(), '.pptx') == ('failed', 'xml_dtd', 0)
    assert outcome(make_ooxml.without_member(CASES['docs/deck.pptx'][1], 'ppt/presentation.xml'), '.pptx') == (
        'failed', 'missing_part', 0)


def test_pptx_reads_only_the_first_choice_of_alternate_content():
    result = parsers.parse(make_ooxml.pptx_alternate_choices(), '.pptx', Limits())
    assert [(u.unit, u.text) for u in result.units] == [
        ({'kind': 'slide', 'slide': 1, 'shape': 1}, '첫 선택 도형'), ({'kind': 'slide', 'slide': 1, 'shape': 2}, SENTINEL)]


def test_pptx_strict_is_unsupported():
    assert outcome(make_ooxml.to_strict(CASES['docs/deck.pptx'][1]), '.pptx') == ('unsupported', 'ooxml_strict', 0)


def test_pptx_slides_are_streamed_too():
    """Review M1-L12: pptx-2 read slides, the presentation and its relationships as trees (about 27 times
    the part, up to +739 MiB with two trees alive). pptx-3 streams every part: a 1 MiB slide and 1 MiB of
    relationships peak <= 2 MiB, and a slide over the old 16 MiB tree limit is read."""
    slide = make_ooxml._slide('<p:pic/>' * (2**20 // 8) + make_ooxml.p_sp([make_ooxml.a_p(make_ooxml.a_r(SENTINEL))]))
    data = make_ooxml.pptx_package([('rId2', slide)], ['rId2'])
    status, error, units, peak = traced_peak(data, '.pptx', Limits())
    assert (status, error, units) == ('ok', None, 1) and peak <= 2 * 2**20
    rels = make_ooxml.zip_bytes({**dict(_members(data)), 'ppt/_rels/presentation.xml.rels': (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + ''.join(f'<Relationship Id="rX{n}" Type="t" Target="slides/none.xml"/>' for n in range(15_000))
        + '<Relationship Id="rId2" Type="t" Target="slides/slide1.xml"/></Relationships>')})
    status, error, units, peak = traced_peak(rels, '.pptx', Limits())
    assert (status, error, units) == ('ok', None, 1) and peak <= 2 * 2**20
    spaced = ('<p:pic/>' + ' ' * 2**20) * 17  # 17 MiB, mostly white space so that the test stays quick
    big = make_ooxml.pptx_package([('rId2', make_ooxml._slide(spaced))], ['rId2'])
    assert outcome(big, '.pptx') == ('ok', None, 0)


def test_pptx_deep_groups_are_a_depth_limit_not_a_recursion_error():
    """pptx-2 recursed once per nested group (a deep enough file was ``exception:RecursionError``)."""
    deep = make_ooxml.pptx_package([('rId2', make_ooxml._slide('<p:grpSp>' * 5_000 + '</p:grpSp>' * 5_000))],
                                   ['rId2'])
    assert outcome(deep, '.pptx') == ('too_large', 'xml_depth', 0)
    groups = make_ooxml._slide(make_ooxml.p_group(make_ooxml.p_group(make_ooxml.p_group(
        make_ooxml.p_sp([make_ooxml.a_p(make_ooxml.a_r('깊은 도형'))])))))
    result = parsers.parse(make_ooxml.pptx_package([('rId2', groups)], ['rId2']), '.pptx', Limits())
    assert [(u.unit, u.text) for u in result.units] == [({'kind': 'slide', 'slide': 1, 'shape': 4}, '깊은 도형')]


def _members(data: bytes) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [(name, archive.read(name)) for name in archive.namelist()]


# ---- XLSX ----------------------------------------------------------------------------------------

def test_xlsx_shared_inline_numbers_booleans_formulas_and_phonetic():
    item, data = CASES['sheets/book.xlsx']
    result = check(item, data)
    assert result.parser_version == xlsx.VERSION
    by_cell = {(u.unit['sheet'], u.unit['cell']): u.text for u in result.units}
    assert by_cell[('합성 시트', 'B1')] == '가상 거래처 A' and '발음표기' not in ' '.join(by_cell.values())  # rPh dropped
    assert by_cell[('합성 시트', 'C2')] == 'TRUE' and by_cell[('합성 시트', 'D2')] == 'FALSE'
    assert by_cell[('합성 시트', 'A3')] == '3'                 # cached formula result
    assert ('합성 시트', 'B3') not in by_cell                  # formula without a cached value
    assert ('합성 시트', 'C3') not in by_cell                  # empty cell
    assert (by_cell[('합성 시트', 'A5')], by_cell[('합성 시트', 'B5')]) == ('주소 없는 셀', '7')  # addresses implied by position
    # The first sheet of the workbook is stored in sheet2.xml: workbook order wins over file names.
    assert result.units[0].unit['sheet'] == '합성 시트' and result.units[-1].unit['sheet'] == 'Sheet2'


def test_xlsx_cells_are_valid_locator_addresses_and_column_letters_round_trip():
    for number in (1, 26, 27, 52, 53, 702, 703, 18278):
        letters = xlsx._letters(number)
        assert xlsx._column(letters) == number
        assert re.fullmatch(r'[A-Z]{1,3}', letters)
    sheet = make_ooxml._sheet(
        '<row r="9">' + make_ooxml._c('AA9', None, '<v>1</v>') + make_ooxml._c(None, None, '<v>2</v>')
        + make_ooxml._c('ZZZ9', None, '<v>3</v>') + make_ooxml._c('a1', None, '<v>4</v>') + '</row>',
        '<row>' + make_ooxml._c(None, None, '<v>5</v>') + '</row>')
    data = make_ooxml.xlsx_package([('시트', 'xl/worksheets/sheet1.xml', sheet)], None)
    result = parsers.parse(data, '.xlsx', Limits())
    assert [(u.unit['cell'], u.text) for u in result.units] == [('AA9', '1'), ('AB9', '2'), ('ZZZ9', '3'), ('A10', '5')]


def test_xlsx_without_shared_strings_sheet_limit_and_damage_are_coded():
    assert outcome(make_ooxml.xlsx_without_shared_strings(), '.xlsx') == ('ok', None, 2)
    check(*CASES['edge/formula_only.xlsx'])
    assert outcome(make_ooxml.xlsx_sheets(3), '.xlsx') == ('ok', None, 3)
    assert outcome(make_ooxml.xlsx_sheets(3), '.xlsx', Limits(xlsx_max_sheets=2)) == ('too_large', 'xlsx_count', 0)
    assert outcome(make_ooxml.xlsx_sheets(3), '.xlsx', Limits(xlsx_max_sheets=3)) == ('ok', None, 3)
    assert outcome(make_ooxml.xlsx_missing_sheet(), '.xlsx') == ('failed', 'missing_part', 0)
    assert outcome(make_ooxml.xlsx_with_dtd(), '.xlsx') == ('failed', 'xml_dtd', 0)


def test_xlsx_duplicate_cells_and_sheet_names_are_refused():
    def book(*rows, sheets=(('시트', 'xl/worksheets/sheet1.xml'),)):
        return make_ooxml.xlsx_package([(name, member, make_ooxml._sheet(*rows)) for name, member in sheets], None)
    twice = '<row r="1">' + make_ooxml._c('A1', None, '<v>1</v>') + make_ooxml._c('A1', None, '<v>2</v>') + '</row>'
    assert outcome(book(twice), '.xlsx') == ('failed', 'unit_duplicate', 0)
    implied = '<row r="1">' + make_ooxml._c('B1', None, '<v>1</v>') + make_ooxml._c(None, None, '<v>2</v>') + \
        make_ooxml._c('C1', None, '<v>3</v>') + '</row>'  # the cell without an address is C1 too
    assert outcome(book(implied), '.xlsx') == ('failed', 'unit_duplicate', 0)
    split_row = ('<row r="1">' + make_ooxml._c('A1', None, '<v>1</v>') + '</row>'
                 + '<row r="1">' + make_ooxml._c('A1', None, '<v>2</v>') + '</row>')
    assert outcome(book(split_row), '.xlsx') == ('failed', 'unit_duplicate', 0)
    one = '<row r="1">' + make_ooxml._c('A1', None, '<v>1</v>') + '</row>'
    same_name = (('시트', 'xl/worksheets/sheet1.xml'), ('시트', 'xl/worksheets/sheet2.xml'))
    assert outcome(book(one, sheets=same_name), '.xlsx') == ('failed', 'unit_duplicate', 0)
    other_name = (('시트', 'xl/worksheets/sheet1.xml'), ('시트2', 'xl/worksheets/sheet2.xml'))
    assert outcome(book(one, sheets=other_name), '.xlsx') == ('ok', None, 2)


def test_xlsx_strict_is_unsupported():
    assert outcome(make_ooxml.to_strict(CASES['sheets/book.xlsx'][1]), '.xlsx') == ('unsupported', 'ooxml_strict', 0)


def test_xlsx_memory_does_not_grow_with_the_part():
    """1 MiB of empty rows: xlsx-1 kept a cleared shell per row (about 15 times the part), xlsx-2 the part and
    one chunk of events; xlsx-3 streams the member: peak <= 2 MiB whatever the size."""
    part = 2**20
    sheet = make_ooxml._sheet('<row/>' * (part // 6))
    data = make_ooxml.xlsx_package([('시트', 'xl/worksheets/sheet1.xml', sheet)], None)
    status, error, units, peak = traced_peak(data, '.xlsx', Limits())
    assert (status, error, units) == ('ok', None, 0) and peak <= 2 * 2**20


SST = '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{}</sst>'


def xlsx_with(sheet_rows: str, shared: str | None = None) -> bytes:
    return make_ooxml.xlsx_package([('시트', 'xl/worksheets/sheet1.xml', make_ooxml._sheet(sheet_rows))], shared)


def test_xlsx_shared_strings_count_and_text_limits():
    """Review M1 B12/B13: shared strings are kept for the whole parse. More of them than ``max_units`` is
    ``limit_units`` (Excel writes only strings its cells use), one longer than ``xml_max_text_chars`` is
    ``xml_text``, and their characters count towards ``max_text_chars``."""
    shared = SST.format('<si><t>a</t></si><si><r><t>b</t></r><r><t>c</t></r></si><si><t>d</t></si>')
    data = xlsx_with('<row r="1"><c r="A1" t="s"><v>1</v></c></row>', shared)
    assert [u.text for u in parsers.parse(data, '.xlsx', Limits()).units] == ['bc']
    assert outcome(data, '.xlsx', Limits(max_units=3)) == ('ok', None, 1)
    assert outcome(data, '.xlsx', Limits(max_units=2)) == ('too_large', 'limit_units', 0)
    assert outcome(data, '.xlsx', Limits(max_text_chars=4)) == ('ok', None, 1)
    assert outcome(data, '.xlsx', Limits(max_text_chars=3)) == ('too_large', 'limit_chars', 0)
    assert outcome(data, '.xlsx', Limits(xml_max_text_chars=2)) == ('ok', None, 1)
    assert outcome(data, '.xlsx', Limits(xml_max_text_chars=1)) == ('too_large', 'xml_text', 0)
    one_huge = xlsx_with('', SST.format('<si><t>' + 'x' * (8 * 2**20 + 1) + '</t></si>'))
    assert outcome(one_huge, '.xlsx') == ('too_large', 'xml_text', 0)


def test_xlsx_cell_text_limit_and_cells_outside_a_row():
    data = xlsx_with('<row r="2"><c t="inlineStr"><is><t>' + 'x' * 100 + '</t></is></c>'
                     '<c t="str"><v>' + 'y' * 100 + '</v></c></row><c r="C9" t="str"><v>z</v></c>')
    result = parsers.parse(data, '.xlsx', Limits())
    assert [(u.unit['cell'], len(u.text)) for u in result.units] == [('A2', 100), ('B2', 100), ('C9', 1)]
    assert outcome(data, '.xlsx', Limits(xml_max_text_chars=99)) == ('too_large', 'xml_text', 0)


def test_xlsx_workbook_relationships_keep_only_the_sheets_targets_and_the_clock_spans_every_part(monkeypatch):
    data = make_ooxml.xlsx_sheets(3)
    members = dict(_members(data))
    rels = members['xl/_rels/workbook.xml.rels'].decode()
    noise = ''.join(f'<Relationship Id="rX{n}" Type="t" Target="worksheets/none.xml"/>' for n in range(15_000))
    members['xl/_rels/workbook.xml.rels'] = rels.replace('</Relationships>', noise + '</Relationships>')
    padded = make_ooxml.zip_bytes(members)
    status, error, units, peak = traced_peak(padded, '.xlsx', Limits())
    assert (status, error, units) == ('ok', None, 3) and peak <= 2 * 2**20
    checks = [0]

    def monotonic():
        checks[0] += 1
        return float(checks[0])
    monkeypatch.setattr(_container, 'time', types.SimpleNamespace(monotonic=monotonic))
    assert outcome(data, '.xlsx', Limits(parse_seconds=1000)) == ('ok', None, 3)
    used = checks[0] - 1  # one look when the clock starts, then one per piece of every part
    assert used >= 5  # workbook, relationships, three sheets: one clock for the whole file
    checks[0] = 0
    assert outcome(data, '.xlsx', Limits(parse_seconds=used - 0.5)) == ('too_large', 'parse_time', 0)


def test_xlsx_unit_limit_stops_early():
    assert outcome(CASES['sheets/book.xlsx'][1], '.xlsx', Limits(max_units=5)) == ('too_large', 'limit_units', 0)


# ---- all three formats ---------------------------------------------------------------------------

def test_generated_documents_follow_the_synthetic_rules():
    for rel_path, (item, data) in CASES.items():
        if item.status == 'ok' and item.unit_count:
            result = parsers.parse(data, ext_of(rel_path), Limits())
            assert any(SENTINEL in unit.text for unit in result.units), rel_path
            assert not any('@' in unit.text for unit in result.units), rel_path  # no mail address in office text


@pytest.mark.parametrize('rel_path', sorted(CASES))
def test_parsers_are_deterministic(rel_path):
    item, data = CASES[rel_path]
    first = parsers.parse(data, ext_of(rel_path), Limits())
    assert parsers.parse(data, ext_of(rel_path), Limits()) == first
    assert dict((i.rel_path, d) for i, d in make_ooxml.build())[rel_path] == data  # same bytes every build


@pytest.mark.parametrize('rel_path', sorted(path for path, (item, _) in CASES.items() if item.status == 'ok' and item.unit_count))
def test_every_unit_passes_locator_check_unit(rel_path):
    item, data = CASES[rel_path]
    units, encoding = parsers.parser_for(ext_of(rel_path)).parse(data, Limits())
    assert encoding is None and len(units) == item.unit_count
    for unit in units:
        assert locator.check_unit(unit.unit) == unit.unit
        assert unit.kind == unit.unit['kind'] and unit.text and unit.text == unit.text.strip()


def test_parser_versions_follow_the_format():
    for ext, name in (('.docx', 'docx'), ('.pptx', 'pptx'), ('.xlsx', 'xlsx')):
        parser = parsers.parser_for(ext)
        assert parser.name == name and re.fullmatch(rf'{name}-[1-9][0-9]*', parser.version)

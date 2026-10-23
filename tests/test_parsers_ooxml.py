"""DOCX, XLSX and PPTX parsers (design M1 §5.3, §5.4, §5.8, §11 ③). Packages are generated, never committed."""
import gc
import re
import tracemalloc

import pytest

from fixtures import SENTINEL, make_ooxml
from workmcp import locator, parsers
from workmcp.parsers import Limits, docx, pptx, xlsx

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


def test_docx_memory_stays_near_the_part_size():
    """Review M1-1 (P2): 1 MiB of empty ``w:p`` packs into a few KiB. docx-1 built a tree plus four maps
    (about 88 times the part); the stream keeps the part and one chunk of events: peak <= 3 x the part
    (k = 3, about 1.8 measured). Over ``zip_max_part_bytes`` the bounded read stops at the limit: peak
    <= 3 x the limit."""
    part = 2**20
    data = make_ooxml.docx_package('<w:p/>' * (part // 6))
    assert len(data) < 16 * 2**10
    status, error, units, peak = traced_peak(data, '.docx', Limits())
    assert (status, error, units) == ('ok', None, 0) and peak <= 3 * part
    status, error, units, peak = traced_peak(data, '.docx', Limits(zip_max_part_bytes=part // 2))
    assert (status, error) == ('too_large', 'zip_part') and peak <= 3 * part // 2


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


def test_pptx_strict_is_unsupported_and_a_slide_over_the_tree_limit_is_too_large():
    assert outcome(make_ooxml.to_strict(CASES['docs/deck.pptx'][1]), '.pptx') == ('unsupported', 'ooxml_strict', 0)
    slide = make_ooxml._slide(make_ooxml.p_sp([make_ooxml.a_p(make_ooxml.a_r(SENTINEL))]))
    data = make_ooxml.pptx_package([('rId2', slide)], ['rId2'])
    assert outcome(data, '.pptx', Limits(xml_max_tree_bytes=len(slide.encode()))) == ('ok', None, 1)
    assert outcome(data, '.pptx', Limits(xml_max_tree_bytes=len(slide.encode()) - 1)) == ('too_large', 'xml_tree', 0)


def test_pptx_tree_memory_is_bounded_by_the_tree_limit():
    """Slides are trees: about 11 times the part for tiny elements (measured), asserted <= 16 x (k = 16).
    So the default ``xml_max_tree_bytes`` (16 MiB) bounds one slide near 180 MiB. A part over the limit
    is refused after the read only: peak <= 3 x the part."""
    part = 2**20
    data = make_ooxml.pptx_package([('rId2', make_ooxml._slide('<p:pic/>' * (part // 8)))], ['rId2'])
    status, error, units, peak = traced_peak(data, '.pptx', Limits())
    assert (status, error, units) == ('ok', None, 0) and peak <= 16 * part
    status, error, units, peak = traced_peak(data, '.pptx', Limits(xml_max_tree_bytes=part // 2))
    assert (status, error) == ('too_large', 'xml_tree') and peak <= 3 * part


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


def test_xlsx_memory_stays_near_the_part_size():
    """1 MiB of empty rows: xlsx-1 kept a cleared shell per row (about 15 times the part); the stream keeps
    the part and one chunk of events: peak <= 3 x the part (k = 3)."""
    part = 2**20
    sheet = make_ooxml._sheet('<row/>' * (part // 6))
    data = make_ooxml.xlsx_package([('시트', 'xl/worksheets/sheet1.xml', sheet)], None)
    status, error, units, peak = traced_peak(data, '.xlsx', Limits())
    assert (status, error, units) == ('ok', None, 0) and peak <= 3 * part


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

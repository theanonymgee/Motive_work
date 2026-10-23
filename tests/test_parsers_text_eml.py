"""TXT/MD/CSV and EML parsers (design M1 §5.7, §5.8, §11 ③). Documents are generated, never committed."""
import re

import pytest

from fixtures import LINES_KO, RESERVED_DOMAINS, SENTINEL, SENTINEL_ASCII, Expected, make_eml, make_text
from workmcp import locator, parsers
from workmcp.parsers import Limits, eml, text

TEXT_CASES = {item.rel_path: (item, data) for item, data in make_text.build()}
EML_CASES = {item.rel_path: (item, data) for item, data in make_eml.build()}
ALL_CASES = {**TEXT_CASES, **EML_CASES}


def ext_of(rel_path: str) -> str:
    return '.' + rel_path.rsplit('.', 1)[1]


def check(item, data, limits=None):
    """Parse ``data`` like the collector and compare with what the generator promised."""
    result = parsers.parse(data, ext_of(item.rel_path), limits or Limits())
    assert (result.parser, result.status, result.error) == (item.parser, item.status, item.error)
    assert result.encoding == item.encoding
    assert len(result.units) == item.unit_count
    assert [unit.ordinal for unit in result.units] == list(range(1, len(result.units) + 1))
    for ordinal, (kind, unit, unit_text) in item.units_sample.items():
        got = result.units[ordinal - 1]
        assert (got.kind, got.unit, got.text) == (kind, unit, unit_text)
    return result


# ---- TXT / MD / CSV ------------------------------------------------------------------------------

@pytest.mark.parametrize('rel_path', [
    'docs/notes_utf8.txt', 'docs/notes_bom.md', 'docs/notes_cp949.txt', 'docs/notes_utf16.txt',
    'docs/notes_crlf.txt', 'docs/notes_mixed.md', 'docs/ascii.txt'])
def test_text_units_and_encodings(rel_path):
    item, data = TEXT_CASES[rel_path]
    result = check(item, data)
    assert result.parser_version == text.VERSION


def test_text_encodings_are_what_the_ladder_says():
    assert {path: item.encoding for path, (item, _) in TEXT_CASES.items() if item.status == 'ok' and item.encoding} == {
        'docs/notes_utf8.txt': 'utf-8', 'docs/notes_bom.md': 'utf-8-sig', 'docs/notes_cp949.txt': 'cp949',
        'docs/notes_utf16.txt': 'utf-16', 'docs/notes_crlf.txt': 'utf-8', 'docs/notes_mixed.md': 'utf-8',
        'docs/long_line.txt': 'utf-8', 'docs/ascii.txt': 'utf-8', 'docs/table_utf8.csv': 'utf-8',
        'docs/table_cp949.csv': 'cp949'}
    # The generated CP949 bytes are not valid UTF-8, so the ladder reaches step 4 for the right reason.
    with pytest.raises(UnicodeDecodeError):
        TEXT_CASES['docs/notes_cp949.txt'][1].decode('utf-8')


def test_text_blank_lines_keep_their_numbers_and_edges_are_stripped():
    result = parsers.parse(b'  first  \n\n\t\n  \nlast\n', '.txt', Limits())
    assert [(u.unit['line'], u.text) for u in result.units] == [(1, 'first'), (5, 'last')]


def test_text_line_split_ignores_unicode_separators():
    # VT, FF, NEL and U+2028 are not line breaks for editors' line numbers; only CRLF, CR and LF are.
    data = 'a\x0bb\x0cc\x85d e\nsecond'.encode()
    result = parsers.parse(data, '.txt', Limits())
    assert [(u.unit['line'], u.text) for u in result.units] == [(1, 'a\x0bb\x0cc\x85d e'), (2, 'second')]


def test_text_long_line_is_one_unit():
    item, data = TEXT_CASES['docs/long_line.txt']
    result = check(item, data)
    assert len(result.units[0].text) == len(LINES_KO[0]) + make_text.LONG_LINE_CHARS


def test_text_rejects_binary_and_unknown_encoding():
    for rel_path in ('edge/binary.txt', 'edge/unknown_encoding.txt'):
        check(*TEXT_CASES[rel_path])
    # UTF-16 without a BOM is full of NUL bytes: binary. A BOM followed by an odd byte count is no
    # text in any step of the ladder.
    assert parsers.parse('가나다'.encode('utf-16-le'), '.txt', Limits()).error == 'binary_content'
    assert parsers.parse(b'\xff\xfe\xff', '.txt', Limits()).error == 'encoding_unknown'
    assert parsers.parse(b'\xfe\xff\x00', '.md', Limits()).error == 'encoding_unknown'
    # Invalid as UTF-8, and a lead byte without its trail byte at the end for CP949.
    assert parsers.parse(b'abc \xb0', '.txt', Limits()).error == 'encoding_unknown'


@pytest.mark.parametrize('ext', ['.txt', '.md', '.csv', '.eml'])
def test_empty_file_is_ok_with_no_units(ext):
    check(Expected(f'edge/empty{ext}', parsers.parser_for(ext).name, 'ok'), b'')


def test_text_unit_limit_stops_early():
    result = parsers.parse(b'a\nb\nc\n', '.txt', Limits(max_units=2))
    assert (result.status, result.error, result.units) == ('too_large', 'limit_units', ())


@pytest.mark.parametrize('rel_path', ['docs/table_utf8.csv', 'docs/table_cp949.csv'])
def test_csv_rows_and_column_limit(rel_path):
    item, data = TEXT_CASES[rel_path]
    result = check(item, data)
    assert result.parser_version == text.CSV_VERSION
    assert SENTINEL in result.units[3].text
    # Row 3 is one record over two physical lines; the blank record 5 is skipped and row 6 keeps its number.
    assert [u.unit['row'] for u in result.units] == [1, 2, 3, 4, 6]
    wide = make_text.csv_wide_row(6)
    assert parsers.parse(wide, '.csv', Limits(csv_max_columns=5)).error == 'csv_columns'
    ok = parsers.parse(wide, '.csv', Limits(csv_max_columns=6))
    assert ok.status == 'ok' and ok.units[0].unit['columns'] == [1, 2, 3, 4, 5, 6]


def test_csv_skips_rows_without_content():
    result = parsers.parse(b'a,b\n,\n"",\n  , \nc,d\n', '.csv', Limits())
    assert [(u.unit['row'], u.text) for u in result.units] == [(1, 'a,b'), (5, 'c,d')]


# ---- EML -----------------------------------------------------------------------------------------

@pytest.mark.parametrize('rel_path', sorted(EML_CASES))
def test_eml_headers_plain_html_only_attachment_and_cp949(rel_path):
    item, data = EML_CASES[rel_path]
    result = check(item, data)
    assert result.parser_version == eml.VERSION
    if item.status == 'ok':
        assert result.units[0].kind == 'mail_header'
        assert [u.kind for u in result.units[1:]] == ['mail'] * (len(result.units) - 1)


def test_eml_subject_and_names_are_decoded_from_rfc2047_words():
    data = make_eml.plain_message()
    assert b'=?utf-8?' in data.lower()  # the generated headers really are encoded words
    header = parsers.parse(data, '.eml', Limits()).units[0].text
    assert f'Subject: {make_eml.SUBJECT}' in header.split('\n')


def test_eml_multipart_numbers_parts_from_one_and_skips_attachments_and_html():
    item, data = EML_CASES['mail/multipart.eml']
    result = check(item, data)
    assert {u.unit['part'] for u in result.units[1:]} == {3}  # mixed=1, alternative=2, plain=3, html=4, attachment=5
    assert not any('첨부' in u.text or '스크립트' in u.text or '숨겨진' in u.text for u in result.units)


def test_eml_html_only_drops_script_style_title_and_breaks_lines_at_blocks():
    item, data = EML_CASES['mail/html_only.eml']
    result = check(item, data)
    joined = '\n'.join(u.text for u in result.units[1:])  # the body; the header has <address>
    assert not any(word in joined for word in ('스크립트', '숨겨진', 'color', '<', '&amp;'))


def test_eml_html_is_read_when_the_only_plain_part_is_an_attachment():
    result = parsers.parse(make_eml.html_with_text_attachment_message(), '.eml', Limits())
    assert {u.unit['part'] for u in result.units[1:]} == {2}
    assert not any('첨부에만' in u.text for u in result.units)


def test_eml_without_headers_or_with_only_other_headers_is_rejected():
    assert parsers.parse(make_eml.NO_HEADERS, '.eml', Limits()).error == 'eml_no_headers'
    assert parsers.parse(b'X-Note: synthetic\n\nbody\n', '.eml', Limits()).error == 'eml_no_headers'
    assert parsers.parse(b'Subject: only a subject\n\n', '.eml', Limits()).units[0].text == 'Subject: only a subject'


def test_eml_part_limit():
    data = make_eml.many_parts(4)
    assert parsers.parse(data, '.eml', Limits()).status == 'ok'
    assert parsers.parse(data, '.eml', Limits(eml_max_parts=4)).error == 'eml_count'  # container + 4 parts = 5
    assert parsers.parse(data, '.eml', Limits(eml_max_parts=5)).status == 'ok'


def test_eml_unknown_charset_is_read_not_raised():
    data = (b'From: a@example.com\nSubject: x\nContent-Type: text/plain; charset="x-no-such-charset"\n'
            b'Content-Transfer-Encoding: 8bit\n\n\xed\x95\x9c\xea\xb8\x80 body\n')
    result = parsers.parse(data, '.eml', Limits())
    assert result.status == 'ok' and result.units[-1].text.endswith('body')


# ---- all formats of this module -----------------------------------------------------------------

def test_generated_documents_follow_the_synthetic_rules():
    for rel_path, (item, data) in ALL_CASES.items():
        if item.status == 'ok' and item.unit_count:
            result = parsers.parse(data, ext_of(rel_path), Limits())
            assert any(SENTINEL in unit.text or SENTINEL_ASCII in unit.text for unit in result.units), rel_path
        for address in re.findall(rb'[\w.+-]+@([\w.-]+)', data):
            assert address.decode() in RESERVED_DOMAINS, rel_path


@pytest.mark.parametrize('rel_path', sorted(ALL_CASES))
def test_parsers_are_deterministic(rel_path):
    item, data = ALL_CASES[rel_path]
    first = parsers.parse(data, ext_of(rel_path), Limits())
    assert parsers.parse(data, ext_of(rel_path), Limits()) == first
    assert parsers.parse(bytes(bytearray(data)), ext_of(rel_path), Limits()) == first
    rebuilt = {i.rel_path: d for i, d in (*make_text.build(), *make_eml.build())}
    assert rebuilt[rel_path] == data  # generators are deterministic too (binding re-reads the same bytes)


@pytest.mark.parametrize('rel_path', sorted(path for path, (item, _) in ALL_CASES.items() if item.status == 'ok'))
def test_every_unit_passes_locator_check_unit(rel_path):
    item, data = ALL_CASES[rel_path]
    units, _ = parsers.parser_for(ext_of(rel_path)).parse(data, Limits()) if data else ([], None)
    assert len(units) == item.unit_count
    for unit in units:
        assert locator.check_unit(unit.unit) == unit.unit
        assert unit.kind == unit.unit['kind'] and unit.text == unit.text.strip() and unit.text


def test_parser_versions_follow_the_format():
    for ext, name in (('.txt', 'text'), ('.md', 'text'), ('.csv', 'csv'), ('.eml', 'eml')):
        parser = parsers.parser_for(ext)
        assert parser.name == name and re.fullmatch(rf'{name}-[1-9][0-9]*', parser.version)

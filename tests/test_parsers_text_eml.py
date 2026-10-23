"""TXT/MD/CSV and EML parsers (design M1 §5.7, §5.8, §11 ③). Documents are generated, never committed."""
import csv
import gc
import io
import random
import re
import tracemalloc
from email import policy
from email.parser import BytesParser

import pytest

from fixtures import LINES_KO, RESERVED_DOMAINS, SENTINEL, SENTINEL_ASCII, Expected, make_eml, make_text
from workmcp import locator, parsers
from workmcp.parsers import Limits, _container, eml, text

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


@pytest.mark.parametrize('inline', [False, True], ids=['attached', 'inline'])
def test_eml_parts_of_an_embedded_message_are_not_read(inline):
    """Review M1-L9 (P13): msg.walk() goes into a message/rfc822 part; its body is not this mail's body."""
    data = make_eml.forwarded_message(inline=inline)
    disposition = BytesParser(policy=policy.default).parsebytes(data).get_payload(1).get_content_disposition()
    assert disposition == (None if inline else 'attachment')
    result = parsers.parse(data, '.eml', Limits())
    assert result.status == 'ok'
    assert [(u.unit, u.text) for u in result.units[1:]] == [
        ({'kind': 'mail', 'part': 2, 'line': n}, line) for n, line in enumerate(LINES_KO[:2], 1)]
    assert not any(make_eml.ATTACHED_LINE in u.text or '전달된' in u.text for u in result.units)


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
    units, _ = parsers.parser_for(ext_of(rel_path)).parse(data, Limits(), _container.Budget(Limits())) if data else ([], None)
    assert len(units) == item.unit_count
    for unit in units:
        assert locator.check_unit(unit.unit) == unit.unit
        assert unit.kind == unit.unit['kind'] and unit.text == unit.text.strip() and unit.text


def test_parser_versions_follow_the_format():
    for ext, name in (('.txt', 'text'), ('.md', 'text'), ('.csv', 'csv'), ('.eml', 'eml')):
        parser = parsers.parser_for(ext)
        assert parser.name == name and re.fullmatch(rf'{name}-[1-9][0-9]*', parser.version)


# Parser resource design §3.9, §3.10 (budget charges)
def peak_of(data: bytes, ext: str, limits: Limits):
    parsers.parse(b'warm up', '.txt', Limits())
    gc.collect()
    tracemalloc.start()
    try:
        result = parsers.parse(data, ext, limits)
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_lines_are_the_same_as_splitting_the_whole_text():
    """The block reader gives exactly the lines of one split, line breaks of every kind across block cuts."""
    noise = random.Random(5)
    budget = _container.Budget(Limits())
    for _ in range(30):
        pieces = []
        while sum(map(len, pieces)) < 300_000:
            pieces.append('x' * noise.choice([0, 1, 5, 70_000, 140_000]) + noise.choice(['\n', '\r', '\r\n', '']))
        body = ''.join(pieces)
        assert list(text.lines(body, Limits(), budget)) == list(enumerate(re.split(r'\r\n|\r|\n', body), 1))
        with_ends = list(text._lines_with_ends(body, Limits(), budget, [0]))
        assert ''.join(with_ends) == body and with_ends == list(io.StringIO(body, newline=''))
    assert list(text.lines('', Limits(), budget)) == [(1, '')]


def test_csv_rows_are_the_same_as_reading_the_whole_text():
    noise = random.Random(7)
    cells = ['a', '', ' b ', '"q"', 'x,y', 'line\nbreak', 'cr\rbreak', 'crlf\r\nbreak', '가나다', 'z' * 70_000]
    for _ in range(20):
        rows = [[noise.choice(cells) for _ in range(noise.randrange(1, 6))] for _ in range(noise.randrange(1, 40))]
        out = io.StringIO()
        csv.writer(out, lineterminator=noise.choice(['\n', '\r\n', '\r'])).writerows(rows)
        body = out.getvalue()
        lines = text._lines_with_ends(body, Limits(), _container.Budget(Limits()), [0])
        assert list(csv.reader(lines)) == list(csv.reader(io.StringIO(body, newline='')))


def test_text_lines_are_lazy():
    """R18 (N4): every line was split into one list before the unit limit was checked (100 MiB of line
    breaks: 100 M list entries, 800 MiB). Lines are now read 64 Ki characters at a time and are events:
    1 M empty lines stop at 1,000 events with a peak of about the decoded text."""
    data = b'\n' * 2**20
    result, peak = peak_of(data, '.txt', Limits(max_events=1_000))
    assert (result.status, result.error) == ('too_large', 'limit_events') and peak < 2 * 2**20
    assert parsers.parse(data, '.txt', Limits()).status == 'ok'
    long_line = b'a' * 3_000 + b'\n' + b'b' * 100
    assert outcome(long_line, '.txt', Limits(max_unit_chars=2_999)) == ('too_large', 'unit_chars')
    assert outcome(long_line, '.txt', Limits(max_unit_chars=3_000)) == ('ok', None)
    huge = b'c' * (2**16 + 10_000)  # a line that runs past the first block cut
    assert outcome(huge, '.txt', Limits(max_unit_chars=5_000)) == ('too_large', 'unit_chars')
    assert outcome(huge, '.txt', Limits(max_text_chars=5_000)) == ('too_large', 'limit_chars')


def test_csv_columns_are_shared_and_rows_feed_lazily():
    """R19 (N6, N21): every row made its own ``list(range(1, k + 1))`` (1,000 columns: 29 KB a unit) and
    the whole text was copied into a StringIO. Rows of one width now share one list; the locators are the
    same JSON as before."""
    row = ','.join(f'v{n}' for n in range(100))
    data = ('\n'.join([row] * 1_000) + '\n').encode()
    result = parsers.parse(data, '.csv', Limits())
    assert result.status == 'ok' and len(result.units) == 1_000
    assert len({id(unit.unit['columns']) for unit in result.units}) == 1
    assert parsers.unit_json(result.units[5].unit) == parsers.unit_json(
        {'kind': 'cell', 'row': 6, 'columns': list(range(1, 101))})
    assert outcome(data, '.csv', Limits(max_events=500)) == ('too_large', 'limit_events')
    quoted = ('"' + 'x\n' * 3_000 + '",b\n').encode()  # one row gathered from 3,001 lines
    assert outcome(quoted, '.csv', Limits(max_unit_chars=5_000)) == ('too_large', 'unit_chars')
    assert outcome(quoted, '.csv', Limits(max_unit_chars=7_000)) == ('ok', None)


def test_eml_lines_and_html_are_charged():
    """R20 (N5, N19): mail lines are TXT lines (events, units charged as they are made), and the text the
    HTML stripper keeps is one event a piece."""
    lines = ('x\n' * 10_000).encode()
    message = b'From: a@example.com\nSubject: s\nContent-Type: text/plain\n\n' + lines
    assert outcome(message, '.eml', Limits(max_units=100)) == ('too_large', 'limit_units')
    assert outcome(message, '.eml', Limits(max_events=1_000)) == ('too_large', 'limit_events')
    html = b'From: a@example.com\nSubject: s\nContent-Type: text/html\n\n' + b'<p></p>' * 10_000
    result = parsers.parse(html, '.eml', Limits())
    assert result.status == 'ok' and [u.kind for u in result.units] == ['mail_header']
    assert outcome(html, '.eml', Limits(max_events=5_000)) == ('too_large', 'limit_events')
    split = b'From: a@example.com\nSubject: s\nContent-Type: text/html\n\n' + b'<p>a &amp; b</p>' * 100_000
    assert len(split) > 2**20
    assert parsers.parse(split, '.eml', Limits()).units[1].text == 'a & b'  # fed in pieces, same text


def outcome(data: bytes, ext: str, limits: Limits):
    result = parsers.parse(data, ext, limits)
    return result.status, result.error


def test_eml_lines_are_held_memory_and_the_parser_reads_on_the_clock(monkeypatch):
    """Probe N5: the message parser keeps every line of a part as a str (100 MiB of one-character lines:
    +2.5 GiB). The lines are held memory, 64 bytes each, before the message is parsed; the parser reads
    8 KiB at a time with the clock read on every read."""
    message = b'From: a@example.com\nSubject: s\nContent-Type: text/plain\n\n' + b'x\r\n' * 4_000
    assert outcome(message, '.eml', Limits(max_held_bytes=200_000)) == ('too_large', 'limit_memory')
    assert outcome(message, '.eml', Limits(max_held_bytes=400_000)) == ('ok', None)
    reads = []
    original = eml._Reader.read

    def read(self, size=-1):
        reads.append(size)
        return original(self, size)
    monkeypatch.setattr(eml._Reader, 'read', read)
    big = message + b'y' * 100_000 + b'\n'
    assert outcome(big, '.eml', Limits()) == ('ok', None) and len(reads) > 5
    assert outcome(big, '.eml', Limits(max_unit_chars=99_999)) == ('too_large', 'unit_chars')  # one raw line
    assert outcome(message, '.eml', Limits(max_events=4_000)) == ('too_large', 'limit_events')


def reference_mail_lines(data: bytes) -> list[tuple[int, str]]:
    """eml-3 before review v4: the whole part decoded, stripped of tags in one go, then split."""
    part = [p for p in BytesParser(policy=policy.default).parsebytes(data).walk() if p.get_content_maintype() == 'text'][0]
    try:
        body = part.get_content()
    except (LookupError, ValueError):
        body = (part.get_payload(decode=True) or b'').decode('utf-8', errors='replace')
    if part.get_content_type() == 'text/html':
        stripper = eml._HtmlText(_container.Budget(Limits()))
        stripper.feed(body)
        stripper.close()
        body = ''.join(stripper.out)
    return [(n, line.strip()) for n, line in enumerate(re.split(r'\r\n|\r|\n', body), 1) if line.strip()]


def test_eml_parts_are_read_in_pieces_with_the_same_lines():
    """Review v4 M1-11: decoding a part piece by piece, stripping HTML piece by piece and cutting lines as
    they come gives the lines (and numbers) of the whole part, whatever the charset, transfer encoding and
    where a CRLF or a character falls against the 1 MiB pieces."""
    import base64
    noise = random.Random(17)
    words = ['합성', 'word', '\U0001f600', '&amp;', 'é', '<b>bold</b>', '<p>', '</p>', '<br>', ' ', 'x' * 300]
    for charset, kind, transfer in [('utf-8', 'text/plain', '8bit'), ('utf-8', 'text/html', '8bit'),
                                    ('utf-8', 'text/html', 'base64'), ('cp949', 'text/plain', '8bit'),
                                    ('iso-8859-1', 'text/html', '8bit'), ('utf-16', 'text/plain', 'base64'),
                                    ('x-unknown', 'text/plain', '8bit')]:
        pieces, size = [], 0
        while size < 2_600_000:
            word = noise.choice(words)
            if charset in ('cp949', 'iso-8859-1') and word in ('\U0001f600', 'é'):
                word = '가' if charset == 'cp949' else 'ü'
            if charset == 'iso-8859-1' and word == '합성':
                word = 'ä'
            pieces.append(word + noise.choice(['', '', '\r\n', '\n', '\r']))
            size += len(pieces[-1])
        body = ''.join(pieces)
        raw = body.encode('utf-8' if charset == 'x-unknown' else charset)
        if transfer == 'base64':
            raw = base64.b64encode(raw)  # one long line: decoded, it has no line break to cut at for HTML
            raw = b'\r\n'.join(raw[n:n + 76] for n in range(0, len(raw), 76))
        data = (f'From: a@example.com\r\nSubject: s\r\nContent-Type: {kind}; charset={charset}\r\n'
                f'Content-Transfer-Encoding: {transfer}\r\n\r\n').encode() + raw
        result = parsers.parse(data, '.eml', Limits(max_units=10**6, max_text_chars=10**8))
        assert result.status == 'ok', (charset, kind, transfer, result.error)
        got = [(u.unit['line'], u.text) for u in result.units[1:]]
        assert got == reference_mail_lines(data), (charset, kind, transfer)


def test_eml_html_part_memory_follows_the_lines_not_the_part():
    """Review v4 M1-11 (G1): a 90 MiB HTML mail of ordinary paragraphs with one emoji took +1,355 MiB: the
    message's copy of the part, the decoded text (4 bytes a character once one emoji is in it), every
    piece the tag stripper kept and their join were alive together. Now only one decoded piece and the
    units are: a 6 MiB part (one emoji) peaks under twice the units' text."""
    line = '<p>' + 'Synthetic paragraph text with ordinary words. ' * 40 + '</p>\r\n'
    body = (line * (6 * 2**20 // len(line))).replace('Synthetic', '\U0001f600', 1)
    data = (b'From: a@example.com\r\nSubject: s\r\nContent-Type: text/html; charset=utf-8\r\n'
            b'Content-Transfer-Encoding: 8bit\r\n\r\n' + body.encode())
    result, peak = peak_of(data, '.eml', Limits())
    kept = sum(len(u.text) for u in result.units)  # ASCII after the first line: about a byte a character
    assert result.status == 'ok' and kept > 5 * 2**20 and peak < 2 * len(data) + 2 * kept

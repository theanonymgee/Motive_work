"""Safe zip/XML/OLE helpers (design M1 §5.3, §5.4, §15). Archives and XML are built in the test, in memory."""
import gc
import io
import struct
import time
import tracemalloc
import types
import warnings
import zipfile

import pytest

from workmcp import parsers
from workmcp.parsers import Limits, ParseFailure, _container

W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
DOCUMENT = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="{W_NS}"><w:body><w:p><w:r><w:t>합성 문단</w:t></w:r></w:p></w:body></w:document>'
            ).encode('utf-8')


def make_zip(members: dict[str, bytes] | list[tuple[str, bytes]], method=zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', method) as archive, warnings.catch_warnings():
        warnings.simplefilter('ignore')  # a repeated name (list form) is what some tests want
        for name, data in (members.items() if isinstance(members, dict) else members):
            archive.writestr(name, data)
    return buffer.getvalue()


def read_part(archive, name, limits, *, required=True):
    """A whole member through ``part_chunks`` (each piece at most 64 KiB)."""
    chunks = _container.part_chunks(archive, name, limits, required=required)
    if chunks is None:
        return None
    pieces = list(chunks)
    assert all(len(piece) <= _container._FEED_BYTES for piece in pieces)
    return b''.join(pieces)


def failure(action) -> tuple[str, str]:
    with pytest.raises(ParseFailure) as caught:
        action()
    return caught.value.status, caught.value.code


# zip
def test_open_zip_and_read_part():
    archive = _container.open_zip(make_zip({'word/document.xml': DOCUMENT}), Limits())
    assert read_part(archive, 'word/document.xml', Limits()) == DOCUMENT


@pytest.mark.parametrize('data', [b'PK\x03\x04' + b'\x00' * 40, b'PK\x05\x06garbage', b'not a zip at all', b''])
def test_damaged_archive_is_bad_zip(data):
    assert failure(lambda: _container.open_zip(data, Limits())) == ('failed', 'bad_zip')


def test_entry_count_limit():
    data = make_zip({f'part{n}.xml': b'<a/>' for n in range(3)})
    assert _container.open_zip(data, Limits(zip_max_entries=3))
    assert failure(lambda: _container.open_zip(data, Limits(zip_max_entries=2))) == ('too_large', 'zip_entries')


def test_declared_total_size_limit():
    data = make_zip({'a.xml': b' ' * 600, 'b.xml': b' ' * 400})
    assert _container.open_zip(data, Limits(zip_max_total_bytes=1000))
    assert failure(lambda: _container.open_zip(data, Limits(zip_max_total_bytes=999))) == ('too_large', 'zip_total')


def test_member_read_is_bounded_zip_bomb():
    """2 MiB of spaces compresses to a few KiB; the read stops at limit + 1 bytes."""
    data = make_zip({'word/document.xml': b' ' * (2 * 2**20)})
    assert len(data) < 16 * 2**10
    limits = Limits(zip_max_part_bytes=2**20)
    archive = _container.open_zip(data, limits)
    assert failure(lambda: read_part(archive, 'word/document.xml', limits)) == ('too_large', 'zip_part')
    exact = Limits(zip_max_part_bytes=2 * 2**20)
    assert len(read_part(_container.open_zip(data, exact), 'word/document.xml', exact)) == 2 * 2**20


def test_member_with_a_lying_size_header_cannot_exceed_the_header():
    """Headers claim 10 bytes for a 2 MiB member: the total check passes, the read is cut and fails its CRC."""
    data = bytearray(make_zip({'word/document.xml': b' ' * (2 * 2**20)}))
    data[22:26] = struct.pack('<I', 10)  # local header uncompressed size
    central = data.find(b'PK\x01\x02')
    data[central + 24:central + 28] = struct.pack('<I', 10)  # central directory uncompressed size
    limits = Limits(zip_max_total_bytes=100, zip_max_part_bytes=100)
    archive = _container.open_zip(bytes(data), limits)
    assert failure(lambda: read_part(archive, 'word/document.xml', limits)) == ('failed', 'bad_zip')


def test_corrupt_member_data_is_bad_zip():
    data = bytearray(make_zip({'word/document.xml': DOCUMENT * 50}))
    data[40:60] = b'\xff' * 20  # inside the deflate stream of the first member
    archive = _container.open_zip(bytes(data), Limits())
    assert failure(lambda: read_part(archive, 'word/document.xml', Limits())) == ('failed', 'bad_zip')


def test_two_members_with_one_name_are_bad_zip():
    data = make_zip([('Contents/section0.xml', b'<a/>'), ('Contents/section0.xml', b'<b/>')])
    assert failure(lambda: _container.open_zip(data, Limits())) == ('failed', 'bad_zip')
    assert _container.open_zip(make_zip([('a.xml', b'<a/>'), ('A.xml', b'<b/>')]), Limits())  # names are exact


def test_member_declared_empty_but_holding_data_fails_its_crc():
    data = bytearray(make_zip({'word/document.xml': DOCUMENT}))
    data[22:26] = struct.pack('<I', 0)
    central = data.find(b'PK\x01\x02')
    data[central + 24:central + 28] = struct.pack('<I', 0)
    archive = _container.open_zip(bytes(data), Limits())
    assert failure(lambda: read_part(archive, 'word/document.xml', Limits())) == ('failed', 'bad_zip')


def test_missing_part():
    archive = _container.open_zip(make_zip({'[Content_Types].xml': b'<Types/>'}), Limits())
    assert failure(lambda: read_part(archive, 'word/document.xml', Limits())) == ('failed', 'missing_part')
    assert read_part(archive, 'xl/sharedStrings.xml', Limits(), required=False) is None
    assert failure(lambda: read_part(archive, '[content_types].xml', Limits())) == (
        'failed', 'missing_part')  # names are exact


def test_stored_members_are_read_too():
    archive = _container.open_zip(make_zip({'mimetype': b'application/hwp+zip'}, zipfile.ZIP_STORED), Limits())
    assert read_part(archive, 'mimetype', Limits()) == b'application/hwp+zip'


# XML
def events(part, limits=None, clock=None) -> list[tuple]:
    """``(kind, tag, value)`` of every event of a part, a text run joined (it may come in several events)."""
    found = []
    for kind, tag, value in _container.XmlStream(part, limits or Limits(), clock):
        if kind == _container.TEXT and found and found[-1][0] == _container.TEXT:
            found[-1] = (kind, None, found[-1][2] + value)
        else:
            found.append((kind, tag, value))
    return found


def traced(action) -> tuple[object, int]:
    """``(result or (status, code), tracemalloc peak bytes)`` of one call; its input is made before tracing."""
    gc.collect()
    tracemalloc.start()
    try:
        try:
            result = action()
        except ParseFailure as caught:
            result = (caught.status, caught.code)
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_stream_gives_namespaced_tags_attributes_and_text_and_skips_comments():
    part = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W_NS}" xmlns:r="urn:synthetic:r">'
            '<w:p w:a="1" b="2" r:id="x">합성<w:br/>문단<!-- note --><?pi data?> 끝</w:p></w:document>').encode()
    w = f'{{{W_NS}}}'
    assert events(part) == [
        ('start', w + 'document', {}), ('start', w + 'p', {w + 'a': '1', 'b': '2', '{urn:synthetic:r}id': 'x'}),
        ('text', None, '합성'), ('start', w + 'br', {}), ('end', w + 'br', None), ('text', None, '문단 끝'),
        ('end', w + 'p', None), ('end', w + 'document', None)]
    utf16 = '<?xml version="1.0" encoding="UTF-16"?><r>합성</r>'.encode('utf-16')
    assert events(utf16) == [('start', 'r', {}), ('text', None, '합성'), ('end', 'r', None)]


def test_path_lists_the_open_tags_and_events_cross_feed_pieces():
    cells = ''.join(f'<c r="A{n}"><v>{n}</v></c>tail{n}' for n in range(1, 6001))
    part = f'<sheetData>{cells}</sheetData>'.encode('utf-8')
    assert len(part) > 2 * _container._FEED_BYTES
    stream = _container.XmlStream(part, Limits())
    paths = {'sheetData': [], 'c': ['sheetData'], 'v': ['sheetData', 'c']}
    seen, values, tails = [], {}, []
    for kind, tag, value in stream:
        if kind == 'text':
            if stream.path[-1] == 'v':
                values[seen[-1]] = values.get(seen[-1], '') + value
            else:
                assert stream.path == ['sheetData']
                tails.append(value)
            continue
        assert stream.path == paths[tag]  # the element itself is not in the path at its start or end
        if kind == 'start' and tag == 'c':
            seen.append(value['r'])  # attributes come with the start
    assert seen == [f'A{n}' for n in range(1, 6001)] and values == {f'A{n}': str(n) for n in range(1, 6001)}
    assert ''.join(tails) == ''.join(f'tail{n}' for n in range(1, 6001))


def test_stream_reads_pieces_from_an_iterable():
    part = b'<r>' + b'<a>x</a>' * 50_000 + b'</r>'
    pieces = (part[n:n + 1000] for n in range(0, len(part), 1000))
    assert events(pieces) == events(part)


BILLION_LAUGHS = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">'
                  b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]><r>&c;</r>')
EXTERNAL = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY e SYSTEM "file:///C:/synthetic.txt">]><r>&e;</r>'


@pytest.mark.parametrize('part', [
    BILLION_LAUGHS,
    EXTERNAL,
    b'<!DOCTYPE r><r/>',
    b'<r><!ENTITY x "y"></r>',
    '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r><r/>'.encode('utf-16'),
    '<!DOCTYPE r><r/>'.encode('utf-16-be'),
], ids=['billion_laughs', 'external_entity', 'doctype', 'entity', 'utf16_bom', 'utf16_be'])
def test_dtd_and_entities_are_refused(part):
    assert failure(lambda: events(part)) == ('failed', 'xml_dtd')


def test_dtd_marker_across_two_pieces_is_refused():
    part = b'<r>' + b' ' * (_container._FEED_BYTES - 6) + b'<!DOCTYPE r></r>'  # the marker straddles 64 KiB
    assert part.index(b'<!DOCTYPE') < _container._FEED_BYTES < part.index(b'<!DOCTYPE') + 9
    assert failure(lambda: events(part)) == ('failed', 'xml_dtd')


def test_doctype_is_refused_by_the_parser_too(monkeypatch):
    """Second layer: with the byte check out of the way, expat's doctype callback still refuses the declaration."""
    monkeypatch.setattr(_container, 'reject_dtd', lambda part: None)
    assert failure(lambda: events(BILLION_LAUGHS)) == ('failed', 'xml_dtd')
    assert failure(lambda: events('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r><r/>'.encode('utf-16'))) == (
        'failed', 'xml_dtd')


@pytest.mark.parametrize('part', [b'<r><a></r>', b'', b'<r>&undefined;</r>', b'not xml',
                                  b'<p:r xmlns:q="urn:q"/>',
                                  '<?xml version="1.0" encoding="cp037"?><r/>'.encode('cp037')])
def test_malformed_xml_is_invalid(part):
    assert failure(lambda: events(part)) == ('failed', 'xml_invalid')


def test_stream_reports_late_damage():
    stream = _container.XmlStream(b'<r>' + b'<a/>' * 10 + b'<b>', Limits())
    with pytest.raises(ParseFailure) as caught:
        for _ in stream:
            pass
    assert (caught.value.status, caught.value.code) == ('failed', 'xml_invalid')


def test_check_root_tells_strict_and_foreign_roots_apart():
    expected = f'{{{W_NS}}}document'
    assert _container.check_root(events(DOCUMENT)[0][1], expected) is None
    assert failure(lambda: _container.check_root('{http://purl.oclc.org/ooxml/wordprocessingml/main}document',
                                                 expected)) == ('unsupported', 'ooxml_strict')
    assert failure(lambda: _container.check_root('document', expected)) == ('unsupported', 'bad_signature')


# XML structure limits (review M1-3, M1-4)
def nested(depth: int) -> bytes:
    return b'<a>' * depth + b'</a>' * depth


def test_depth_limit():
    assert len(events(nested(10), Limits(xml_max_depth=10))) == 20
    assert failure(lambda: events(nested(11), Limits(xml_max_depth=10))) == ('too_large', 'xml_depth')
    assert len(events(nested(256))) == 512  # the default
    assert failure(lambda: events(nested(257))) == ('too_large', 'xml_depth')


def test_deep_nesting_is_refused_at_once_in_constant_memory():
    """Review M1-3: ``iter_xml`` kept every open element and walked the whole open path after each piece, so
    a part nested 100,000 deep took memory per level and time per level squared (1M levels: +514 MiB, 68 s).
    The stream refuses it at depth 257, after one piece: peak well under 1 MiB, far under a second."""
    part = nested(100_000)
    started = time.perf_counter()
    result, peak = traced(lambda: events(part))
    assert result == ('too_large', 'xml_depth') and peak < 2**20 and time.perf_counter() - started < 1


def test_attribute_limit_counts_attributes_and_namespace_declarations():
    attributes = ' '.join(f'a{n}="v"' for n in range(10)).encode()
    assert events(b'<r ' + attributes + b'/>', Limits(xml_max_attributes=10))
    assert failure(lambda: events(b'<r ' + attributes + b' b="v"/>', Limits(xml_max_attributes=10))) == (
        'too_large', 'xml_attributes')
    assert failure(lambda: events(b'<r ' + attributes + b' xmlns:p="urn:p"/>', Limits(xml_max_attributes=10))) == (
        'too_large', 'xml_attributes')
    assert events(b'<r ' + attributes + b'><a xmlns:p="urn:p"/></r>', Limits(xml_max_attributes=10))  # per element


def test_attribute_bomb_is_refused_in_bounded_memory():
    """Review M1-4 (B6): one element with a huge number of distinct attributes. The old stream kept the
    element and every name (about 30 times the part: 64 MiB -> +1.9 GiB). Now a start tag over
    ``xml_max_markup_bytes`` is refused while expat is still reading it, and one under that limit is
    refused by its attribute count; either way memory is bounded by the 1 MiB start tag, not the part."""
    huge = b'<r ' + b' '.join(b'a%07d=""' % n for n in range(400_000)) + b'/>'  # 5 MiB start tag
    result, peak = traced(lambda: events(huge))
    assert result == ('too_large', 'xml_markup') and peak < 4 * 2**20
    many = b'<r ' + b' '.join(b'a%06d=""' % n for n in range(60_000)) + b'/>'  # 0.6 MiB start tag
    result, peak = traced(lambda: events(many))
    assert result == ('too_large', 'xml_attributes') and peak < 24 * 2**20


def test_markup_limit_bounds_what_expat_holds_for_one_tag_comment_or_instruction():
    """A start tag, comment or processing instruction is buffered whole by expat until it ends; one that
    is still open after more than ``xml_max_markup_bytes`` is refused (checked between 64 KiB pieces)."""
    size = 300_000
    limits = Limits(xml_max_markup_bytes=200_000)
    for part in (b'<r><!--' + b'x' * size + b'--></r>', b'<r><?pi ' + b'x' * size + b'?></r>',
                 b'<r a="' + b'x' * size + b'"/>'):
        assert failure(lambda: events(part, limits)) == ('too_large', 'xml_markup')
        assert events(part, Limits(xml_max_markup_bytes=400_000))
    text = b'<r>' + b'x' * size + b'</r>'  # text is handed out piece by piece, never held
    assert events(text, limits) == [('start', 'r', {}), ('text', None, 'x' * size), ('end', 'r', None)]
    assert events(b'<r/>' + b' ' * size, limits)  # nor is white space after the root
    long_value = b'<r a="' + b'x' * (8 * 2**20) + b'"/>'
    result, peak = traced(lambda: events(long_value))
    assert result == ('too_large', 'xml_markup') and peak < 4 * 2**20  # the default is 1 MiB


def test_name_limit_counts_distinct_element_and_attribute_names_with_their_prefix():
    part = b'<a:r xmlns:a="urn:x" xmlns:b="urn:x"><a:x/><b:x c="1"/><a:x/></a:r>'
    assert [tag for kind, tag, _ in events(part) if kind == 'start'] == ['{urn:x}r', '{urn:x}x', '{urn:x}x', '{urn:x}x']
    # xmlns:a, xmlns:b, a:r, a:x, b:x, c: two prefixes of one namespace are two names (expat stores both)
    assert events(part, Limits(xml_max_names=6))
    assert failure(lambda: events(part, Limits(xml_max_names=5))) == ('too_large', 'xml_names')


def test_name_bomb_is_refused_in_bounded_memory():
    """Review M1-4 (B7): distinct element names (``<x0000001/>``...) filled the name cache of the old stream."""
    part = b'<r>' + b''.join(b'<x%07d/>' % n for n in range(200_000)) + b'</r>'
    result, peak = traced(lambda: events(part))
    assert result == ('too_large', 'xml_names') and peak < 8 * 2**20


def test_text_node_limit_spans_pieces():
    limits = Limits(xml_max_text_chars=100_000)
    assert events(b'<r>' + b'x' * 100_000 + b'<a/>' + b'y' * 100_000 + b'</r>', limits)  # two runs
    assert failure(lambda: events(b'<r>' + b'x' * 100_001 + b'</r>', limits)) == ('too_large', 'xml_text')
    assert failure(lambda: events(b'<r><![CDATA[' + b'x' * 100_001 + b']]></r>', limits)) == ('too_large', 'xml_text')


def test_stream_memory_does_not_grow_with_the_part():
    """Review M1-1 again: the stream holds one 64 KiB piece and its events whatever the part's size (about
    1.6 MiB measured for 1 and 4 MiB parts). The old stream held about one part's worth of events and
    detached elements, so 2 MiB of empty elements peaked above 2 MiB."""
    part = b'<r>' + b'<a/>' * (2**19) + b'</r>'
    result, peak = traced(lambda: sum(1 for _ in _container.XmlStream(part, Limits())))
    assert result == 2 + 2 * 2**19 and peak < 2 * 2**20


class Countdown:
    """A clock that runs out after ``checks`` calls of ``check``."""

    def __init__(self, checks: int):
        self.left = checks

    def check(self):
        self.left -= 1
        if self.left < 0:
            raise ParseFailure('too_large', 'parse_time')


def test_time_limit_is_checked_after_every_piece():
    part = b'<r>' + b'<a/>' * 100_000 + b'</r>'  # 400 KiB: 7 pieces and the final call
    assert events(part, clock=Countdown(8))
    assert failure(lambda: events(part, clock=Countdown(7))) == ('too_large', 'parse_time')
    assert failure(lambda: events(part, Limits(parse_seconds=-1))) == ('too_large', 'parse_time')


def test_clock_runs_out_after_parse_seconds(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(_container, 'time', types.SimpleNamespace(monotonic=lambda: now[0]))
    clock = _container.Clock(Limits(parse_seconds=60))
    now[0] = 160.0
    clock.check()
    now[0] = 160.5
    assert failure(clock.check) == ('too_large', 'parse_time')


def test_text_budget_stops_at_max_text_chars():
    budget = _container.TextBudget(Limits(max_text_chars=10))
    budget.take(6)
    budget.take(4)
    assert failure(lambda: budget.take(1)) == ('too_large', 'limit_chars')


# OLE
@pytest.mark.parametrize('data', [b'', b'PK\x03\x04', parsers.OLE_SIGNATURE, parsers.OLE_SIGNATURE + b'\x00' * 2048])
def test_ole_streams_of_non_ole_or_damaged_data_is_none(data):
    assert _container.ole_streams(data) is None

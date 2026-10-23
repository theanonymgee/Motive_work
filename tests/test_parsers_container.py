"""Safe zip/XML/OLE helpers (design M1 §5.3, §5.4). Archives and XML are built in the test, in memory."""
import io
import struct
import zipfile

import pytest

from workmcp import parsers
from workmcp.parsers import Limits, ParseFailure, _container

W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
DOCUMENT = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="{W_NS}"><w:body><w:p><w:r><w:t>합성 문단</w:t></w:r></w:p></w:body></w:document>'
            ).encode('utf-8')


def make_zip(members: dict[str, bytes], method=zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', method) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def failure(action) -> tuple[str, str]:
    with pytest.raises(ParseFailure) as caught:
        action()
    return caught.value.status, caught.value.code


# zip
def test_open_zip_and_read_part():
    archive = _container.open_zip(make_zip({'word/document.xml': DOCUMENT}), Limits())
    assert _container.read_part(archive, 'word/document.xml', Limits()) == DOCUMENT


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
    assert failure(lambda: _container.read_part(archive, 'word/document.xml', limits)) == ('too_large', 'zip_part')
    exact = Limits(zip_max_part_bytes=2 * 2**20)
    assert len(_container.read_part(_container.open_zip(data, exact), 'word/document.xml', exact)) == 2 * 2**20


def test_member_with_a_lying_size_header_cannot_exceed_the_header():
    """Headers claim 10 bytes for a 2 MiB member: the total check passes, the read is cut and fails its CRC."""
    data = bytearray(make_zip({'word/document.xml': b' ' * (2 * 2**20)}))
    data[22:26] = struct.pack('<I', 10)  # local header uncompressed size
    central = data.find(b'PK\x01\x02')
    data[central + 24:central + 28] = struct.pack('<I', 10)  # central directory uncompressed size
    limits = Limits(zip_max_total_bytes=100, zip_max_part_bytes=100)
    archive = _container.open_zip(bytes(data), limits)
    assert failure(lambda: _container.read_part(archive, 'word/document.xml', limits)) == ('failed', 'bad_zip')


def test_corrupt_member_data_is_bad_zip():
    data = bytearray(make_zip({'word/document.xml': DOCUMENT * 50}))
    data[40:60] = b'\xff' * 20  # inside the deflate stream of the first member
    archive = _container.open_zip(bytes(data), Limits())
    assert failure(lambda: _container.read_part(archive, 'word/document.xml', Limits())) == ('failed', 'bad_zip')


def test_missing_part():
    archive = _container.open_zip(make_zip({'[Content_Types].xml': b'<Types/>'}), Limits())
    assert failure(lambda: _container.read_part(archive, 'word/document.xml', Limits())) == ('failed', 'missing_part')
    assert _container.read_part(archive, 'xl/sharedStrings.xml', Limits(), required=False) is None
    assert failure(lambda: _container.read_part(archive, '[content_types].xml', Limits())) == (
        'failed', 'missing_part')  # names are exact


def test_stored_members_are_read_too():
    archive = _container.open_zip(make_zip({'mimetype': b'application/hwp+zip'}, zipfile.ZIP_STORED), Limits())
    assert _container.read_part(archive, 'mimetype', Limits()) == b'application/hwp+zip'


# XML
def test_parse_xml_keeps_namespaces_and_text():
    root = _container.parse_xml(DOCUMENT)
    assert root.tag == f'{{{W_NS}}}document'
    assert [t.text for t in root.iter(f'{{{W_NS}}}t')] == ['합성 문단']


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
    assert failure(lambda: _container.parse_xml(part)) == ('failed', 'xml_dtd')
    assert failure(lambda: list(_container.iter_xml(part))) == ('failed', 'xml_dtd')


def test_doctype_is_refused_by_the_tree_builder_too(monkeypatch):
    """Second layer: with the byte check out of the way, the builder still refuses the declaration."""
    monkeypatch.setattr(_container, 'reject_dtd', lambda part: None)
    assert failure(lambda: _container.parse_xml(BILLION_LAUGHS)) == ('failed', 'xml_dtd')
    assert failure(lambda: list(_container.iter_xml(BILLION_LAUGHS))) == ('failed', 'xml_dtd')


@pytest.mark.parametrize('part', [
    b'<r><a></r>', b'', b'<r>&undefined;</r>', b'not xml',
    b'<?xml version="1.0" encoding="cp037"?>' + '<r/>'.encode('cp037'),  # encoding expat cannot read
])
def test_malformed_xml_is_invalid(part):
    assert failure(lambda: _container.parse_xml(part)) == ('failed', 'xml_invalid')
    assert failure(lambda: list(_container.iter_xml(part))) == ('failed', 'xml_invalid')


def test_iter_xml_yields_elements_in_end_order_across_feed_chunks():
    cells = ''.join(f'<c r="A{n}"><v>{n}</v></c>' for n in range(1, 6001))
    part = f'<sheetData>{cells}</sheetData>'.encode('utf-8')
    assert len(part) > 2 * _container._FEED_BYTES
    seen = []
    for element in _container.iter_xml(part):
        if element.tag == 'c':
            seen.append((element.get('r'), element.find('v').text))
            element.clear()
    assert len(seen) == 6000 and seen[0] == ('A1', '1') and seen[-1] == ('A6000', '6000')


def test_iter_xml_reports_late_damage():
    elements = _container.iter_xml(b'<r>' + b'<a/>' * 10 + b'<b>')
    with pytest.raises(ParseFailure) as caught:
        for _ in elements:
            pass
    assert (caught.value.status, caught.value.code) == ('failed', 'xml_invalid')


# OLE
@pytest.mark.parametrize('data', [b'', b'PK\x03\x04', parsers.OLE_SIGNATURE, parsers.OLE_SIGNATURE + b'\x00' * 2048])
def test_ole_streams_of_non_ole_or_damaged_data_is_none(data):
    assert _container.ole_streams(data) is None

"""Synthetic HWP 5.x and HWPX generator (design M1 §7.3, appendix C). Standard library only.

HWP 5.x is a compound file (``cfb.write_cfb``) with ``FileHeader`` (256 bytes: signature, version,
flags), a minimal ``DocInfo`` and one ``BodyText/Section<n>`` stream per section holding records.
A paragraph is a ``PARA_HEADER`` (66, dummy payload) followed by ``PARA_TEXT`` (67, UTF-16LE text
ending in 0x000D); the stream is raw-deflate compressed when the header flag bit 0 is set.
A ``\\t`` in paragraph text is written the way HWP stores it: control code 9 followed by seven
more WCHARs (8 in all), which a reader skips, so the tab does not survive extraction.

HWPX is a zip with ``mimetype`` stored first, ``version.xml``, ``Contents/content.hpf`` and one
``Contents/section<n>.xml`` per section whose ``hp:p`` elements hold ``hp:run/hp:t`` text; a table
(``hp:tbl/hp:tr/hp:tc/hp:subList/hp:p``) sits inside a run of its outer paragraph.
"""
from __future__ import annotations

import io
import struct
import zipfile
import zlib

from . import LINES_EN, LINES_KO, SENTINEL, Expected
from .cfb import write_cfb

SIGNATURE = b'HWP Document File'
FLAG_COMPRESSED, FLAG_ENCRYPTED, FLAG_DISTRIBUTION = 1, 2, 4
TAG_DOCUMENT_PROPERTIES, TAG_PARA_HEADER, TAG_PARA_TEXT = 16, 66, 67
HP_NS = 'http://www.hancom.co.kr/hwpml/2011/paragraph'
HS_NS = 'http://www.hancom.co.kr/hwpml/2011/section'

_TAB = struct.pack('<8H', 9, *[0] * 7)


def record(tag: int, level: int, payload: bytes) -> bytes:
    """One record: 32-bit header (tag 10 bits, level 10 bits, size 12 bits; size 0xFFF means a 4-byte size follows)."""
    if len(payload) < 0xFFF:
        return struct.pack('<I', tag | (level << 10) | (len(payload) << 20)) + payload
    return struct.pack('<II', tag | (level << 10) | (0xFFF << 20), len(payload)) + payload


def text_payload(text: str) -> bytes:
    out = bytearray()
    for char in text:
        if char == '\t':
            out += _TAB
        elif char == '\n':
            out += struct.pack('<H', 10)
        else:
            out += char.encode('utf-16-le')
    return bytes(out) + b'\r\x00'


def para(text: str | None) -> bytes:
    """A paragraph; ``None`` is a paragraph without a ``PARA_TEXT`` record (as HWP writes an empty one)."""
    header = record(TAG_PARA_HEADER, 0, b'\x00' * 22)
    return header if text is None else header + record(TAG_PARA_TEXT, 1, text_payload(text))


def section_stream(paragraphs: list[str | None]) -> bytes:
    return b''.join(para(text) for text in paragraphs)


def deflate(raw: bytes) -> bytes:
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    return packer.compress(raw) + packer.flush()


def file_header(flags: int, *, signature: bytes = SIGNATURE, version: tuple[int, int, int, int] = (5, 0, 3, 0)) -> bytes:
    """256 bytes. ``version`` is (major, minor, build, revision); stored little-endian, so 5.0.3.0 is 00 03 00 05."""
    major, minor, build, revision = version
    return (signature.ljust(32, b'\x00') + bytes([revision, build, minor, major]) + struct.pack('<I', flags)
            + b'\x00' * 216)


def hwp_bytes(sections: list[list[str | None]], *, compressed: bool = True, flags: int = 0,
              signature: bytes = SIGNATURE, version: tuple[int, int, int, int] = (5, 0, 3, 0),
              raw_sections: dict[int, bytes] | None = None) -> bytes:
    """A HWP 5.x file. ``raw_sections`` replaces the uncompressed record stream of section ``n`` (damage cases)."""
    flags |= FLAG_COMPRESSED if compressed else 0
    pack = deflate if compressed else bytes
    streams = {'FileHeader': file_header(flags, signature=signature, version=version),
               'DocInfo': pack(record(TAG_DOCUMENT_PROPERTIES, 0, b'\x00' * 26))}
    for number, paragraphs in enumerate(sections):
        raw = section_stream(paragraphs)
        if raw_sections and number in raw_sections:
            raw = raw_sections[number]
        streams[f'BodyText/Section{number}'] = pack(raw)
    return write_cfb(streams)


def _xml_text(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def hp_para(text: str, *, table: list[list[str]] | None = None) -> str:
    """An ``hp:p``. ``\\t`` and ``\\n`` become ``hp:tab`` and ``hp:lineBreak``; ``table`` (rows of cell texts)
    is placed in a run of this paragraph, each cell holding one nested paragraph."""
    runs = []
    for piece in text.replace('\t', '\x01\t\x01').replace('\n', '\x01\n\x01').split('\x01'):
        if piece == '\t':
            runs.append('<hp:run charPrIDRef="0"><hp:tab width="4000" leader="0" type="1"/></hp:run>')
        elif piece == '\n':
            runs.append('<hp:run charPrIDRef="0"><hp:lineBreak/></hp:run>')
        elif piece:
            runs.append(f'<hp:run charPrIDRef="0"><hp:t>{_xml_text(piece)}</hp:t></hp:run>')
    if table:
        rows = ''.join('<hp:tr>' + ''.join(
            f'<hp:tc name="" header="0"><hp:subList><hp:p paraPrIDRef="0" styleIDRef="0">'
            f'<hp:run charPrIDRef="0"><hp:t>{_xml_text(cell)}</hp:t></hp:run></hp:p></hp:subList></hp:tc>'
            for cell in row) + '</hp:tr>' for row in table)
        runs.append(f'<hp:run charPrIDRef="0"><hp:tbl rowCnt="{len(table)}" colCnt="{len(table[0])}">{rows}'
                    '</hp:tbl></hp:run>')
    return f'<hp:p paraPrIDRef="0" styleIDRef="0">{"".join(runs)}</hp:p>'


def hwpx_section(paragraphs: list[str]) -> bytes:
    body = ''.join(paragraphs)
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<hs:sec xmlns:hs="{HS_NS}" xmlns:hp="{HP_NS}">{body}</hs:sec>').encode('utf-8')


def hwpx_bytes(sections: list[bytes], *, extra: dict[str, bytes] | None = None) -> bytes:
    """A HWPX zip; ``sections[n]`` is the XML of ``Contents/section<n>.xml``."""
    members = {'mimetype': b'application/hwp+zip', 'version.xml': b'<?xml version="1.0" encoding="UTF-8"?><hv:HCFVersion '
               b'xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version" tagetApplication="WORDPROCESSOR"/>',
               'Contents/content.hpf': b'<?xml version="1.0" encoding="UTF-8"?><opf:package '
               b'xmlns:opf="http://www.idpf.org/2007/opf/"/>'}
    for number, xml in enumerate(sections):
        members[f'Contents/section{number}.xml'] = xml
    members.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        def add(name: str, data: bytes, method: int) -> None:
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))  # fixed time: the same input gives the same bytes
            info.compress_type = method
            archive.writestr(info, data)

        add('mimetype', members.pop('mimetype'), zipfile.ZIP_STORED)
        for name, data in members.items():
            add(name, data, zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def _unit(section: int, index: int) -> dict:
    return {'kind': 'section_para', 'section': section, 'index': index}


def cases() -> dict[str, tuple[bytes, Expected]]:
    """``{rel_path: (bytes, Expected)}``: the HWP and HWPX documents of the synthetic bundle."""
    # PARA_TEXT records are numbered per section: paragraph 3 of section 1 is empty text (index kept, no unit).
    hwp_sections = [[LINES_KO[0], LINES_EN[1], '', LINES_KO[1] + '\n' + LINES_EN[2]],
                    ['항목\t값', None, LINES_KO[2]]]
    hwp_sample = {
        1: ('section_para', _unit(1, 1), LINES_KO[0]),
        2: ('section_para', _unit(1, 2), LINES_EN[1]),
        3: ('section_para', _unit(1, 4), LINES_KO[1] + '\n' + LINES_EN[2]),
        4: ('section_para', _unit(2, 1), '항목값'),
        5: ('section_para', _unit(2, 2), LINES_KO[2]),
    }
    xml_sections = [
        hwpx_section([hp_para(LINES_KO[0]),
                      hp_para(LINES_EN[1], table=[['머리 1', '머리 2'], ['값 1', '값 2']]),
                      hp_para(LINES_KO[1] + '\n' + LINES_EN[2])]),
        hwpx_section([hp_para('항목\t값'), hp_para(''), hp_para(LINES_KO[2])]),
    ]
    # hp:p order: 1 = LINES_KO[0], 2 = LINES_EN[1] (outer), 3-6 = the four cells, 7 = third paragraph.
    hwpx_sample = {
        1: ('section_para', _unit(1, 1), LINES_KO[0]),
        2: ('section_para', _unit(1, 2), LINES_EN[1]),
        3: ('section_para', _unit(1, 3), '머리 1'),
        6: ('section_para', _unit(1, 6), '값 2'),
        7: ('section_para', _unit(1, 7), LINES_KO[1] + '\n' + LINES_EN[2]),
        8: ('section_para', _unit(2, 1), '항목\t값'),
        9: ('section_para', _unit(2, 3), LINES_KO[2]),
    }
    one = [[LINES_KO[0]]]
    out = {
        'docs/hwp_compressed.hwp': (hwp_bytes(hwp_sections),
                                    Expected('docs/hwp_compressed.hwp', 'hwp', 'ok', None, 5, hwp_sample)),
        'docs/hwp_plain.hwp': (hwp_bytes(hwp_sections, compressed=False),
                               Expected('docs/hwp_plain.hwp', 'hwp', 'ok', None, 5, hwp_sample)),
        'docs/hwpx_tables.hwpx': (hwpx_bytes(xml_sections),
                                  Expected('docs/hwpx_tables.hwpx', 'hwpx', 'ok', None, 9, hwpx_sample)),
        'edge/hwp_encrypted.hwp': (hwp_bytes(one, flags=FLAG_ENCRYPTED),
                                   Expected('edge/hwp_encrypted.hwp', 'hwp', 'encrypted', 'hwp_encrypted')),
        'edge/hwp_distribution.hwp': (hwp_bytes(one, flags=FLAG_DISTRIBUTION),
                                      Expected('edge/hwp_distribution.hwp', 'hwp', 'encrypted', 'hwp_encrypted')),
        'edge/hwp_bad_signature.hwp': (hwp_bytes(one, signature=b'Not A HWP Document'),
                                       Expected('edge/hwp_bad_signature.hwp', 'hwp', 'unsupported', 'hwp_bad_header')),
        'edge/hwp_old_version.hwp': (hwp_bytes(one, version=(3, 0, 0, 0)),
                                     Expected('edge/hwp_old_version.hwp', 'hwp', 'unsupported', 'hwp_version')),
        'edge/hwp_bad_record.hwp': (hwp_bytes(one, raw_sections={0: record(TAG_PARA_TEXT, 1, b'\x41\x00')[:-1]}),
                                    Expected('edge/hwp_bad_record.hwp', 'hwp', 'failed', 'hwp_bad_record')),
        'edge/hwpx_no_section.hwpx': (hwpx_bytes([]),
                                      Expected('edge/hwpx_no_section.hwpx', 'hwpx', 'failed', 'missing_part')),
        'edge/hwpx_dtd.hwpx': (hwpx_bytes([b'<?xml version="1.0"?><!DOCTYPE hs:sec [<!ENTITY e "x">]><hs:sec/>']),
                               Expected('edge/hwpx_dtd.hwpx', 'hwpx', 'failed', 'xml_dtd')),
    }
    assert SENTINEL in LINES_KO[0]
    return out

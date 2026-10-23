"""HWP 5.x (OLE records) and HWPX (zip + XML) section paragraphs (design M1 §5.8).

Both formats give one unit per paragraph, ``{"kind": "section_para", "section": s, "index": i}``:

- ``s`` is the number in the stream or part name plus one (``BodyText/Section0`` and
  ``Contents/section0.xml`` are section 1); sections are read in numeric order.
- HWP 5.x: ``i`` counts the ``PARA_TEXT`` records of the section from 1, empty ones included.
  Table cells and text boxes arrive as ordinary paragraphs in the same stream, so they are included
  flat with no table coordinates. Headers, footers, footnotes and other streams are not read, and a
  tab (control code 9) is dropped like every other control.
- HWPX: ``i`` counts the ``hp:p`` elements of the section in document order, nested ones included.
  A paragraph owns only its own ``hp:t`` text, so a table inside a paragraph is not repeated in it.
"""
from __future__ import annotations

import importlib.metadata
import io
import re
import struct
import zlib

import olefile

from . import Limits, ParseFailure, Unit, _container

NAME = 'hwp'
VERSION = f'hwp-1/olefile-{importlib.metadata.version("olefile")}'
HWPX_NAME = 'hwpx'
HWPX_VERSION = 'hwpx-1'

_SIGNATURE = b'HWP Document File'
_HEADER_BYTES = 40
_FLAG_COMPRESSED, _FLAG_ENCRYPTED, _FLAG_DISTRIBUTION = 1, 2, 4
_TAG_PARA_TEXT = 67
# HWP 5.x control codes that occupy 8 WCHARs (code plus 7 of inline data); the others are one WCHAR.
_LONG_CONTROLS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23})
_HWP_SECTION = re.compile(r'Section([0-9]{1,9})')
_HWPX_SECTION = re.compile(r'Contents/section([0-9]{1,9})\.xml')
_HP = '{http://www.hancom.co.kr/hwpml/2011/paragraph}'
_P, _T = _HP + 'p', _HP + 't'
_INLINE = {_HP + 'tab': '\t', _HP + 'lineBreak': '\n'}


def _para_text(raw: bytes) -> str:
    chars = struct.unpack('<%dH' % (len(raw) // 2), raw[:len(raw) // 2 * 2])
    units, i = [], 0
    while i < len(chars):
        c = chars[i]
        if c in _LONG_CONTROLS:
            i += 8
            continue
        if c in (10, 13):
            units.append(10)
        elif c >= 32:
            units.append(c)
        i += 1
    # Code units are decoded together so that UTF-16 surrogate pairs combine; a lone one becomes U+FFFD.
    return struct.pack('<%dH' % len(units), *units).decode('utf-16-le', errors='replace')


def _inflate(raw: bytes, limit: int) -> bytes:
    inflater = zlib.decompressobj(-15)  # raw deflate; data after the end of the stream is ignored
    try:
        out = inflater.decompress(raw, limit + 1)
    except zlib.error:
        raise ParseFailure('failed', 'hwp_bad_stream') from None
    if len(out) > limit:
        raise ParseFailure('too_large', 'zip_part')
    if not inflater.eof:
        raise ParseFailure('failed', 'hwp_bad_stream')
    return out


def _records(raw: bytes):
    """Yield ``(tag, payload)`` of every record; a header or size running past the end is ``hwp_bad_record``."""
    pos, end = 0, len(raw)
    while pos < end:
        if end - pos < 4:
            raise ParseFailure('failed', 'hwp_bad_record')
        word = struct.unpack_from('<I', raw, pos)[0]
        tag, size = word & 0x3FF, (word >> 20) & 0xFFF
        pos += 4
        if size == 0xFFF:
            if end - pos < 4:
                raise ParseFailure('failed', 'hwp_bad_record')
            size = struct.unpack_from('<I', raw, pos)[0]
            pos += 4
        if size > end - pos:
            raise ParseFailure('failed', 'hwp_bad_record')
        yield tag, raw[pos:pos + size]
        pos += size


def _read_stream(ole: olefile.OleFileIO, path: str, limit: int) -> bytes:
    try:
        if ole.get_size(path) > limit:
            raise ParseFailure('too_large', 'zip_part')
        with ole.openstream(path) as stream:
            return stream.read()
    except MemoryError:
        raise
    except ParseFailure:
        raise
    except Exception:  # olefile raises several types on damaged sector chains
        raise ParseFailure('failed', 'hwp_bad_stream') from None


def parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except MemoryError:
        raise
    except Exception:
        raise ParseFailure('failed', 'ole_invalid') from None
    with ole:
        try:
            paths = ole.listdir(streams=True, storages=False)
        except MemoryError:
            raise
        except Exception:
            raise ParseFailure('failed', 'ole_invalid') from None
        if ['FileHeader'] not in paths:
            raise ParseFailure('unsupported', 'hwp_bad_header')
        header = _read_stream(ole, 'FileHeader', limits.zip_max_part_bytes)
        if len(header) < _HEADER_BYTES or not header.startswith(_SIGNATURE):
            raise ParseFailure('unsupported', 'hwp_bad_header')
        if header[35] != 5:  # major version, the high byte of the little-endian version word
            raise ParseFailure('unsupported', 'hwp_version')
        flags = struct.unpack_from('<I', header, 36)[0]
        if flags & (_FLAG_ENCRYPTED | _FLAG_DISTRIBUTION):
            raise ParseFailure('encrypted', 'hwp_encrypted')
        sections = []
        for path in paths:
            match = len(path) == 2 and path[0] == 'BodyText' and _HWP_SECTION.fullmatch(path[1])
            if match:
                sections.append((int(match.group(1)), '/'.join(path)))
        if len(sections) > limits.hwp_max_sections:
            raise ParseFailure('too_large', 'hwp_count')
        if not sections:
            raise ParseFailure('failed', 'hwp_bad_stream')
        units, total = [], 0
        for number, path in sorted(sections):
            raw = _read_stream(ole, path, limits.zip_max_part_bytes)
            if flags & _FLAG_COMPRESSED:
                raw = _inflate(raw, limits.zip_max_part_bytes)
            total += len(raw)
            if total > limits.zip_max_total_bytes:
                raise ParseFailure('too_large', 'zip_total')
            index = 0
            for tag, payload in _records(raw):
                if tag != _TAG_PARA_TEXT:
                    continue
                index += 1
                text = _para_text(payload).strip()
                if text:
                    units.append(Unit(0, 'section_para', {'kind': 'section_para', 'section': number + 1,
                                                          'index': index}, text))
    return units, None


def _paragraph_text(paragraph) -> str:
    """Text a paragraph owns: its ``hp:t`` (with inline tab and line break), not that of nested paragraphs."""
    parts = []
    stack = [iter(paragraph)]
    while stack:
        for child in stack[-1]:
            tag = child.tag
            if tag == _P:
                continue
            if tag == _T:
                parts.append(child.text or '')
                for inner in child:
                    parts.append(_INLINE.get(inner.tag, ''))
                    parts.append(inner.tail or '')
            elif tag in _INLINE:
                parts.append(_INLINE[tag])
            else:
                stack.append(iter(child))
                break
        else:
            stack.pop()
    return ''.join(parts)


def parse_hwpx(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    archive = _container.open_zip(data, limits)
    sections = []
    for name in archive.namelist():
        match = _HWPX_SECTION.fullmatch(name)
        if match:
            sections.append((int(match.group(1)), name))
    if len(sections) > limits.hwp_max_sections:
        raise ParseFailure('too_large', 'hwp_count')
    if not sections:
        raise ParseFailure('failed', 'missing_part')
    units = []
    for number, name in sorted(sections):
        root = _container.parse_xml(_container.read_part(archive, name, limits))
        index = 0
        for element in root.iter(_P):
            index += 1
            text = _paragraph_text(element).strip()
            if text:
                units.append(Unit(0, 'section_para', {'kind': 'section_para', 'section': number + 1, 'index': index},
                                  text))
    return units, None

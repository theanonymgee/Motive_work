"""HWP 5.x (OLE records) and HWPX (zip + XML) section paragraphs (design M1 §5.8).

Both formats give one unit per paragraph, ``{"kind": "section_para", "section": s, "index": i}``:

- ``s`` is the number in the stream or part name plus one (``BodyText/Section0`` and
  ``Contents/section0.xml`` are section 1); sections are read in numeric order. Only the plain
  decimal spelling is a section (``Section00`` or ``section01.xml`` is not), so two streams or parts
  can never give one section number.
- HWP 5.x: ``i`` counts the ``PARA_TEXT`` records of the section from 1, empty ones included.
  Table cells and text boxes arrive as ordinary paragraphs in the same stream, so they are included
  flat with no table coordinates. Headers, footers, footnotes and other streams are not read, and a
  tab (control code 9) is dropped like every other control. The text is cut out of the record bytes
  and decoded once (no per-character integers); a record over ``Limits.hwp_max_para_bytes`` is
  ``too_large/hwp_para``. One section's decompressed stream is bounded by ``zip_max_part_bytes`` and
  all of them by ``zip_max_total_bytes``.
- HWPX: ``i`` counts the ``hp:p`` elements of the section in document order, nested ones included.
  A paragraph owns only its own ``hp:t`` text, so a table inside a paragraph is not repeated in it.
  A section is read as a stream of element events (``_container.iter_xml``), never as a tree.
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
VERSION = f'hwp-2/olefile-{importlib.metadata.version("olefile")}'
HWPX_NAME = 'hwpx'
HWPX_VERSION = 'hwpx-2'

_SIGNATURE = b'HWP Document File'
_HEADER_BYTES = 40
_FLAG_COMPRESSED, _FLAG_ENCRYPTED, _FLAG_DISTRIBUTION = 1, 2, 4
_TAG_PARA_TEXT = 67
# HWP 5.x control codes that occupy 8 WCHARs (code plus 7 of inline data); the others are one WCHAR.
_LONG_CONTROLS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23})
_HWP_SECTION = re.compile(r'Section(0|[1-9][0-9]{0,8})')
_HWPX_SECTION = re.compile(r'Contents/section(0|[1-9][0-9]{0,8})\.xml')
_CONTROL = re.compile(rb'[\x00-\x1f]\x00')  # a WCHAR below 32 when the match starts at an even offset
_HP = '{http://www.hancom.co.kr/hwpml/2011/paragraph}'
_P, _T = _HP + 'p', _HP + 't'
_INLINE = {_HP + 'tab': '\t', _HP + 'lineBreak': '\n'}


def _para_text(raw) -> str:
    """Text of one ``PARA_TEXT`` payload (bytes or memoryview): WCHARs from 32 up, 10 and 13 as a line break.

    A control below 32 is one WCHAR, or eight for the codes in ``_LONG_CONTROLS`` (code plus inline data).
    """
    end = len(raw) - len(raw) % 2
    pieces, keep, scan = [], 0, 0
    while True:
        match = _CONTROL.search(raw, scan, end)
        if match is None:
            pieces.append(raw[keep:end])
            break
        at = match.start()
        if at % 2:  # the high byte of one WCHAR and the low byte of the next
            scan = at + 1
            continue
        pieces.append(raw[keep:at])
        code = raw[at]
        if code in (10, 13):
            pieces.append(b'\n\x00')
        keep = scan = min(at + (16 if code in _LONG_CONTROLS else 2), end)
    # Code units are decoded together so that UTF-16 surrogate pairs combine; a lone one becomes U+FFFD.
    return b''.join(pieces).decode('utf-16-le', errors='replace')


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
    """Yield ``(tag, payload)`` of every record, the payload as a memoryview (no copy); a header or size
    running past the end is ``hwp_bad_record``."""
    view = memoryview(raw)
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
        yield tag, view[pos:pos + size]
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
        units, total, chars = [], 0, 0
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
                if len(payload) > limits.hwp_max_para_bytes:
                    raise ParseFailure('too_large', 'hwp_para')
                text = _para_text(payload).strip()
                if text:
                    units.append(Unit(0, 'section_para', {'kind': 'section_para', 'section': number + 1,
                                                          'index': index}, text))
                    chars += len(text)
                    if len(units) > limits.max_units:
                        raise ParseFailure('too_large', 'limit_units')
                    if chars > limits.max_text_chars:
                        raise ParseFailure('too_large', 'limit_chars')
    return units, None


def _section_units(part, section: int, units: list, limits: Limits) -> None:
    """Units of one HWPX section, from a stream of element events.

    Each open element has a role towards its closest ``hp:p``: ``walk`` (its children count),
    ``text`` (an ``hp:t``: its text, then for each child the inline tab or line break and the
    child's tail) or ``skip`` (nothing inside counts, except nested paragraphs, which are their own).
    """
    found, roles, index, pending = [], [], 0, None
    for event, element, path in _container.iter_xml(part):
        if pending is not None:  # the tail of the last child of an hp:t is complete by now
            pieces, child = pending
            pieces.append(child.tail or '')
            pending = None
        tag = element.tag
        if event == 'start':
            parent = roles[-1] if roles else None
            if tag == _P:
                index += 1
                role = ('walk', [index, []])
            elif parent is None or parent[0] == 'skip':
                role = ('skip', None)
            elif parent[0] == 'text':
                parent[3].append(_INLINE.get(tag, ''))
                role = ('skip', None)
            else:  # the parent walks for a paragraph
                pieces = parent[1][1]
                if tag == _T:
                    role = ('text', parent[1], len(pieces), [])
                    pieces.append('')
                elif tag in _INLINE:
                    pieces.append(_INLINE[tag])
                    role = ('skip', None)
                else:
                    role = ('walk', parent[1])
            roles.append(role)
            continue
        role = roles.pop()
        if role[0] == 'text':
            role[1][1][role[2]] = (element.text or '') + ''.join(role[3])
        if tag == _P:
            number, pieces = role[1]
            text = ''.join(pieces).strip()
            if text:
                found.append((number, text))
                if len(units) + len(found) > limits.max_units:
                    raise ParseFailure('too_large', 'limit_units')
        if roles and roles[-1][0] == 'text':
            pending = (roles[-1][3], element)
    found.sort()  # a nested paragraph ends before the one around it
    for number, text in found:
        units.append(Unit(0, 'section_para', {'kind': 'section_para', 'section': section, 'index': number}, text))


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
        _section_units(_container.read_part(archive, name, limits), number + 1, units, limits)
    return units, None

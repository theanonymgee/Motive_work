"""HWP 5.x (OLE records) and HWPX (zip + XML) section paragraphs (design M1 §5.8).

Both formats give one unit per paragraph, ``{"kind": "section_para", "section": s, "index": i}``:

- ``s`` is the number in the stream or part name plus one (``BodyText/Section0`` and
  ``Contents/section0.xml`` are section 1); sections are read in numeric order. Only the plain
  decimal spelling is a section (``Section00`` or ``section01.xml`` is not), so two streams or parts
  can never give one section number.
- HWP 5.x: ``i`` counts the ``PARA_TEXT`` records of the section from 1, empty ones included.
  Table cells and text boxes arrive as ordinary paragraphs in the same stream, so they are included
  flat with no table coordinates. Headers, footers, footnotes and other streams are not read, and a
  tab (control code 9) is dropped like every other control. A section is inflated and cut into
  records 256 KiB at a time; only an unfinished ``PARA_TEXT`` record is buffered, and its text is cut
  out of the record bytes into one buffer (no list of pieces) and decoded once. A record over
  ``Limits.hwp_max_para_bytes`` is ``too_large/hwp_para``. One section's decompressed stream is bounded
  by ``zip_max_part_bytes`` and all of them by ``zip_max_total_bytes``. Every record and every run of
  control characters is an event of the file's budget (``too_large/limit_events``), the clock is read
  per 256 KiB piece, and a paragraph's text counts as text and a unit when it is kept.
- HWPX: ``i`` counts the ``hp:p`` elements of the section in document order, nested ones included.
  A paragraph owns only its own ``hp:t`` text, so a table inside a paragraph is not repeated in it.
  A section is read as a stream of events (``_container.XmlStream``), never as a tree. A text piece,
  tab or line break counts as text (``too_large/limit_chars``) and towards its paragraph's
  ``max_unit_chars`` (``unit_chars``); a paragraph with text counts as a unit when it ends.
"""
from __future__ import annotations

import importlib.metadata
import io
import re
import struct
import zlib
from collections.abc import Iterable, Iterator

import olefile

from . import Limits, ParseFailure, Unit, _container
from ._container import START, TEXT

NAME = 'hwp'
VERSION = f'hwp-4/olefile-{importlib.metadata.version("olefile")}'
HWPX_NAME = 'hwpx'
HWPX_VERSION = 'hwpx-4'

_SIGNATURE = b'HWP Document File'
_HEADER_BYTES = 40
_FLAG_COMPRESSED, _FLAG_ENCRYPTED, _FLAG_DISTRIBUTION = 1, 2, 4
_TAG_PARA_TEXT = 67
_PIECE_BYTES = 1 << 18  # decompressed section bytes handled at a time
_INPUT_BYTES = 1 << 16  # compressed bytes given to zlib at a time
# HWP 5.x control codes that occupy 8 WCHARs (code plus 7 of inline data); the others are one WCHAR.
_LONG_CONTROLS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23})
_HWP_SECTION = re.compile(r'Section(0|[1-9][0-9]{0,8})')
_HWPX_SECTION = re.compile(r'Contents/section(0|[1-9][0-9]{0,8})\.xml')
_CONTROL = re.compile(rb'[\x00-\x1f]\x00')  # a WCHAR below 32 when the match starts at an even offset
_SHORT_CONTROLS = bytes(sorted(set(range(32)) - _LONG_CONTROLS - {10, 13}))
# A run of controls that give no text, and a run of line breaks, matched from a WCHAR boundary (every
# alternative is whole WCHARs, so the match never leaves the WCHAR grid). At most 4,096 a match: the
# regular expression engine keeps state for every repetition of a group until the match ends.
_SKIPPED = re.compile(rb'(?:[' + re.escape(_SHORT_CONTROLS) + rb']\x00|['
                      + re.escape(bytes(sorted(_LONG_CONTROLS))) + rb']\x00(?:..){7}){1,4096}', re.S)
_LINE_BREAKS = re.compile(rb'(?:[\x0a\x0d]\x00){1,4096}')
_NEWLINES = b'\n\x00' * 4096
_STEPS = 4096  # steps of the control loop charged to the budget at a time
_HP = '{http://www.hancom.co.kr/hwpml/2011/paragraph}'
_P, _T = _HP + 'p', _HP + 't'
_INLINE = {_HP + 'tab': '\t', _HP + 'lineBreak': '\n'}


def _para_text(raw, budget: _container.Budget) -> str:
    """Text of one ``PARA_TEXT`` payload (bytes or memoryview): WCHARs from 32 up, 10 and 13 as a line break.

    A control below 32 is one WCHAR, or eight for the codes in ``_LONG_CONTROLS`` (code plus inline data).
    The text between controls goes straight into one buffer, at most the payload's size. A run of
    controls is passed in one regular-expression match, so the loop takes one step per run; every step
    is one event of the budget.
    """
    end = len(raw) - len(raw) % 2
    out, keep, scan, steps = bytearray(), 0, 0, 0
    search = _CONTROL.search
    while True:
        match = search(raw, scan, end)
        if match is None:
            break
        steps += 1
        if steps == _STEPS:
            budget.event(steps)
            steps = 0
        at = match.start()
        if at % 2:  # the high byte of one WCHAR and the low byte of the next
            scan = at + 1
            continue
        out += raw[keep:at]
        if raw[at] in (10, 13):
            run = _LINE_BREAKS.match(raw, at, end).end()
            out += _NEWLINES[:run - at]  # one line break per WCHAR of the run
        else:
            run = _SKIPPED.match(raw, at, end)
            run = run.end() if run else end  # a long control cut short by the end takes the rest
        keep = scan = run
    budget.event(steps)
    # Code units are decoded together so that UTF-16 surrogate pairs combine; a lone one becomes U+FFFD.
    if not keep:
        return str(raw[:end], 'utf-16-le', 'replace')
    out += raw[keep:end]
    return str(out, 'utf-16-le', 'replace')


def _inflated(raw: bytes, limit: int) -> Iterator[bytes]:
    """Raw deflate data as decompressed pieces of at most 256 KiB, fed 64 KiB of input at a time (so no
    call copies the rest of the input). Past ``limit`` bytes it is ``too_large/zip_part``; damaged or
    cut-short data is ``hwp_bad_stream``; data after the end of the deflate stream is ignored."""
    inflater = zlib.decompressobj(-15)
    view, pos, total, data = memoryview(raw), 0, 0, b''
    while True:
        if not data and pos < len(view):
            data, pos = view[pos:pos + _INPUT_BYTES], pos + _INPUT_BYTES
        try:
            out = inflater.decompress(data, _PIECE_BYTES)  # with no input left it hands out what zlib still holds
        except zlib.error:
            raise ParseFailure('failed', 'hwp_bad_stream') from None
        total += len(out)
        if total > limit:
            raise ParseFailure('too_large', 'zip_part')
        if out:
            yield out
        if inflater.eof:
            return
        data = inflater.unconsumed_tail
        if not out and not data and pos >= len(view):
            raise ParseFailure('failed', 'hwp_bad_stream')  # the input ran out before the end of the stream


def _pieces(raw: bytes) -> Iterator[memoryview]:
    view = memoryview(raw)
    for start in range(0, len(raw), _PIECE_BYTES):
        yield view[start:start + _PIECE_BYTES]


def _para_texts(pieces: Iterable, limits: Limits, budget: _container.Budget, total: list) -> Iterator:
    """The payload of every ``PARA_TEXT`` record of one section stream, read piece by piece.

    A ``PARA_TEXT`` record that runs into the next pieces is gathered into a buffer of its own size;
    any other record, and a ``PARA_TEXT`` over ``hwp_max_para_bytes``, is skipped without keeping it,
    so memory is one piece plus one paragraph. A header or size running past the end is
    ``hwp_bad_record``; a ``PARA_TEXT`` over the limit is ``too_large/hwp_para`` once it is complete.
    ``total`` (a one-item list) counts the section bytes of the whole file against
    ``zip_max_total_bytes``. Every record header read is one event of the budget; the clock is also
    read once per piece.
    """
    left = b''                 # an unfinished record header from the previous piece (under 8 bytes)
    skip, oversized = 0, False  # bytes of a skipped record still to come; whether it is an oversized PARA_TEXT
    record, filled = None, 0    # a PARA_TEXT record being gathered and how much of it is there
    for data in pieces:
        data = memoryview(data)  # slices below are views, not copies
        total[0] += len(data)
        if total[0] > limits.zip_max_total_bytes:
            raise ParseFailure('too_large', 'zip_total')
        budget.clock()
        if skip:
            used = min(skip, len(data))
            skip -= used
            if skip:
                continue
            if oversized:
                raise ParseFailure('too_large', 'hwp_para')
            data = data[used:]
        if record is not None:
            used = min(len(record) - filled, len(data))
            record[filled:filled + used] = data[:used]
            filled += used
            if filled < len(record):
                continue
            yield record
            record, data = None, data[used:]
        if left:
            data, left = memoryview(left + bytes(data)), b''
        pos, end = 0, len(data)
        while end - pos >= 4:
            budget.event()
            word = struct.unpack_from('<I', data, pos)[0]
            tag, size, header = word & 0x3FF, (word >> 20) & 0xFFF, 4
            if size == 0xFFF:
                if end - pos < 8:
                    break
                size, header = struct.unpack_from('<I', data, pos + 4)[0], 8
            start = pos + header
            text = tag == _TAG_PARA_TEXT
            if text and size > limits.hwp_max_para_bytes or not text and start + size > end:
                if start + size <= end:
                    raise ParseFailure('too_large', 'hwp_para')
                skip, oversized, pos = start + size - end, text, end
                break
            if start + size > end:  # the rest of this paragraph comes with the next pieces
                record, filled = bytearray(size), end - start
                record[:filled] = data[start:end]
                pos = end
                break
            if text:
                yield bytes(data[start:start + size])
            pos = start + size
        left = bytes(data[pos:])
    if skip or record is not None or left:
        raise ParseFailure('failed', 'hwp_bad_record')


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


def parse(data: bytes, limits: Limits, budget: _container.Budget) -> tuple[list[Unit], str | None]:
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
        units, total = [], [0]
        for number, path in sorted(sections):
            raw = _read_stream(ole, path, limits.zip_max_part_bytes)
            pieces = _inflated(raw, limits.zip_max_part_bytes) if flags & _FLAG_COMPRESSED else _pieces(raw)
            index = 0
            for payload in _para_texts(pieces, limits, budget, total):
                index += 1
                if len(payload) < 2:
                    continue  # an empty paragraph keeps its index
                text = _para_text(memoryview(payload), budget).strip()  # slices of a view are not copies
                if text:
                    if len(text) > limits.max_unit_chars:  # hwp_max_para_bytes keeps it under the default
                        raise ParseFailure('too_large', 'unit_chars')
                    budget.text(len(text))
                    budget.unit()
                    units.append(Unit(0, 'section_para', {'kind': 'section_para', 'section': number + 1,
                                                          'index': index}, text))
    return units, None


def _keep(paragraph: list, piece: str, most: int, budget) -> None:
    """Add a text piece (or a tab or line break) to an open HWPX paragraph, charging it as text."""
    budget.text(len(piece))
    paragraph[2] += len(piece)
    if paragraph[2] > most:
        raise ParseFailure('too_large', 'unit_chars')
    if paragraph[1] is None:
        paragraph[1] = _container.Pieces()
    paragraph[1].append(piece)


def _section_units(chunks, section: int, units: list, limits: Limits, budget) -> None:
    """Units of one HWPX section, from a stream of events.

    Each open element has a role towards its closest ``hp:p``: ``walk`` (its children count),
    ``text`` (an ``hp:t``: the text directly in it, and for each child the inline tab or line break)
    or ``skip`` (nothing inside counts, except nested paragraphs, which are their own).
    """
    stream = _container.XmlStream(chunks, limits, budget)
    most = limits.max_unit_chars
    found, roles, index = [], [], 0
    for kind, tag, value in stream:
        if kind is TEXT:
            if roles and roles[-1][0] == 'text':
                _keep(roles[-1][1], value, most, budget)
            continue
        if kind is START:
            parent = roles[-1] if roles else None
            if tag == _P:
                index += 1
                role = ('walk', [index, None, 0])  # [index, Pieces or None before the first piece, chars]
            elif parent is None or parent[0] == 'skip':
                role = ('skip', None)
            elif parent[0] == 'text':
                if tag in _INLINE:
                    _keep(parent[1], _INLINE[tag], most, budget)
                role = ('skip', None)
            elif tag == _T:  # the parent walks for a paragraph
                role = ('text', parent[1])
            elif tag in _INLINE:
                _keep(parent[1], _INLINE[tag], most, budget)
                role = ('skip', None)
            else:
                role = ('walk', parent[1])
            roles.append(role)
            continue
        role = roles.pop()
        if tag == _P:
            number, pieces, _ = role[1]
            text = pieces.text().strip() if pieces else ''
            if text:
                budget.unit()
                found.append((number, text))
    found.sort()  # a nested paragraph ends before the one around it
    for number, text in found:
        units.append(Unit(0, 'section_para', {'kind': 'section_para', 'section': section, 'index': number}, text))


def parse_hwpx(data: bytes, limits: Limits, budget: _container.Budget) -> tuple[list[Unit], str | None]:
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
        _section_units(_container.part_chunks(archive, name, limits), number + 1, units, limits, budget)
    return units, None

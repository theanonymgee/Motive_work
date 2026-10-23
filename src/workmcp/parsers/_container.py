"""Safe container helpers shared by the zip/XML/OLE parsers (design M1 §5.3, §5.4; parser resource design §2, §3.2).

Everything works on bytes in memory: no path is opened, nothing is extracted to disk and no
nested archive is read.

- Resources: ``Budget`` is one file's budget of unit text, units, events (a machine-independent
  measure of work), held memory and wall-clock time. The dispatcher makes it and hands it to the
  parser; every place that keeps document content charges it when it makes it.
- zip: entry count, declared total size and duplicate member names are checked when opening.
  ``part_chunks`` reads one member in 64 KiB pieces and stops past ``zip_max_part_bytes``, so a
  member whose header lies about its size still cannot exceed the limit, and a part is never held
  whole. ``zipfile`` checks the CRC when the read reaches the end.
- XML (``XmlStream``): expat called directly with handlers that build no element objects; the
  caller gets ``start``/``text``/``end`` events and the list of open tags. Memory is one fed piece,
  the events it produced and what the caller keeps, whatever the part's size or shape. Structural
  limits (all ``too_large``): nesting depth (``xml_depth``), attributes and namespace declarations
  of one element (``xml_attributes``), bytes of one piece of markup such as a start tag, a comment
  or a processing instruction (``xml_markup``), distinct element and attribute names in a part
  (``xml_names``, expat and the name table keep every name until the part ends) and one text node
  (``unit_chars``). A part containing ``<!DOCTYPE`` or ``<!ENTITY`` (UTF-8 or UTF-16 bytes) and a
  document type declaration in any encoding are ``failed/xml_dtd``; Office/HWPX parts never carry a
  DTD. Unknown encodings and malformed XML are ``xml_invalid``.
- OOXML root: ``check_root`` tells Strict Open XML (``unsupported/ooxml_strict``) and other
  foreign roots (``unsupported/bad_signature``) from the transitional parts the parsers read.
- OLE: stream names only (to tell encrypted OOXML from legacy Office files, and to find HWP
  streams).
"""
from __future__ import annotations

import io
import time
import zipfile
from collections.abc import Iterable, Iterator
from xml.parsers import expat

import olefile

from . import OLE_SIGNATURE, Limits, ParseFailure

START, END, TEXT = 'start', 'end', 'text'
_DTD_MARKERS = tuple(marker.encode(codec) for marker in ('<!DOCTYPE', '<!ENTITY')
                     for codec in ('utf-8', 'utf-16-le', 'utf-16-be'))
_MARKER_TAIL = max(len(marker) for marker in _DTD_MARKERS) - 1
_FEED_BYTES = 1 << 16  # one zip read and one expat feed; its events (a few MiB at most) are handed out before the next
_STRICT_NAMESPACE = '{http://purl.oclc.org/ooxml/'
_NO_ATTRIBUTES: dict = {}  # shared by every element without attributes: read it, never change it
_TICK = 4096         # events between two reads of the clock
_OBJECT_BYTES = 256  # held-memory model: one kept object (a str, tuple or dict entry and its slot) ...
_CHAR_BYTES = 4      # ... plus four bytes per character (the widest str storage)
_JOIN_PIECES = 4096  # text pieces of one unit joined into one string at a time


class Budget:
    """One file's resource budget (parser resource design §2). The dispatcher makes one per parse and
    every parser charges it at the moment it makes something; each meter has its own ``too_large`` code:

    - ``text(n)``: characters of unit text kept (``limit_chars``, ``max_text_chars``);
    - ``unit(n)``: units made or waiting to be made (``limit_units``, ``max_units``);
    - ``event(n)``: XML events, HWP records and control characters, text lines and rows, PDF operators
      and text pieces (``limit_events``, ``max_events``): work counted the same on every machine. The
      clock is read every 4,096 events;
    - ``hold(objects, chars)``: approximate bytes of tables kept until the parse ends (shared strings,
      relationship targets, names) at 256 bytes per object and 4 per character (``limit_memory``,
      ``max_held_bytes``). Only ``XmlStream`` gives some back (``release``: its per-part name table);
    - ``clock()``: the wall clock (``parse_time``, ``parse_seconds``).

    Charging too much only refuses earlier; it never lets memory grow.
    """
    __slots__ = ('limits', 'deadline', 'text_left', 'units_left', 'events_left', 'held_left', '_tick')

    def __init__(self, limits: Limits):
        self.limits = limits
        self.deadline = time.monotonic() + limits.parse_seconds
        self.text_left, self.units_left = limits.max_text_chars, limits.max_units
        self.events_left, self.held_left = limits.max_events, limits.max_held_bytes
        self._tick = _TICK

    def clock(self) -> None:
        if time.monotonic() > self.deadline:
            raise ParseFailure('too_large', 'parse_time')

    def text(self, chars: int) -> None:
        self.text_left -= chars
        if self.text_left < 0:
            raise ParseFailure('too_large', 'limit_chars')

    def unit(self, count: int = 1) -> None:
        self.units_left -= count
        if self.units_left < 0:
            raise ParseFailure('too_large', 'limit_units')

    def event(self, count: int = 1) -> None:
        self.events_left -= count
        if self.events_left < 0:
            raise ParseFailure('too_large', 'limit_events')
        self._tick -= count
        if self._tick <= 0:
            self._tick = _TICK
            self.clock()

    def hold(self, objects: int = 1, chars: int = 0) -> None:
        self.held_left -= _OBJECT_BYTES * objects + _CHAR_BYTES * chars
        if self.held_left < 0:
            raise ParseFailure('too_large', 'limit_memory')

    def release(self, objects: int = 1, chars: int = 0) -> None:
        self.held_left += _OBJECT_BYTES * objects + _CHAR_BYTES * chars


class Pieces:
    """The text pieces of one unit being read (a paragraph, cell, shape or string). Every 4,096 pieces are
    joined into one string, so memory follows the characters kept and not the number of pieces: a piece is
    a str object of about 50 bytes, and a part of one-character pieces (text split by comments, empty
    paragraphs, breaks) would otherwise take more than ten times the characters the budget counts."""
    __slots__ = ('joined', 'tail')

    def __init__(self):
        self.joined, self.tail = [], []

    def append(self, piece: str) -> None:
        tail = self.tail
        tail.append(piece)
        if len(tail) == _JOIN_PIECES:
            self.joined.append(''.join(tail))
            tail.clear()

    def text(self) -> str:
        if not self.joined:
            return ''.join(self.tail)
        return ''.join(self.joined) + ''.join(self.tail)


def open_zip(data: bytes, limits: Limits) -> zipfile.ZipFile:
    """A zip archive over ``data`` within the entry-count and declared-size limits."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        infos = archive.infolist()
    except MemoryError:
        raise
    except Exception:  # any failure to read the directory means a damaged archive
        raise ParseFailure('failed', 'bad_zip') from None
    if len(infos) > limits.zip_max_entries:
        raise ParseFailure('too_large', 'zip_entries')
    if sum(info.file_size for info in infos) > limits.zip_max_total_bytes:
        raise ParseFailure('too_large', 'zip_total')
    if len({info.filename for info in infos}) != len(infos):
        raise ParseFailure('failed', 'bad_zip')  # two members with one name: which one is the document is unclear
    return archive


def part_chunks(archive: zipfile.ZipFile, name: str, limits: Limits, *,
                required: bool = True) -> Iterator[bytes] | None:
    """One member's bytes (exact name) as pieces of at most 64 KiB, read only when iterated.

    Missing: ``failed/missing_part``, or None when not required. Past ``zip_max_part_bytes``:
    ``too_large/zip_part`` (the excess is never handed out). Damaged data, a CRC mismatch or an
    encrypted member: ``failed/bad_zip``.
    """
    try:
        info = archive.getinfo(name)
    except KeyError:
        if required:
            raise ParseFailure('failed', 'missing_part') from None
        return None
    return _member_chunks(archive, info, limits.zip_max_part_bytes)


def _member_chunks(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> Iterator[bytes]:
    try:
        member = archive.open(info, 'r')
    except MemoryError:
        raise
    except Exception:  # encrypted or unsupported member
        raise ParseFailure('failed', 'bad_zip') from None
    total = 0
    with member:
        while True:
            try:
                chunk = member.read(_FEED_BYTES)  # reading up to the end makes zipfile check the CRC
            except MemoryError:
                raise
            except Exception:  # bad compression data, CRC mismatch
                raise ParseFailure('failed', 'bad_zip') from None
            if not chunk:
                return
            total += len(chunk)
            if total > limit:
                raise ParseFailure('too_large', 'zip_part')
            yield chunk


def reject_dtd(part: bytes) -> None:
    if any(marker in part for marker in _DTD_MARKERS):
        raise ParseFailure('failed', 'xml_dtd')


def _handlers(limits: Limits, budget: Budget):
    """An expat parser whose callbacks queue ``(kind, tag, value)`` events in the returned list.

    Names become ``{uri}local`` tags; every distinct name (prefix included, as expat stores it) and
    every element's attributes are counted. A namespace declaration counts as the name
    ``xmlns:prefix=uri``: expat keeps every URI until the part ends, so distinct URIs are distinct
    names. Each name is held in the budget; ``held`` (names, characters) is what the stream gives back
    when the part ends. Comments, processing instructions, the XML declaration and whatever else expat
    reports (white space around the root) make no event but set ``progress[0]``: expat no longer holds
    them. No element object is ever made. The callbacks are closures over locals because they run once
    per event.
    """
    events = []
    append = events.append
    tags = {}   # name as expat reports it ('uri}local}prefix') -> tag; its size is the name count
    ends = {}   # the same name -> its END event (one tuple per name, shared by every end)
    get, end_of = tags.get, ends.__getitem__
    depth = declared = 0  # open elements; namespace declarations of the element about to start
    held = [0, 0]         # names of this part held in the budget and their characters
    progress = [False]    # a callback that makes no event ran during the last Parse call
    max_names, max_attributes, max_depth = limits.xml_max_names, limits.xml_max_attributes, limits.xml_max_depth

    def name(raw):
        if len(tags) >= max_names:
            raise ParseFailure('too_large', 'xml_names')
        held[0] += 1
        held[1] += len(raw)
        budget.hold(1, len(raw))
        uri, separator, rest = raw.partition('}')
        tag = '{' + uri + '}' + rest.partition('}')[0] if separator else raw
        tags[raw], ends[raw] = tag, (END, tag, None)
        return tag

    def start(raw, attributes):
        nonlocal depth, declared
        depth += 1
        if depth > max_depth:
            raise ParseFailure('too_large', 'xml_depth')
        tag = get(raw) or name(raw)
        if attributes or declared:
            if len(attributes) + declared > max_attributes:
                raise ParseFailure('too_large', 'xml_attributes')
            declared = 0
            append((START, tag, {get(key) or name(key): value for key, value in attributes.items()}))
        else:
            append((START, tag, _NO_ATTRIBUTES))

    def end(raw):
        nonlocal depth
        depth -= 1
        append(end_of(raw))

    def text(data):
        append((TEXT, None, data))

    def namespace(prefix, uri):
        nonlocal declared
        declared += 1
        key = 'xmlns:' + (prefix or '') + '=' + (uri or '')
        if key not in tags:
            name(key)

    def other(*args):
        progress[0] = True

    def doctype(*args):
        raise ParseFailure('failed', 'xml_dtd')

    parser = expat.ParserCreate(namespace_separator='}')
    parser.namespace_prefixes = True  # 'uri}local}prefix': distinct raw names stay distinct when counted
    parser.buffer_text = True
    parser.buffer_size = _FEED_BYTES  # one text event per run per fed piece
    if hasattr(parser, 'SetReparseDeferralEnabled'):
        # expat >= 2.6 may wait for more input before parsing a token that has already ended, so a fed
        # piece could end a start tag and still give no event. Parsing at once keeps the count of quiet
        # pieces exact; the markup limit bounds the cost of tokenizing a long tag again.
        parser.SetReparseDeferralEnabled(False)
    parser.StartElementHandler, parser.EndElementHandler, parser.CharacterDataHandler = start, end, text
    parser.StartNamespaceDeclHandler, parser.StartDoctypeDeclHandler = namespace, doctype
    parser.CommentHandler = parser.ProcessingInstructionHandler = parser.XmlDeclHandler = other
    parser.DefaultHandlerExpand = other
    return parser, events, progress, held


class XmlStream:
    """Events of one XML part, in document order: ``(START, tag, attributes)``, ``(TEXT, None, text)``,
    ``(END, tag, None)``.

    Tags and attribute names are ``{uri}local`` (or ``local`` without a namespace), as in
    ElementTree; the attributes dict is the caller's to read, not to change. ``path`` lists the open
    tags, outermost first; it is the same list throughout (read it, do not keep or change it). At
    ``START`` and ``END`` it does not include the element itself; at ``TEXT`` its last tag is the
    element the text is directly in. A text node may come as several ``TEXT`` events (one per fed
    piece). Comments and processing instructions give no event.

    ``source`` is the part's bytes or an iterable of pieces (``part_chunks``). Damage found late in
    a part fails the whole parse, so a caller keeps its units only when the stream ends normally.

    Resources (``budget``, a new one when none is given): the events of every fed piece are charged
    (``limit_events``) and the clock is read after every piece; a text node over ``max_unit_chars`` is
    ``too_large/unit_chars``; the part's names are held until the part ends. Markup that expat must hold
    whole (a start tag, comment or processing instruction) is measured by the fed pieces in a row that
    gave no event at all: more than ``xml_max_markup_bytes`` of them is ``too_large/xml_markup``. With
    64 KiB pieces a token of at most 1 MiB always passes, one of 1.125 MiB or more never does, wherever
    it is in the part.
    """

    def __init__(self, source: bytes | Iterable[bytes], limits: Limits, budget: Budget | None = None):
        self.source, self.limits = source, limits
        self.budget = budget if budget is not None else Budget(limits)
        self.path: list[str] = []

    def _pieces(self) -> Iterator[bytes]:
        source = self.source
        if isinstance(source, (bytes, bytearray, memoryview)):
            source = (source,)
        for chunk in source:
            for start in range(0, len(chunk), _FEED_BYTES):
                yield chunk[start:start + _FEED_BYTES]

    def __iter__(self) -> Iterator[tuple]:
        limits, budget, path = self.limits, self.budget, self.path
        max_text, max_markup = limits.max_unit_chars, limits.xml_max_markup_bytes
        parser, events, progress, held = _handlers(limits, budget)
        try:
            pieces = self._pieces()
            chunk, run, quiet = next(pieces, None), 0, 0
            while True:
                final = chunk is None
                if not final:
                    following = next(pieces, None)  # one piece ahead: a DTD marker across the boundary is found first
                    reject_dtd(chunk)
                    if following is not None:
                        reject_dtd(chunk[-_MARKER_TAIL:] + following[:_MARKER_TAIL])
                try:
                    parser.Parse(b'' if final else chunk, final)
                except expat.ExpatError:
                    raise ParseFailure('failed', 'xml_invalid') from None
                if events or progress[0]:
                    quiet = progress[0] = 0
                elif not final:
                    quiet += len(chunk)  # all held by expat for markup that has not ended yet
                    if quiet > max_markup:
                        raise ParseFailure('too_large', 'xml_markup')
                budget.event(len(events))
                for event in events:
                    kind = event[0]
                    if kind is START:
                        run = 0
                        yield event
                        path.append(event[1])
                    elif kind is END:
                        run = 0
                        path.pop()
                        yield event
                    else:
                        run += len(event[2])
                        if run > max_text:
                            raise ParseFailure('too_large', 'unit_chars')
                        yield event
                events.clear()
                budget.clock()
                if final:
                    return
                chunk = following
        finally:
            budget.release(*held)


def check_root(tag: str, expected: str) -> None:
    """The root of a main OOXML part must be the transitional ``expected`` tag."""
    if tag != expected:
        if tag.startswith(_STRICT_NAMESPACE):
            raise ParseFailure('unsupported', 'ooxml_strict')
        raise ParseFailure('unsupported', 'bad_signature')


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], 'little')


def ole_entries(data: bytes, limit: int) -> int | None:
    """Directory entries of a compound file, counted from its header and FAT without opening it (olefile
    makes an object of about 1.5 KB per entry), or None when the header cannot be followed (olefile then
    decides). Counting stops past ``limit``."""
    if len(data) < 512 or not data.startswith(OLE_SIGNATURE):
        return None
    shift = int.from_bytes(data[0x1E:0x20], 'little')
    if shift not in (9, 12):
        return None
    size = 1 << shift
    per_sector, entries_per_sector, sectors = size // 4, size // 128, len(data) // size

    def sector(number: int) -> bytes | None:
        start = (number + 1) * size
        return data[start:start + size] if number < sectors and start + size <= len(data) else None
    difat = [_u32(data, 0x4C + 4 * n) for n in range(109)]
    following, left = _u32(data, 0x44), min(_u32(data, 0x48), sectors)
    while following < 0xFFFFFFFA and left > 0:  # DIFAT sectors: the FAT sectors beyond the first 109
        block = sector(following)
        if block is None:
            return None
        difat.extend(_u32(block, 4 * n) for n in range(per_sector - 1))
        following, left = _u32(block, size - 4), left - 1
    current, count = _u32(data, 0x30), 0
    while current < 0xFFFFFFFA:  # the directory's sector chain
        count += entries_per_sector
        if count > limit:
            return count
        index, slot = divmod(current, per_sector)
        block = sector(difat[index]) if index < len(difat) else None
        if block is None:
            return None
        current = _u32(block, 4 * slot)
    return count


def ole_streams(data: bytes) -> set[str] | None:
    """Stream paths (``'BodyText/Section0'``) of an OLE compound file, or None if it is not a readable one."""
    if not data.startswith(OLE_SIGNATURE):
        return None
    try:
        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            return {'/'.join(path) for path in ole.listdir(streams=True, storages=False)}
    except MemoryError:
        raise
    except Exception:  # olefile raises several types on damaged files
        return None

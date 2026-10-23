"""Safe container helpers shared by the zip/XML/OLE parsers (design M1 §5.3, §5.4).

Everything works on bytes in memory: no path is opened, nothing is extracted to disk and no
nested archive is read.

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
  (``xml_text``). A part containing ``<!DOCTYPE`` or ``<!ENTITY`` (UTF-8 or UTF-16 bytes) and a
  document type declaration in any encoding are ``failed/xml_dtd``; Office/HWPX parts never carry a
  DTD. Unknown encodings and malformed XML are ``xml_invalid``.
- Time: ``Clock`` holds the deadline of one parse (``Limits.parse_seconds``); the XML stream checks
  it after every fed piece and the HWP and PDF parsers in their record and page loops
  (``too_large/parse_time``).
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


class Clock:
    """Deadline of one parse: ``check()`` raises ``too_large/parse_time`` once ``parse_seconds`` have passed."""
    __slots__ = ('deadline',)

    def __init__(self, limits: Limits):
        self.deadline = time.monotonic() + limits.parse_seconds

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            raise ParseFailure('too_large', 'parse_time')


class TextBudget:
    """Characters a parser keeps for its units across a whole file; past ``max_text_chars`` it is
    ``too_large/limit_chars`` at once instead of after the text has piled up."""
    __slots__ = ('left',)

    def __init__(self, limits: Limits):
        self.left = limits.max_text_chars

    def take(self, count: int) -> None:
        self.left -= count
        if self.left < 0:
            raise ParseFailure('too_large', 'limit_chars')


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


def _handlers(limits: Limits):
    """An expat parser whose callbacks queue ``(kind, tag, value)`` events in the returned list.

    Names become ``{uri}local`` tags; every distinct name (prefix included, as expat stores it) and
    every element's attributes are counted. No element object is ever made. The callbacks are
    closures over locals because they run once per event.
    """
    events = []
    append = events.append
    tags = {}   # name as expat reports it ('uri}local}prefix') -> tag; its size is the name count
    ends = {}   # the same name -> its END event (one tuple per name, shared by every end)
    get, end_of = tags.get, ends.__getitem__
    depth = declared = 0  # open elements; namespace declarations of the element about to start
    max_names, max_attributes, max_depth = limits.xml_max_names, limits.xml_max_attributes, limits.xml_max_depth

    def name(raw):
        if len(tags) >= max_names:
            raise ParseFailure('too_large', 'xml_names')
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
        key = 'xmlns:' + (prefix or '')
        if key not in tags:
            name(key)

    def doctype(*args):
        raise ParseFailure('failed', 'xml_dtd')

    parser = expat.ParserCreate(namespace_separator='}')
    parser.namespace_prefixes = True  # 'uri}local}prefix': distinct raw names stay distinct when counted
    parser.buffer_text = True
    parser.buffer_size = _FEED_BYTES  # one text event per run per fed piece
    parser.StartElementHandler, parser.EndElementHandler, parser.CharacterDataHandler = start, end, text
    parser.StartNamespaceDeclHandler, parser.StartDoctypeDeclHandler = namespace, doctype
    return parser, events


class XmlStream:
    """Events of one XML part, in document order: ``(START, tag, attributes)``, ``(TEXT, None, text)``,
    ``(END, tag, None)``.

    Tags and attribute names are ``{uri}local`` (or ``local`` without a namespace), as in
    ElementTree; the attributes dict is the caller's to read, not to change. ``path`` lists the open tags, outermost first; it is the same list throughout (read
    it, do not keep or change it). At ``START`` and ``END`` it does not include the element itself;
    at ``TEXT`` its last tag is the element the text is directly in. A text node may come as several
    ``TEXT`` events (one per fed piece). Comments and processing instructions give no event.

    ``source`` is the part's bytes or an iterable of pieces (``part_chunks``). Damage found late in
    a part fails the whole parse, so a caller keeps its units only when the stream ends normally.
    """

    def __init__(self, source: bytes | Iterable[bytes], limits: Limits, clock: Clock | None = None):
        self.source, self.limits = source, limits
        self.clock = clock or Clock(limits)
        self.path: list[str] = []

    def _pieces(self) -> Iterator[bytes]:
        source = self.source
        if isinstance(source, (bytes, bytearray, memoryview)):
            source = (source,)
        for chunk in source:
            for start in range(0, len(chunk), _FEED_BYTES):
                yield chunk[start:start + _FEED_BYTES]

    def __iter__(self) -> Iterator[tuple]:
        limits, clock, path = self.limits, self.clock, self.path
        max_text, max_markup = limits.xml_max_text_chars, limits.xml_max_markup_bytes
        parser, events = _handlers(limits)
        pieces = self._pieces()
        chunk, run, fed = next(pieces, None), 0, 0
        while True:
            final = chunk is None
            if not final:
                following = next(pieces, None)  # one piece ahead: a DTD marker across the boundary is found first
                reject_dtd(chunk)
                if following is not None:
                    reject_dtd(chunk[-_MARKER_TAIL:] + following[:_MARKER_TAIL])
                fed += len(chunk)
            try:
                parser.Parse(b'' if final else chunk, final)
            except expat.ExpatError:
                raise ParseFailure('failed', 'xml_invalid') from None
            if fed - parser.CurrentByteIndex > max_markup:  # bytes expat holds for markup that has not ended yet
                raise ParseFailure('too_large', 'xml_markup')
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
                        raise ParseFailure('too_large', 'xml_text')
                    yield event
            events.clear()
            clock.check()
            if final:
                return
            chunk = following


def check_root(tag: str, expected: str) -> None:
    """The root of a main OOXML part must be the transitional ``expected`` tag."""
    if tag != expected:
        if tag.startswith(_STRICT_NAMESPACE):
            raise ParseFailure('unsupported', 'ooxml_strict')
        raise ParseFailure('unsupported', 'bad_signature')


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

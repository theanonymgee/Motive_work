"""Safe container helpers shared by the zip/XML/OLE parsers (design M1 §5.3, §5.4).

Everything works on bytes in memory: no path is opened, nothing is extracted to disk and no
nested archive is read.

- zip: entry count, declared total size and duplicate member names are checked when opening;
  one member is read with a bounded read (``limit + 1`` bytes at most), so a member whose header
  lies about its size still cannot exceed the limit.
- XML: a part containing ``<!DOCTYPE`` or ``<!ENTITY`` (in UTF-8 or UTF-16) is refused before
  parsing, and a first pass that builds nothing refuses a document type declaration in any
  encoding expat reads. Office/HWPX parts never carry a DTD. Unknown encodings and malformed XML
  are ``xml_invalid``.
- XML memory: a tree costs about 20 times the part's size, so ``parse_xml`` (small parts only)
  refuses a part over ``Limits.xml_max_tree_bytes`` (``too_large/xml_tree``). Large parts go
  through ``iter_xml``, which detaches every finished element from its parent: memory holds the
  part's bytes, the open element path and what the caller keeps.
- OOXML root: ``check_root`` tells Strict Open XML (``unsupported/ooxml_strict``) and other
  foreign roots (``unsupported/bad_signature``) from the transitional parts the parsers read.
- OLE: stream names only (to tell encrypted OOXML from legacy Office files, and to find HWP
  streams).
"""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator

import olefile

from . import OLE_SIGNATURE, Limits, ParseFailure

_DTD_MARKERS = tuple(marker.encode(codec) for marker in ('<!DOCTYPE', '<!ENTITY')
                     for codec in ('utf-8', 'utf-16-le', 'utf-16-be'))
_FEED_BYTES = 1 << 14  # one chunk of events (about 40 bytes of objects per XML byte) at a time
_READ_BYTES = 1 << 18
_STRICT_NAMESPACE = '{http://purl.oclc.org/ooxml/'


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


def read_part(archive: zipfile.ZipFile, name: str, limits: Limits, *, required: bool = True) -> bytearray | None:
    """One member's bytes (exact name). Missing: ``failed/missing_part``, or None when not required.

    The bytes go into one buffer of the declared size (capped at the limit + 1) in 256 KiB reads, so
    reading costs the member's size once. ``zipfile`` never returns more than the declared size and
    checks the CRC when it reaches it.
    """
    try:
        info = archive.getinfo(name)
    except KeyError:
        if required:
            raise ParseFailure('failed', 'missing_part') from None
        return None
    size = min(info.file_size, limits.zip_max_part_bytes + 1)
    data = bytearray(size)
    filled = 0
    try:
        with archive.open(info, 'r') as member, memoryview(data) as view:
            while filled < size:
                chunk = member.read(min(_READ_BYTES, size - filled))
                if not chunk:
                    break
                view[filled:filled + len(chunk)] = chunk
                filled += len(chunk)
            if filled <= limits.zip_max_part_bytes:
                member.read(1)  # reach the end so that zipfile checks the CRC (also of a member declared empty)
    except MemoryError:
        raise
    except Exception:  # bad compression data, CRC mismatch, encrypted or unsupported member
        raise ParseFailure('failed', 'bad_zip') from None
    del data[filled:]
    if len(data) > limits.zip_max_part_bytes:
        raise ParseFailure('too_large', 'zip_part')
    return data


class _DocumentType(Exception):
    pass


class _DoctypeGuard:
    """Parser target that builds nothing and refuses a document type declaration."""

    def doctype(self, name, pubid, system):
        raise _DocumentType()


def reject_dtd(part: bytes) -> None:
    if any(marker in part for marker in _DTD_MARKERS):
        raise ParseFailure('failed', 'xml_dtd')


def _feed(parser, part: bytes) -> None:
    """Feed the whole part in chunks (expat copies each fed buffer) and close the parser."""
    try:
        for start in range(0, len(part), _FEED_BYTES):
            parser.feed(part[start:start + _FEED_BYTES])
        return parser.close()
    except _DocumentType:
        raise ParseFailure('failed', 'xml_dtd') from None
    except ET.ParseError:
        raise ParseFailure('failed', 'xml_invalid') from None


def _guard(part: bytes) -> None:
    """Refuse a DTD (byte check, then a pass that builds nothing) and malformed XML before any tree is made."""
    reject_dtd(part)
    _feed(ET.XMLParser(target=_DoctypeGuard()), part)


def parse_xml(part: bytes, limits: Limits) -> ET.Element:
    """Root element of one small XML part (namespaces as ``{uri}local`` tags)."""
    if len(part) > limits.xml_max_tree_bytes:
        raise ParseFailure('too_large', 'xml_tree')
    _guard(part)
    return _feed(ET.XMLParser(), part)


def iter_xml(part: bytes) -> Iterator[tuple[str, ET.Element, list[ET.Element]]]:
    """``(event, element, path)`` for every ``'start'`` and ``'end'`` of an element, in document order.

    ``path`` is the list of open ancestors (parent last, the element itself not included; the
    same list object throughout, read it but do not keep or change it). Attributes are there at
    ``'start'`` and text at ``'end'``; a child's ``tail`` is set by the time the next event
    arrives. After each fed chunk every finished element is detached from its parent, so a caller
    must not rely on an element's children: it keeps what it needs from each element when that
    element ends. The whole part is checked first (DTD, well-formedness).
    """
    _guard(part)
    pull = ET.XMLPullParser(events=('start', 'end'))
    path = []
    for start in range(0, len(part) + _FEED_BYTES, _FEED_BYTES):
        last = start >= len(part)
        try:
            if last:
                pull.close()
            else:
                pull.feed(part[start:start + _FEED_BYTES])
            events = list(pull.read_events())
        except ET.ParseError:
            raise ParseFailure('failed', 'xml_invalid') from None
        for event, element in events:
            if event == 'start':
                yield event, element, path
                path.append(element)
            else:
                path.pop()
                yield event, element, path
        del events
        # Detach every finished element: an open element keeps only its open child, which is its last one.
        for depth, element in enumerate(path):
            if len(element):
                if depth + 1 < len(path) and element[-1] is path[depth + 1]:
                    del element[:-1]
                else:
                    del element[:]
        if last:
            return


def check_root(element: ET.Element, expected: str) -> None:
    """The root of a main OOXML part must be the transitional ``expected`` tag."""
    if element.tag != expected:
        if element.tag.startswith(_STRICT_NAMESPACE):
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

"""Safe container helpers shared by the zip/XML/OLE parsers (design M1 §5.3, §5.4).

Everything works on bytes in memory: no path is opened, nothing is extracted to disk and no
nested archive is read.

- zip: entry count and declared total size are checked when opening; one member is read with a
  bounded read (``limit + 1`` bytes at most), so a member whose header lies about its size
  still cannot exceed the limit.
- XML: a part containing ``<!DOCTYPE`` or ``<!ENTITY`` (in UTF-8 or UTF-16) is refused before
  parsing, and the tree builder refuses a document type declaration in any encoding expat
  reads. Office/HWPX parts never carry a DTD. Unknown encodings and malformed XML are
  ``xml_invalid``.
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
_FEED_BYTES = 1 << 16


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
    return archive


def read_part(archive: zipfile.ZipFile, name: str, limits: Limits, *, required: bool = True) -> bytes | None:
    """One member's bytes (exact name). Missing: ``failed/missing_part``, or None when not required."""
    try:
        info = archive.getinfo(name)
    except KeyError:
        if required:
            raise ParseFailure('failed', 'missing_part') from None
        return None
    try:
        with archive.open(info, 'r') as member:
            data = member.read(limits.zip_max_part_bytes + 1)
    except MemoryError:
        raise
    except Exception:  # bad compression data, CRC mismatch, encrypted or unsupported member
        raise ParseFailure('failed', 'bad_zip') from None
    if len(data) > limits.zip_max_part_bytes:
        raise ParseFailure('too_large', 'zip_part')
    return data


class _DocumentType(Exception):
    pass


class _Builder(ET.TreeBuilder):
    """Tree builder that refuses a DTD and records each element when it ends."""

    def __init__(self):
        super().__init__()
        self.ended = []

    def doctype(self, name, pubid, system):
        raise _DocumentType()

    def end(self, tag):
        element = super().end(tag)
        self.ended.append(element)
        return element


def reject_dtd(part: bytes) -> None:
    if any(marker in part for marker in _DTD_MARKERS):
        raise ParseFailure('failed', 'xml_dtd')


def _feed(parser, chunk: bytes) -> None:
    try:
        parser.feed(chunk)
    except _DocumentType:
        raise ParseFailure('failed', 'xml_dtd') from None
    except ET.ParseError:
        raise ParseFailure('failed', 'xml_invalid') from None


def _close(parser):
    try:
        return parser.close()
    except _DocumentType:
        raise ParseFailure('failed', 'xml_dtd') from None
    except ET.ParseError:
        raise ParseFailure('failed', 'xml_invalid') from None


def parse_xml(part: bytes) -> ET.Element:
    """Root element of one XML part (namespaces as ``{uri}local`` tags)."""
    reject_dtd(part)
    builder = _Builder()
    parser = ET.XMLParser(target=builder)
    _feed(parser, part)
    return _close(parser)


def iter_xml(part: bytes) -> Iterator[ET.Element]:
    """Elements of one XML part in end order (like ``iterparse``); call ``clear()`` on handled ones.

    The whole part is checked for a DTD before the first element is produced.
    """
    reject_dtd(part)
    builder = _Builder()
    parser = ET.XMLParser(target=builder)
    for start in range(0, len(part), _FEED_BYTES):
        _feed(parser, part[start:start + _FEED_BYTES])
        yield from builder.ended
        builder.ended.clear()
    _close(parser)
    yield from builder.ended
    builder.ended.clear()


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

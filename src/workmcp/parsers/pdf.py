"""PDF pages through pypdf (design M1 §5.8; parser resource design §4).

One unit per page that has extractable text (``{"kind": "page", "page": p}``, ``p`` counts every
page, empty ones included). An encrypted PDF is only reported, never decrypted. Nothing a library
logs or raises is kept: pypdf's logger is silenced because its messages can quote document content,
and the dispatcher drops exception messages.

Resources, charged to the file's budget:

- The page tree is walked before pypdf lists the pages: every ``/Kids`` reference is one event and
  every page reference (repeated ones too) counts, so more than ``pdf_max_pages`` is
  ``too_large/pdf_count`` before pypdf makes one object per reference; a cycle is
  ``failed/pdf_read_error``. The raw size of every tree object is read from the cross-reference table
  before pypdf parses it: more than 16 MiB for the catalog and the tree is ``pdf_count`` too (pypdf
  makes an object of about 100 bytes or more for each ``1 0 R`` of 6 bytes it parses).
- One page's text is read with visitors: every content operator is an event (and the clock is read
  every 4,096), and the text pypdf hands out is counted as it comes. Past ``max_unit_chars`` (or the
  text left in the budget) the page stops at once with ``unit_chars`` (``limit_chars``), also from
  inside nested form XObjects, whose errors pypdf swallows.
- A page whose ``/Resources`` lookup would follow a ``/Parent`` cycle (pypdf loops forever there),
  or that uses such a form XObject, is read as a page without text.
- After each page the decoded copies of its content streams and form XObjects are dropped, so the
  memory pypdf caches does not grow with the number of pages.
- A stream that pypdf refuses to decode (over its own 75 MB output limit) is ``too_large/pdf_stream``, and
  so is decoded syntax over a token budget, measured before pypdf parses it: all object streams together
  (2 M tokens) and one page's content with its forms (3 M); pypdf makes an object of 35 to 120 bytes a
  token.

Opening the file and the parts of one page's extraction where pypdf calls no visitor (fonts, image
data) cannot be interrupted; their memory is bounded by pypdf's stream limit and the file size.
"""
from __future__ import annotations

import importlib.metadata
import io
import logging
import re

from pypdf import PdfReader
from pypdf.errors import LimitReachedError, PdfReadError

from . import Limits, ParseFailure, Unit, _container

NAME = 'pdf'
VERSION = f'pdf-4/pypdf-{importlib.metadata.version("pypdf")}'

_logger = logging.getLogger('pypdf')
_logger.setLevel(logging.CRITICAL)  # pypdf warnings may quote document content
_logger.propagate = False           # and no root handler (log file, stderr) may receive them
_logger.addHandler(logging.NullHandler())

_PARENT_STEPS = 64  # /Parent links followed when looking for inherited /Resources
_TREE_BYTES = 16 * 2**20  # raw bytes of the catalog and page tree objects parsed before the pages are read
# pypdf turns every token it parses into an object, about 35 to 120 bytes with its list slot (measured:
# 72 MB of decoded content stream or object stream, a few hundred KiB compressed, took +1.3 to over 4 GiB).
# Decoded data is measured in tokens before pypdf parses it: all object streams together (pypdf parses a
# whole object stream when one object of it is needed and keeps it), and one page's content with every form
# XObject it can draw. Over either is too_large/pdf_stream; at 120 bytes a token they stay under 600 MB.
_OBJECT_STREAM_TOKENS = 2_000_000
_PAGE_TOKENS = 3_000_000
_TOKEN = re.compile(rb'[\x00\t\n\x0c\r ]+|[/\[(<]')  # a run of white space, or a delimiter that starts one


class _Stop(Exception):
    """Carries a ParseFailure out of pypdf, which swallows any exception raised inside a form XObject."""

    def __init__(self, failure: ParseFailure):
        super().__init__()
        self.failure = failure


def _clean(text: str) -> str:
    # A lone surrogate from a broken font map cannot be stored; make it a replacement character.
    return text.encode('utf-8', errors='replace').decode('utf-8').strip()


def _resolved(value):
    return value.get_object() if value is not None and hasattr(value, 'get_object') else value


def _get(node, key):
    return _resolved(node.get(key)) if isinstance(node, dict) else None


def _node_type(node: dict) -> str:
    """What pypdf's page tree walk takes a node for: its ``/Type``, else a page when it has no ``/Kids``."""
    if '/Type' in node:
        return _get(node, '/Type')
    return '/Page' if '/Kids' not in node else '/Pages'


def _key(raw, node) -> tuple:
    return (raw.idnum, raw.generation) if hasattr(raw, 'idnum') else ('direct', id(node))


class _Sizes:
    """Raw size of an object from the cross-reference table: from its offset to the next object's (or the
    end of the file). pypdf makes an object of 100 bytes or more for every value it parses (a ``1 0 R``
    of 6 bytes), so the size of what it is about to parse is checked before it parses it. An object in an
    object stream has no offset of its own and counts 0 (pypdf parses a whole object stream, at most
    75 MB decoded, at once)."""

    def __init__(self, reader: PdfReader, size: int):
        self.xref, self.size = reader.xref, size
        self.offsets = sorted(offset for table in reader.xref.values() for offset in table.values()
                              if isinstance(offset, int))

    def of(self, raw) -> int:
        start = self.xref.get(getattr(raw, 'generation', None), {}).get(getattr(raw, 'idnum', None))
        if not isinstance(start, int):
            return 0
        low, high = 0, len(self.offsets)
        while low < high:  # the first offset after start
            middle = (low + high) // 2
            if self.offsets[middle] <= start:
                low = middle + 1
            else:
                high = middle
        return (self.offsets[low] if low < len(self.offsets) else self.size) - start


def _count_pages(reader: PdfReader, limits: Limits, budget, size: int) -> None:
    """Walk the page tree the way pypdf will flatten it, counting every page reference. The tree's
    objects are measured before they are parsed: more than 16 MiB of them is ``pdf_count`` too."""
    sizes, left = _Sizes(reader, size), [_TREE_BYTES]

    def resolve(raw):
        left[0] -= sizes.of(raw)
        if left[0] < 0:
            raise ParseFailure('too_large', 'pdf_count')
        return _resolved(raw)
    resolve(reader.trailer.raw_get('/Root') if '/Root' in reader.trailer else None)
    raw = reader.root_object.get('/Pages')
    root = resolve(raw)
    if not isinstance(root, dict):
        raise ParseFailure('failed', 'pdf_read_error')
    if _node_type(root) != '/Pages':
        return
    count, open_nodes, stack, seen = 0, set(), [], set()

    def enter(raw, node):
        key = _key(raw, node)
        if key in open_nodes:
            raise ParseFailure('failed', 'pdf_read_error')  # a cycle in the page tree
        open_nodes.add(key)
        kids = _get(node, '/Kids')
        stack.append((key, iter(kids if isinstance(kids, list) else ())))
    enter(raw, root)
    while stack:
        key, kids = stack[-1]
        kid = next(kids, None)
        if kid is None:
            stack.pop()
            open_nodes.discard(key)
            continue
        budget.event()
        if _key(kid, kid) not in seen:  # a repeated reference is parsed once
            seen.add(_key(kid, kid))
            node = resolve(kid)
        else:
            node = _resolved(kid)
        if not node or not isinstance(node, dict):
            continue  # pypdf skips an empty kid and fails on another kind
        kind = _node_type(node)
        if kind == '/Pages':
            enter(kid, node)
        elif kind == '/Page':
            count += 1
            if count > limits.pdf_max_pages:
                raise ParseFailure('too_large', 'pdf_count')


def _resources(node, budget) -> tuple[bool, object]:
    """``(found, resources)`` as pypdf looks them up: the node's own, else up the ``/Parent`` links.
    ``found`` is False when that lookup would not end (a cycle, or more than 64 links)."""
    for _ in range(_PARENT_STEPS):
        budget.event()
        if not isinstance(node, dict):
            return True, None  # pypdf's lookup fails and it reads no text
        if '/Resources' in node:
            return True, _get(node, '/Resources')
        if '/Parent' not in node:
            return True, None
        node = _get(node, '/Parent')
    return False, None


def _tokens(stream, most: int, budget) -> int:
    """Tokens of a stream's decoded data, counted up to ``most + 1`` (one match at a time, so the count
    takes no memory of its own) without keeping a decoded copy pypdf had not made (it decodes again when it
    reads the stream; a stream is at most 75 MB decoded)."""
    stream = _resolved(stream)
    if not hasattr(stream, 'get_data') or most < 0:
        return 0
    budget.clock()
    cached = getattr(stream, 'decoded_self', None)
    count = 0
    for count, _ in enumerate(_TOKEN.finditer(stream.get_data()), 1):
        if count > most:
            break
    if cached is None and getattr(stream, 'decoded_self', None) is not None:
        stream.decoded_self = None
    budget.event(count // 8 + 1)  # a counted token is about a tenth of the work of an event
    return count


def _check_object_streams(reader: PdfReader, budget) -> None:
    """Before any object is resolved: the tokens of every object stream together (``pdf_stream``)."""
    left = _OBJECT_STREAM_TOKENS
    for number in sorted({entry[0] for entry in reader.xref_objStm.values()}):
        left -= _tokens(reader.get_object(number), left, budget)
        if left < 0:
            raise ParseFailure('too_large', 'pdf_stream')


def _readable(page, budget) -> bool:
    """False when pypdf's ``/Resources`` lookup would loop on the page or on a form XObject it may draw.
    The page's content and every form it can draw are measured in tokens first (``pdf_stream``)."""
    found, resources = _resources(page, budget)
    if not found:
        return False
    contents = _get(page, '/Contents')
    left = _PAGE_TOKENS
    for stream in (contents if isinstance(contents, list) else [contents]):
        left -= _tokens(stream, left, budget)
    if left < 0:
        raise ParseFailure('too_large', 'pdf_stream')
    stack, seen = [resources], set()
    while stack:
        xobjects = _get(stack.pop(), '/XObject')
        if not isinstance(xobjects, dict):
            continue
        for name in list(xobjects):
            budget.event()
            raw = xobjects.get(name)
            form = _resolved(raw)
            key = _key(raw, form)
            if key in seen or not isinstance(form, dict) or _get(form, '/Subtype') != '/Form':
                continue
            seen.add(key)
            left -= _tokens(form, left, budget)
            if left < 0:
                raise ParseFailure('too_large', 'pdf_stream')
            found, inner = _resources(form, budget)
            if not found:
                return False
            stack.append(inner)
    return True


def _page_text(page, budget) -> str:
    """One page's text, stopped from the visitors once the page's text or the budget runs out."""
    most = min(budget.limits.max_unit_chars, budget.text_left)
    seen, stopped = 0, []  # characters handed out so far (nested forms report theirs again); the failure

    def stop(failure: ParseFailure):
        stopped.append(failure)
        raise _Stop(failure)

    def operator(op, operands, cm, tm):
        if stopped:
            raise _Stop(stopped[0])  # every later operator, in every enclosing form, stops at once
        try:
            budget.event()
        except ParseFailure as failure:
            stop(failure)

    def text(value, cm, tm, font, size):
        nonlocal seen
        if stopped:
            raise _Stop(stopped[0])
        seen += len(value)
        if seen > most:
            stop(ParseFailure('too_large', 'unit_chars' if most < budget.text_left else 'limit_chars'))

    try:
        value = page.extract_text(visitor_operand_before=operator, visitor_text=text)
    except _Stop:
        pass
    if stopped:  # raised at page level, or swallowed inside a form and then returned normally
        raise stopped[0]
    return value or ''


def _drop_decoded(page, budget) -> None:
    """Forget the decoded copies pypdf keeps of this page's content streams and form XObjects."""
    contents = _get(page, '/Contents')
    streams = list(contents) if isinstance(contents, list) else [contents]
    found, resources = _resources(page, budget)
    xobjects = _get(resources, '/XObject') if found else None
    if isinstance(xobjects, dict):
        streams.extend(xobjects.get(name) for name in xobjects)
    budget.event(len(streams))
    for stream in streams:
        stream = _resolved(stream)
        if getattr(stream, 'decoded_self', None) is not None:
            stream.decoded_self = None


def parse(data: bytes, limits: Limits, budget: _container.Budget) -> tuple[list[Unit], str | None]:
    try:
        budget.clock()
        reader = PdfReader(io.BytesIO(data))
        budget.clock()  # opening cannot be interrupted
        if reader.is_encrypted:
            raise ParseFailure('encrypted', 'pdf_encrypted')
        _check_object_streams(reader, budget)
        _count_pages(reader, limits, budget, len(data))
        pages = reader.pages
        count = len(pages)
        if count > limits.pdf_max_pages:
            raise ParseFailure('too_large', 'pdf_count')
        units = []
        for number in range(count):
            budget.clock()
            page = pages[number]
            text = _clean(_page_text(page, budget)) if _readable(page, budget) else ''
            _drop_decoded(page, budget)
            if text:
                if len(text) > limits.max_unit_chars:
                    raise ParseFailure('too_large', 'unit_chars')
                budget.text(len(text))
                budget.unit()
                units.append(Unit(0, 'page', {'kind': 'page', 'page': number + 1}, text))
    except PdfReadError:
        raise ParseFailure('failed', 'pdf_read_error') from None
    except LimitReachedError:
        raise ParseFailure('too_large', 'pdf_stream') from None
    if not units:
        raise ParseFailure('failed', 'pdf_no_text')
    return units, None

"""EML header block and body lines (design M1 §5.8).

One ``mail_header`` unit (From/To/Cc/Subject/Date that are present, RFC 2047 words decoded) and the
lines of every ``text/plain`` part that is not an attachment (``mail`` units, ``part`` = 1-based
position in ``msg.walk()``). A message without such a part gives the text of its ``text/html`` parts
instead. Attachments are not read, and neither is any part of a message carried inside the
message (``message/rfc822`` and other ``message/*`` parts, attached or inline).

Resources (parser resource design §3.10, M1 design §15.2): a part's text is never held whole. Its
payload is decoded 1 MiB at a time (cut after a line break; for UTF-8 also before any character), fed
to the tag stripper when it is HTML (1 Mi characters at a time, the clock read in between, every piece
it keeps one event), and cut into lines as it comes: the same lines and numbers as splitting the whole
text, one event each, at most ``max_unit_chars`` a line. The message's own copy of the payload is
dropped once it has been read. The message parser reads the bytes 8 KiB at a time with the clock read
on every read; it keeps every line of a part as a str and copies a line several times, so before it
runs the lines are held memory (64 bytes a line in the budget: more than about two million lines is
``too_large/limit_memory``) and events, and a raw line over ``max_unit_chars`` is
``too_large/unit_chars``.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser

from . import Limits, ParseFailure, Unit, _container

NAME = 'eml'
VERSION = 'eml-4'

HEADERS = ('From', 'To', 'Cc', 'Subject', 'Date')
_FEED_CHARS = 1 << 20  # HTML characters given to the tag stripper at a time
_LINE_OBJECTS = 4      # the message parser keeps each line as a str (about 64 bytes): a quarter of a held object
_RAW_BREAK = re.compile(rb'\r\n|\r|\n')
_LINE_BREAK = re.compile(r'\r\n|\r|\n')  # as text.lines: \x0b, \x1c, U+2028 ... do not end a line
_DECODE_BYTES = 1 << 20  # payload bytes decoded at a time
_SKIPPED = frozenset({'script', 'style', 'title'})
_BLOCKS = frozenset({
    'p', 'div', 'br', 'li', 'tr', 'table', 'ul', 'ol', 'blockquote', 'pre', 'hr', 'section', 'article',
    'header', 'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'dt', 'dd'})
_CELLS = frozenset({'td', 'th'})


class _HtmlText(HTMLParser):
    """Visible text of an HTML part: no script/style/title content, a line break after block tags."""

    def __init__(self, budget):
        super().__init__(convert_charrefs=True)
        self.out = []  # the text since the caller last took it
        self._skip = 0
        self._budget = budget

    def _keep(self, piece: str) -> None:
        self._budget.event()
        self.out.append(piece)

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED:
            self._skip += 1
        elif tag == 'br':
            self._keep('\n')

    def handle_startendtag(self, tag, attrs):
        if tag == 'br':
            self._keep('\n')

    def handle_endtag(self, tag):
        if tag in _SKIPPED:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCKS:
            self._keep('\n')
        elif tag in _CELLS:
            self._keep(' ')

    def handle_data(self, data):
        if not self._skip:
            self._keep(data)


class _Reader:
    """The message for the text parser behind ``BytesParser``, which reads it 8 KiB at a time: the bytes
    as ``parsebytes`` hands them over (ASCII with surrogate escapes, line ends untouched, unlike
    ``BytesParser.parse``, whose text wrapper turns every CR and CRLF into LF). The clock is read on every
    read, so parsing a message can be interrupted."""

    def __init__(self, data: bytes, budget):
        self._data, self._pos, self._budget = data, 0, budget

    def read(self, size: int = -1) -> str:
        self._budget.clock()
        end = len(self._data) if size is None or size < 0 else self._pos + size
        piece = self._data[self._pos:end]
        self._pos += len(piece)
        return piece.decode('ascii', errors='surrogateescape')


def _check_lines(data: bytes, limits: Limits, budget) -> None:
    """Before the message parser sees the bytes: its lines are held memory (it keeps each as a str until
    its part ends) and one line is at most ``max_unit_chars`` (it copies a line several times; a mail line
    is at most 998 characters by RFC 5322). Every line is one event."""
    breaks = data.count(b'\n') + data.count(b'\r') - data.count(b'\r\n')
    budget.hold(breaks // _LINE_OBJECTS)
    budget.event(breaks)
    longest = pos = 0
    for match in _RAW_BREAK.finditer(data):  # at most about two million, as the hold above allows
        longest = max(longest, match.start() - pos)
        pos = match.end()
    if max(longest, len(data) - pos) > limits.max_unit_chars:
        raise ParseFailure('too_large', 'unit_chars')


def _splittable(charset: str) -> bool:
    """Whether bytes in this charset can be decoded line by line: ASCII-compatible (a line break is the
    byte 0x0A and never part of another character) and without a state that crosses lines."""
    name = charset.lower().replace('_', '-')
    if name.startswith(('iso-2022', 'utf-7', 'utf-16', 'utf-32', 'hz')):
        return False
    try:
        return '\n'.encode(charset) == b'\n' and 'a\n'.encode(charset) == b'a\n'
    except (LookupError, UnicodeError):
        return False


def _part_chunks(part, budget) -> Iterator[str]:
    """The text of a text part as ``part.get_content()`` decodes it (its charset, ``errors='replace'``,
    UTF-8 when the charset is unknown), handed out about 1 MiB of payload at a time. The message's own
    copy of the payload is dropped once the bytes are taken."""
    data = part.get_payload(decode=True) or b''
    charset = part.get_content_charset('us-ascii')
    try:
        b'a'.decode(charset, errors='replace')
    except LookupError:
        charset = 'utf-8'
    part.set_payload('')
    splittable, utf8 = _splittable(charset), charset.lower().replace('_', '-') in ('utf-8', 'utf8')
    pos = 0
    while pos < len(data):
        budget.clock()
        end = len(data)
        if splittable and pos + _DECODE_BYTES < end:
            cut = data.find(b'\n', pos + _DECODE_BYTES, pos + 8 * _DECODE_BYTES)
            if cut >= 0:
                end = cut + 1
            elif utf8:
                end = pos + _DECODE_BYTES
                while end > pos and 0x80 <= data[end] < 0xC0:  # not inside a character
                    end -= 1
        yield data[pos:end].decode(charset, errors='replace')
        pos = end


def _html_chunks(chunks: Iterator[str], budget) -> Iterator[str]:
    """The visible text of HTML handed in pieces, as the tag stripper gives it piece by piece."""
    parser = _HtmlText(budget)
    for chunk in chunks:
        for start in range(0, len(chunk), _FEED_CHARS):
            parser.feed(chunk[start:start + _FEED_CHARS])
            budget.clock()
            if parser.out:
                yield ''.join(parser.out)
                parser.out.clear()
    parser.close()
    if parser.out:
        yield ''.join(parser.out)


def _chunk_lines(chunks: Iterator[str], limits: Limits, budget) -> Iterator[tuple[int, str]]:
    """``(number, line)`` of text handed in pieces: the same lines and numbers as ``text.lines`` gives for
    the whole text. Only the line still open is kept between pieces, at most ``max_unit_chars``."""
    number, carry = 0, ''
    for chunk in chunks:
        joined = carry + chunk
        held = joined.endswith('\r')  # it may be the first half of \r\n
        found = _LINE_BREAK.split(joined[:-1] if held else joined)
        carry = found.pop() + ('\r' if held else '')
        budget.event(len(found))
        for line in found:
            number += 1
            if len(line) > limits.max_unit_chars:
                raise ParseFailure('too_large', 'unit_chars')
            yield number, line
        if len(carry) > limits.max_unit_chars + 1:
            raise ParseFailure('too_large', 'unit_chars')
    found = _LINE_BREAK.split(carry)
    budget.event(len(found))
    for line in found:
        number += 1
        if len(line) > limits.max_unit_chars:
            raise ParseFailure('too_large', 'unit_chars')
        yield number, line


def _lines(lines: Iterator[tuple[int, str]], part_number: int, units: list, budget) -> None:
    for number, line in lines:
        if line and (line := line.strip()):
            budget.text(len(line))
            budget.unit()
            units.append(Unit(len(units) + 1, 'mail', {'kind': 'mail', 'part': part_number, 'line': number}, line))


def parse(data: bytes, limits: Limits, budget: _container.Budget) -> tuple[list[Unit], str | None]:
    _check_lines(data, limits, budget)
    msg = BytesParser(policy=policy.default).parser.parse(_Reader(data, budget))  # as parsebytes, in pieces
    header_lines = []
    for name in HEADERS:
        value = msg.get(name)
        if value is not None:
            header_lines.append(f'{name}: {value}')
    if not header_lines:
        raise ParseFailure('failed', 'eml_no_headers')
    parts = list(msg.walk())
    if len(parts) > limits.eml_max_parts:
        raise ParseFailure('too_large', 'eml_count')
    units = []
    header_text = '\n'.join(header_lines).strip()
    if header_text:
        budget.text(len(header_text))
        budget.unit()
        units.append(Unit(1, 'mail_header', {'kind': 'mail_header'}, header_text))
    # A message inside the message (message/rfc822 and the other message/* types) is an attachment
    # whatever its disposition: none of its parts is read. They keep their numbers in msg.walk().
    embedded = set()
    for part in parts:
        if part.get_content_maintype() == 'message' and id(part) not in embedded:
            embedded.update(id(inner) for inner in part.walk() if inner is not part)
    readable = [(number, part) for number, part in enumerate(parts, 1)
                if id(part) not in embedded and part.get_content_disposition() != 'attachment']
    plain = [(number, part) for number, part in readable if part.get_content_type() == 'text/plain']
    if plain:
        for number, part in plain:
            _lines(_chunk_lines(_part_chunks(part, budget), limits, budget), number, units, budget)
    else:
        for number, part in readable:
            if part.get_content_type() == 'text/html':
                chunks = _html_chunks(_part_chunks(part, budget), budget)
                _lines(_chunk_lines(chunks, limits, budget), number, units, budget)
    return units, 'rfc822'

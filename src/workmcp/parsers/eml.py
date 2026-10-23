"""EML header block and body lines (design M1 §5.8).

One ``mail_header`` unit (From/To/Cc/Subject/Date that are present, RFC 2047 words decoded) and the
lines of every ``text/plain`` part that is not an attachment (``mail`` units, ``part`` = 1-based
position in ``msg.walk()``). A message without such a part gives the text of its ``text/html`` parts
instead. Attachments are not read, and neither is any part of a message carried inside the
message (``message/rfc822`` and other ``message/*`` parts, attached or inline).

Resources (parser resource design §3.10): the body lines are read like TXT lines (``text.lines``: one
event each, at most ``max_unit_chars`` a line) and HTML is fed to the tag stripper 1 Mi characters at
a time with the clock read in between, every piece of text it keeps being one event. The message
parser reads the bytes 8 KiB at a time with the clock read on every read; it keeps every line of a
part as a str and copies a line several times, so before it runs the lines are held memory (64 bytes a
line in the budget: more than about two million lines is ``too_large/limit_memory``) and events, and a
raw line over ``max_unit_chars`` is ``too_large/unit_chars``.
"""
from __future__ import annotations

import io
import re
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser

from . import Limits, ParseFailure, Unit, _container, text

NAME = 'eml'
VERSION = 'eml-3'

HEADERS = ('From', 'To', 'Cc', 'Subject', 'Date')
_FEED_CHARS = 1 << 20  # HTML characters given to the tag stripper at a time
_LINE_OBJECTS = 4      # the message parser keeps each line as a str (about 64 bytes): a quarter of a held object
_RAW_BREAK = re.compile(rb'\r\n|\r|\n')
_SKIPPED = frozenset({'script', 'style', 'title'})
_BLOCKS = frozenset({
    'p', 'div', 'br', 'li', 'tr', 'table', 'ul', 'ol', 'blockquote', 'pre', 'hr', 'section', 'article',
    'header', 'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'dt', 'dd'})
_CELLS = frozenset({'td', 'th'})


class _HtmlText(HTMLParser):
    """Visible text of an HTML part: no script/style/title content, a line break after block tags."""

    def __init__(self, budget):
        super().__init__(convert_charrefs=True)
        self.out = _container.Pieces()
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


class _Reader(io.BytesIO):
    """The message bytes for ``BytesParser.parse``, which reads them 8 KiB at a time; the clock is read
    on every read, so parsing a message can be interrupted."""

    def __init__(self, data: bytes, budget):
        super().__init__(data)
        self._budget = budget

    def read(self, size=-1):
        self._budget.clock()
        return super().read(size)

    def read1(self, size=-1):
        self._budget.clock()
        return super().read1(size)

    def readinto(self, buffer):
        self._budget.clock()
        return super().readinto(buffer)


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


def _html_text(markup: str, budget) -> str:
    parser = _HtmlText(budget)
    for start in range(0, len(markup), _FEED_CHARS):
        parser.feed(markup[start:start + _FEED_CHARS])
        budget.clock()
    parser.close()
    return parser.out.text()


def _part_text(part) -> str:
    try:
        return part.get_content()
    except (LookupError, ValueError):  # unknown charset name or a content-manager failure
        return (part.get_payload(decode=True) or b'').decode('utf-8', errors='replace')


def _lines(body: str, part_number: int, limits: Limits, units: list, budget) -> None:
    for number, line in text.lines(body, limits, budget):
        if line and (line := line.strip()):
            budget.text(len(line))
            budget.unit()
            units.append(Unit(len(units) + 1, 'mail', {'kind': 'mail', 'part': part_number, 'line': number}, line))


def parse(data: bytes, limits: Limits, budget: _container.Budget) -> tuple[list[Unit], str | None]:
    _check_lines(data, limits, budget)
    msg = BytesParser(policy=policy.default).parse(_Reader(data, budget))
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
            _lines(_part_text(part), number, limits, units, budget)
    else:
        for number, part in readable:
            if part.get_content_type() == 'text/html':
                _lines(_html_text(_part_text(part), budget), number, limits, units, budget)
    return units, 'rfc822'

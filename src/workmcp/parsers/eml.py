"""EML header block and body lines (design M1 §5.8).

One ``mail_header`` unit (From/To/Cc/Subject/Date that are present, RFC 2047 words decoded) and the
lines of every ``text/plain`` part that is not an attachment (``mail`` units, ``part`` = 1-based
position in ``msg.walk()``). A message without such a part gives the text of its ``text/html`` parts
instead. Attachments are not read, and neither is any part of a message carried inside the
message (``message/rfc822`` and other ``message/*`` parts, attached or inline).
"""
from __future__ import annotations

import re
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser

from . import Limits, ParseFailure, Unit

NAME = 'eml'
VERSION = 'eml-2'

HEADERS = ('From', 'To', 'Cc', 'Subject', 'Date')
_LINE_BREAK = re.compile(r'\r\n|\r|\n')
_SKIPPED = frozenset({'script', 'style', 'title'})
_BLOCKS = frozenset({
    'p', 'div', 'br', 'li', 'tr', 'table', 'ul', 'ol', 'blockquote', 'pre', 'hr', 'section', 'article',
    'header', 'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'dt', 'dd'})
_CELLS = frozenset({'td', 'th'})


class _HtmlText(HTMLParser):
    """Visible text of an HTML part: no script/style/title content, a line break after block tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED:
            self._skip += 1
        elif tag == 'br':
            self.out.append('\n')

    def handle_startendtag(self, tag, attrs):
        if tag == 'br':
            self.out.append('\n')

    def handle_endtag(self, tag):
        if tag in _SKIPPED:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCKS:
            self.out.append('\n')
        elif tag in _CELLS:
            self.out.append(' ')

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def _html_text(markup: str) -> str:
    parser = _HtmlText()
    parser.feed(markup)
    parser.close()
    return ''.join(parser.out)


def _part_text(part) -> str:
    try:
        return part.get_content()
    except (LookupError, ValueError):  # unknown charset name or a content-manager failure
        return (part.get_payload(decode=True) or b'').decode('utf-8', errors='replace')


def _lines(text: str, part_number: int, limits: Limits, units: list) -> None:
    for number, line in enumerate(_LINE_BREAK.split(text), 1):
        line = line.strip()
        if line:
            units.append(Unit(len(units) + 1, 'mail', {'kind': 'mail', 'part': part_number, 'line': number}, line))
            if len(units) > limits.max_units:
                raise ParseFailure('too_large', 'limit_units')


def parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    msg = BytesParser(policy=policy.default).parsebytes(data)
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
            _lines(_part_text(part), number, limits, units)
    else:
        for number, part in readable:
            if part.get_content_type() == 'text/html':
                _lines(_html_text(_part_text(part)), number, limits, units)
    return units, 'rfc822'

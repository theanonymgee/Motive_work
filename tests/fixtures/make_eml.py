"""Synthetic EML messages (design M1 §7.3), built with ``email.message.EmailMessage``.

``build()`` returns ``(Expected, bytes)`` pairs parsed with the default ``Limits``; ``write(root)``
writes them below ``root`` (``mail/`` and ``edge/``). ``many_parts`` builds a message for
``Limits(eml_max_parts=...)`` and is used by the parser tests only. Addresses are on reserved
domains (``fixtures.mail_address``), dates and amounts are made up.
"""
from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

from . import LINES_KO, PEOPLE, PROJECTS, SENTINEL, Expected, mail_address

DATE = 'Tue, 04 Mar 2031 10:00:00 +0900'
SUBJECT = f'{PROJECTS[0]} 회의 안내'
HTML_BODY = (
    '<html><head><title>숨겨진 제목</title><style>p { color: red }</style></head><body>'
    f'<p>{LINES_KO[0]}</p><div>견적 &amp; 일정<br>두 번째 줄</div>'
    '<script>var hidden = "스크립트 문장";</script><ul><li>항목 하나</li><li>항목 둘</li></ul>'
    '<table><tr><td>칸1</td><td>칸2</td></tr></table></body></html>')
# (line number in the converted HTML text, text): each block tag ends a line and </ul> adds an empty one.
HTML_LINES = ((1, LINES_KO[0]), (2, '견적 & 일정'), (3, '두 번째 줄'), (4, '항목 하나'), (5, '항목 둘'), (7, '칸1 칸2'))


def _headers(msg: EmailMessage, *, cc: bool = True) -> None:
    msg['From'] = f'{PEOPLE[0]} <{mail_address(PEOPLE[0])}>'
    msg['To'] = f'{PEOPLE[1]} <{mail_address(PEOPLE[1])}>'
    if cc:
        msg['Cc'] = f'{PEOPLE[2]} <{mail_address(PEOPLE[2])}>'
    msg['Subject'] = SUBJECT
    msg['Date'] = DATE


def _header_text(cc: bool = True) -> str:
    lines = [f'From: {PEOPLE[0]} <{mail_address(PEOPLE[0])}>', f'To: {PEOPLE[1]} <{mail_address(PEOPLE[1])}>']
    if cc:
        lines.append(f'Cc: {PEOPLE[2]} <{mail_address(PEOPLE[2])}>')
    return '\n'.join([*lines, f'Subject: {SUBJECT}', f'Date: {DATE}'])


def _body(lines) -> str:
    return '\n'.join(lines) + '\n'


def _mail_units(lines, part: int, first_ordinal: int = 2) -> dict[int, tuple[str, dict, str]]:
    sample, ordinal = {}, first_ordinal - 1
    for number, line in enumerate(_body(lines).split('\n'), 1):
        if line.strip():
            ordinal += 1
            sample[ordinal] = ('mail', {'kind': 'mail', 'part': part, 'line': number}, line.strip())
    return sample


def _expected(rel_path, lines, part, *, cc=True) -> Expected:
    sample = {1: ('mail_header', {'kind': 'mail_header'}, _header_text(cc)), **_mail_units(lines, part)}
    return Expected(rel_path, 'eml', 'ok', None, len(sample), sample, 'rfc822')


def plain_message(lines=LINES_KO, charset='utf-8') -> bytes:
    msg = EmailMessage()
    _headers(msg)
    msg.set_content(_body(lines), charset=charset)
    return msg.as_bytes()


def multipart_message() -> bytes:
    """mixed[ alternative[ plain (part 3), html (part 4) ], attachment .txt (part 5) ]."""
    msg = EmailMessage()
    _headers(msg)
    msg.set_content(_body(LINES_KO))
    msg.add_alternative(HTML_BODY, subtype='html')
    msg.add_attachment(f'{SENTINEL} 첨부 파일 내용 줄\n'.encode(), maintype='text', subtype='plain', filename='첨부.txt')
    msg.set_boundary('=_synthetic_mixed_=')  # fixed boundaries: the same bytes every run
    msg.get_payload(0).set_boundary('=_synthetic_alternative_=')
    return msg.as_bytes()


def html_only_message() -> bytes:
    msg = EmailMessage()
    _headers(msg, cc=False)
    msg.set_content(HTML_BODY, subtype='html')
    return msg.as_bytes()


def html_with_text_attachment_message() -> bytes:
    """An HTML body whose only text/plain part is an attachment: the HTML is read (part 2)."""
    msg = EmailMessage()
    _headers(msg, cc=False)
    msg.set_content(HTML_BODY, subtype='html')
    msg.add_attachment('첨부에만 있는 문장\n'.encode(), maintype='text', subtype='plain', filename='첨부.txt')
    msg.set_boundary('=_synthetic_mixed_=')
    return msg.as_bytes()


def many_parts(count: int) -> bytes:
    """A multipart message with ``count`` text parts (``count + 1`` parts counting the container)."""
    msg = EmailMessage()
    _headers(msg, cc=False)
    msg.set_content('첫 파트\n')
    for number in range(2, count + 1):
        msg.add_attachment(f'파트 {number}\n'.encode(), maintype='text', subtype='plain', filename=f'p{number}.txt')
    msg.set_boundary('=_synthetic_mixed_=')
    return msg.as_bytes()


ATTACHED_LINE = '첨부된 메일 본문 줄'


def forwarded_message(*, inline: bool = False) -> bytes:
    """mixed[ plain (part 2), message/rfc822 (part 3)[ the attached message (part 4) ] ].

    The attached message has its own headers and a text/plain body (``ATTACHED_LINE``), which the parser
    must not read. ``inline`` drops the attachment disposition of the message/rfc822 part.
    """
    inner = EmailMessage()
    inner['From'] = f'{PEOPLE[2]} <{mail_address(PEOPLE[2])}>'
    inner['Subject'] = '전달된 합성 메일'
    inner['Date'] = DATE
    inner.set_content(f'{ATTACHED_LINE}\n')
    msg = EmailMessage()
    _headers(msg, cc=False)
    msg.set_content(_body(LINES_KO[:2]))
    msg.add_attachment(inner)
    msg.set_boundary('=_synthetic_mixed_=')
    if inline:
        del msg.get_payload(1)['Content-Disposition']
    return msg.as_bytes()


NO_HEADERS ='\n본문만 있고 헤더가 없는 바이트 합성표지7f3a\n'.encode()


def html_expected(rel_path: str, part: int) -> Expected:
    sample = {1: ('mail_header', {'kind': 'mail_header'}, _header_text(False))}
    for ordinal, (line, text) in enumerate(HTML_LINES, 2):
        sample[ordinal] = ('mail', {'kind': 'mail', 'part': part, 'line': line}, text)
    return Expected(rel_path, 'eml', 'ok', None, len(sample), sample, 'rfc822')


def build() -> list[tuple[Expected, bytes]]:
    return [
        (_expected('mail/plain_utf8.eml', LINES_KO, 1), plain_message()),
        (_expected('mail/multipart.eml', LINES_KO, 3), multipart_message()),
        (_expected('mail/plain_euckr.eml', LINES_KO, 1), plain_message(charset='euc-kr')),
        (html_expected('mail/html_only.eml', 1), html_only_message()),
        (Expected('edge/no_headers.eml', 'eml', 'failed', 'eml_no_headers'), NO_HEADERS),
    ]


def write(root: Path) -> list[Expected]:
    expected = []
    for item, data in build():
        target = Path(root) / item.rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        expected.append(item)
    return expected

"""Synthetic document bundle: content rules and shared values (design M1 §7.1, §7.2).

Documents are generated at test time by ``make_*.py`` in this package (standard library only)
into a temporary folder outside the repository. No binary file is committed here, and the
generators' sources are what a reviewer reads to confirm that every document is synthetic.

Content rules (every generator follows them):

1. No real person, company, product, address, phone number or e-mail address. People are
   ``PEOPLE`` ("담당자 갑/을/병"), organisations ``ORGS``, projects ``PROJECTS``. Mail addresses and
   hosts use the reserved domains ``example.com``/``example.org``/``example.net`` or the ``.invalid``
   top-level domain (RFC 2606) only, through ``mail_address``. Amounts and dates are made up.
2. Every generated document carries ``SENTINEL`` at least once (``SENTINEL_ASCII`` where only
   ASCII text can be written, e.g. the WinAnsi PDF), so tests can show that no document text
   reaches stdout, stderr or logs.
3. Text comes from ``LINES_KO``/``LINES_EN`` (or is built from them), so each format's expected
   units can be computed.
4. A generator returns ``Expected`` values; parser tests and bundle tests share them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SENTINEL = '합성표지7f3a'
SENTINEL_ASCII = 'synthetic-7f3a'
RESERVED_DOMAINS = ('example.com', 'example.org', 'example.net')
RESERVED_TLD = '.invalid'

PEOPLE = ('담당자 갑', '담당자 을', '담당자 병')
ORGS = ('가상 거래처 A', '가상 거래처 B')
PROJECTS = ('합성 프로젝트 1호', '합성 프로젝트 2호')

LINES_KO = (
    f'{SENTINEL} 합성 프로젝트 1호 주간 회의록입니다.',
    '담당자 갑이 가상 거래처 A에 견적 1,234,000원을 보냈다.',
    '다음 회의는 2031년 3월 4일 오전 10시로 정했다.',
    '담당자 을은 일정표 초안을 금요일까지 공유한다.',
    '검토 항목: 요구사항 7건, 위험 2건, 보류 1건.',
)
LINES_EN = (
    f'{SENTINEL_ASCII} Synthetic project 1 weekly notes.',
    'Owner A sent quote Q-0042 for 1234 units to Fictional Customer A.',
    'Next review is set for 2031-03-04 at 10:00.',
    'Owner B will share the draft schedule by Friday.',
    'Open items: 7 requirements, 2 risks, 1 on hold.',
)

ASCII_NAMES = {'담당자 갑': 'owner.gap', '담당자 을': 'owner.eul', '담당자 병': 'owner.byeong'}


def mail_address(person: str, domain: str = RESERVED_DOMAINS[0]) -> str:
    """Synthetic address of one of ``PEOPLE`` on a reserved domain."""
    if domain not in RESERVED_DOMAINS and not domain.endswith(RESERVED_TLD):
        raise ValueError(f'not a reserved domain: {domain}')
    return f'{ASCII_NAMES[person]}@{domain}'


@dataclass(frozen=True)
class Expected:
    """What parsing one generated file must give (design §7.2).

    ``units_sample`` maps an ordinal (1-based) to ``(kind, unit, text)`` for the units a test
    checks exactly; ``unit_count`` is the total. For a non-ok status, ``unit_count`` is 0.
    """
    rel_path: str                      # path inside the bundle root, '/' separated
    parser: str                        # ParseResult.parser
    status: str                        # ParseResult.status
    error: str | None = None           # ParseResult.error
    unit_count: int = 0
    units_sample: dict[int, tuple[str, dict, str]] = field(default_factory=dict)
    encoding: str | None = None

"""EML header block and body lines (design M1 §5.8). Stage-0 stub."""
from __future__ import annotations

from email import policy  # noqa: F401  (design §8.1 import lines; used by the implementation)
from email.parser import BytesParser  # noqa: F401
from html.parser import HTMLParser  # noqa: F401

from . import Limits, ParseFailure, Unit

NAME = 'eml'
VERSION = 'eml-1'


def parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    raise ParseFailure('failed', 'not_implemented')

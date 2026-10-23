"""DOCX paragraphs with table cell coordinates (design M1 §5.8). Stage-0 stub."""
from __future__ import annotations

from . import Limits, ParseFailure, Unit, _container  # noqa: F401  (_container: used by the implementation)

NAME = 'docx'
VERSION = 'docx-1'


def parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    raise ParseFailure('failed', 'not_implemented')

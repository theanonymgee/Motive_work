"""PDF pages through pypdf (design M1 §5.8). Stage-0 stub."""
from __future__ import annotations

import importlib.metadata
import logging

from pypdf import PdfReader  # noqa: F401  (design §8.1 import lines; used by the implementation)
from pypdf.errors import PdfReadError  # noqa: F401

from . import Limits, ParseFailure, Unit

NAME = 'pdf'
VERSION = f'pdf-1/pypdf-{importlib.metadata.version("pypdf")}'

logging.getLogger('pypdf').setLevel(logging.ERROR)  # pypdf warnings may quote document content


def parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    raise ParseFailure('failed', 'not_implemented')

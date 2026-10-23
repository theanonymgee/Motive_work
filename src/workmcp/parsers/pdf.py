"""PDF pages through pypdf (design M1 §5.8).

One unit per page that has extractable text (``{"kind": "page", "page": p}``, ``p`` counts every
page, empty ones included). An encrypted PDF is only reported, never decrypted. Nothing a library
logs or raises is kept: pypdf's logger is silenced because its messages can quote document content,
and the dispatcher drops exception messages. The parse time (``Limits.parse_seconds``) is checked
before each page (``too_large/parse_time``); a single pypdf call (opening the file, one page's text)
runs to its end.
"""
from __future__ import annotations

import importlib.metadata
import io
import logging

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from . import Limits, ParseFailure, Unit, _container

NAME = 'pdf'
VERSION = f'pdf-2/pypdf-{importlib.metadata.version("pypdf")}'

_logger = logging.getLogger('pypdf')
_logger.setLevel(logging.CRITICAL)  # pypdf warnings may quote document content
_logger.propagate = False           # and no root handler (log file, stderr) may receive them
_logger.addHandler(logging.NullHandler())


def _clean(text: str) -> str:
    # A lone surrogate from a broken font map cannot be stored; make it a replacement character.
    return text.encode('utf-8', errors='replace').decode('utf-8').strip()


def parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    clock = _container.Clock(limits)
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ParseFailure('encrypted', 'pdf_encrypted')
        pages = reader.pages
        count = len(pages)
        if count > limits.pdf_max_pages:
            raise ParseFailure('too_large', 'pdf_count')
        units, chars = [], 0
        for number in range(count):
            clock.check()  # between pages only: one pypdf call cannot be interrupted
            text = _clean(pages[number].extract_text() or '')
            if text:
                units.append(Unit(0, 'page', {'kind': 'page', 'page': number + 1}, text))
                chars += len(text)
                if chars > limits.max_text_chars:
                    raise ParseFailure('too_large', 'limit_chars')
    except PdfReadError:
        raise ParseFailure('failed', 'pdf_read_error') from None
    if not units:
        raise ParseFailure('failed', 'pdf_no_text')
    return units, None

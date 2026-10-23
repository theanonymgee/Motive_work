"""HWP 5.x (OLE records) and HWPX (zip + XML) section paragraphs (design M1 §5.8). Stage-0 stub."""
from __future__ import annotations

import importlib.metadata
import struct  # noqa: F401  (design §8.1 import lines; used by the implementation)
import zlib  # noqa: F401

import olefile  # noqa: F401

from . import Limits, ParseFailure, Unit, _container  # noqa: F401

NAME = 'hwp'
VERSION = f'hwp-1/olefile-{importlib.metadata.version("olefile")}'
HWPX_NAME = 'hwpx'
HWPX_VERSION = 'hwpx-1'


def parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    raise ParseFailure('failed', 'not_implemented')


def parse_hwpx(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    raise ParseFailure('failed', 'not_implemented')

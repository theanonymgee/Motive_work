"""TXT/MD lines and CSV rows with the encoding ladder (design M1 §5.7, §5.8). Stage-0 stub."""
from __future__ import annotations

import csv  # noqa: F401  (design §8.1 import line; used by the implementation)

from . import Limits, ParseFailure, Unit

NAME = 'text'
VERSION = 'text-1'
CSV_NAME = 'csv'
CSV_VERSION = 'csv-1'


def parse(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    raise ParseFailure('failed', 'not_implemented')


def parse_csv(data: bytes, limits: Limits) -> tuple[list[Unit], str | None]:
    raise ParseFailure('failed', 'not_implemented')

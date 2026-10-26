"""``workmcp push``: send what the operating server lacks, from this collector's store (design M5 remote §3.4,
§3.6).

The server is the reference and this side keeps no record of what it sent: every run asks the server for
its two summary hashes (``sync.summary`` on both sides); equal hashes end the run with no batch and no
byte sent. Otherwise documents and versions go first, then the unit text of the versions the server asks
for (split into parts), the vectors it lacks, the document events and the end batch. A batch is
idempotent (``sync.batch_id``): connection errors, 5xx and retryable answers resend the same batch after
``RETRY_DELAYS``; a 4xx stops the run (exit 1).

Boundaries (``tests/test_network_boundary.py``, ``tests/test_readonly.py``): besides the model download this is
the only module that sends anything over the network, and only to the configured server. ``urllib`` is
imported inside the request function only; no ``ssl`` import (the default context; a private CA comes
through ``SSL_CERT_FILE``); no URL literal: every destination is ``sync.endpoint`` of ``config [push] url``.
No file is opened here: the store is read through ``store.open_read`` and the token through
``paths.read_token`` (or ``WORKMCP_PUSH_TOKEN``). It never imports the collector, the parsers or the binding
check, so it cannot read an original file. Its log ``<home>/logs/push-YYYYMMDD.log`` and the report hold
paths, short hashes and counts, never unit text or the token.

Exit codes: 0 done (up to date included), 1 a batch refused or a step failed, 2 data home refused, 4 settings,
arguments, no token or a server of another schema/chunk version, 6 server unreachable after the retries.

Stage 0 (design M5 remote §11.1) fixes the signatures, the report keys and the exit codes; ``execute`` and the
request function come in stage 1. (Not named ``run``: the network boundary test reserves ``run(...)`` for the
MCP transports.)
"""
from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import config, paths, sync

LOG = logging.getLogger('workmcp.push')
STAGE1 = 'workmcp push: not implemented yet (M5 stage 1)'
EXIT_OK, EXIT_FAILED, EXIT_HOME, EXIT_USAGE, EXIT_UNREACHABLE = 0, 1, 2, 4, 6
RETRY_DELAYS = (1, 2, 4, 8, 16)            # seconds before each resend of the same batch
REQUEST_TIMEOUT_SECONDS = 300

# execute() result: what left this PC (design §3.6); counts, sizes, hashes and paths only.
REPORT_KEYS = ('exit_code', 'push_id', 'url', 'collector', 'started_at', 'up_to_date', 'dry_run', 'state', 'sent',
               'batches', 'bytes_sent', 'state_requests', 'refused', 'retries', 'elapsed', 'log', 'server')
STATE_COUNT_KEYS = ('documents_changed', 'added', 'changed', 'deleted', 'restored', 'reparsed', 'vectors_missing')
SENT_KEYS = ('documents', 'versions', 'units', 'chars', 'vectors', 'vector_bytes', 'events')
SERVER_KEYS = ('documents_sha256', 'vectors_sha256', 'vectors_missing')


@dataclass(frozen=True)
class Options:
    """One field per ``workmcp push`` argument (design §4.6, §6.3)."""
    max_mb: int | None = None                  # --max-mb N: stop sending after N MiB in this run
    limit_docs: int | None = None              # --limit-docs N: at most N changed documents in this run
    token_file: str | None = None              # --token-file PATH (inside the data home; default push.token)
    dry_run: bool = False                      # --dry-run: compare and report, send no batch
    require_vectors: bool = False              # --require-vectors: exit 1 while a target chunk has no vector
    allow_insecure_loopback: bool = False      # --allow-insecure-loopback: http:// to a loopback test server


def options_from_args(args) -> Options:
    return Options(max_mb=args.max_mb, limit_docs=args.limit_docs, token_file=args.token_file, dry_run=args.dry_run,
                   require_vectors=args.require_vectors, allow_insecure_loopback=args.allow_insecure_loopback)


def check_options(options: Options) -> str | None:
    """The first argument rule ``options`` breaks (one line for stderr, exit 4), else None."""
    if options.max_mb is not None and options.max_mb < 1:
        return '--max-mb must be a positive integer'
    if options.limit_docs is not None and options.limit_docs < 1:
        return '--limit-docs must be a positive integer'
    return None


def push_token(home: Path, env: Mapping[str, str], options: Options) -> str:
    """``WORKMCP_PUSH_TOKEN``, else the token file (``paths.read_token``; ``paths.TokenFileError``)."""
    token = (env.get(paths.PUSH_TOKEN_ENV) or '').strip()
    return token or paths.read_token(home, options.token_file)


def execute(home: Path, settings: config.Config, token: str, options: Options, *, log=print) -> dict:
    """One push run (design §3.4) to ``settings.push.url``; returns ``REPORT_KEYS``. Stage 1."""
    raise NotImplementedError(STAGE1)


def report_lines(report: dict) -> list[str]:
    """The stdout report of ``execute`` (design §3.6). Stage 1."""
    raise NotImplementedError(STAGE1)


def main(args) -> int:
    """``workmcp push`` body: data home (exit 2), settings, URL, arguments and token (exit 4), ``execute``."""
    try:
        home = paths.resolve_home()
    except paths.DataPathError as exc:
        print(exc, file=sys.stderr)
        return EXIT_HOME
    try:
        settings = config.load(home)
    except config.ConfigError as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE
    options = options_from_args(args)
    if error := check_options(options):
        print(error, file=sys.stderr)
        return EXIT_USAGE
    if settings.push.url is None:
        print(f'[push] url is not set in {settings.file}', file=sys.stderr)
        return EXIT_USAGE
    if problem := sync.url_problem(settings.push.url, allow_insecure_loopback=options.allow_insecure_loopback):
        print(f'[push] url: {problem}', file=sys.stderr)
        return EXIT_USAGE
    try:
        token = push_token(home, os.environ, options)
    except paths.TokenFileError as exc:
        print(f'{exc} (or set {paths.PUSH_TOKEN_ENV})', file=sys.stderr)
        return EXIT_USAGE
    report = execute(home, settings, token, options)
    print('\n'.join(report_lines(report)))
    return report['exit_code']

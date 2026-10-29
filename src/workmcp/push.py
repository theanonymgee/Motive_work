"""``workmcp push``: send what the operating server lacks, from this collector's store (design M5 remote §3.4,
§3.6).

The server is the reference and this side keeps no record of what it sent: every run asks the server for
its two summary hashes (``sync.summary`` on both sides) and its event count; equal values end the run with no
batch and no byte sent. Otherwise the per-document summaries are compared page by page and only the
documents that differ go out (with all their versions), then the unit text of the versions the server
answers ``need`` for (a version the server can copy from another one with the same content is never sent;
a long version is split into parts, a long unit into pieces), the vectors it lacks, the document events and
the end batch. A batch is idempotent (``sync.batch_id``): connection errors, 5xx answers and retryable
refusals (``store_busy``) resend the same batch after ``RETRY_DELAYS``; any other refusal stops the run.

Boundaries (``tests/test_network_boundary.py``, ``tests/test_readonly.py``): besides the model download this is
the only module that sends anything over the network, and only to the configured server. ``urllib`` is
imported inside the request function only; no ``ssl`` import (the default context; a private CA comes
through ``SSL_CERT_FILE``); no URL literal: every destination is ``sync.endpoint`` of ``config [push] url``.
The opener follows no redirect and uses no proxy (neither the environment's nor the system's), so the
connection goes to the configured host and nowhere else. The token travels as an unredirected
``Authorization`` header (not in the request's audited headers, never logged).
No file is opened here: the store is read through ``store.open_read`` (one read snapshot for the whole run)
and the token through ``paths.read_token`` (or ``WORKMCP_PUSH_TOKEN``). It never imports the collector, the
parsers or the binding check, so it cannot read an original file. Its log ``<home>/logs/push-YYYYMMDD.log``
and the report hold paths, short hashes and counts, never unit text or the token.

Exit codes: 0 done (up to date included), 1 a batch or document refused or a step failed, 2 data home refused,
3 another push of this data home is running (``push.lock``), 4 settings, arguments, no token or a server of
another schema/chunk version, 6 server unreachable after the retries, 7 the server's TLS certificate did not
verify (not retried).

A rebuilt or restored store (review M5 remote v1 M-1) needs nothing special: the server matches documents by
path and versions by content, the local ids it no longer finds here are sent as ``forget``, and when the
event history differs from the server's (``events_sha256``) all events are sent again with ``events_reset``.

(Not named ``run``: the network boundary test reserves ``run(...)`` for the MCP transports.)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__, chunks, config, paths, store, sync

LOG = logging.getLogger('workmcp.push')
LOG_FORMAT = '%(asctime)s | %(message)s'
EXIT_OK, EXIT_FAILED, EXIT_HOME, EXIT_LOCKED, EXIT_USAGE, EXIT_UNREACHABLE, EXIT_TLS = 0, 1, 2, 3, 4, 6, 7
PUSH_LOCK = store.PUSH_LOCK_NAME           # one push at a time per data home (store.acquire_lock)
PUSH_LOCK_STALE = store.PUSH_LOCK_STALE    # a lock left by a killed push stops blocking after this long
PUSH_LOCK_HEARTBEAT = timedelta(minutes=5)  # a live push writes heartbeat_at this often (never looks stale)
CERTIFICATE_ERRORS = ('SSLCertVerificationError',)   # by class name: this module does not import ssl
RETRY_DELAYS = (1, 2, 4, 8, 16)            # seconds before each resend of the same batch
REQUEST_TIMEOUT_SECONDS = 300
CLIENT_VERSION = __version__               # recorded by the server (pushes.client_version); the release number

# execute() result: what left this PC (design §3.6); counts, sizes, hashes and paths only.
REPORT_KEYS = ('exit_code', 'push_id', 'url', 'collector', 'started_at', 'up_to_date', 'dry_run', 'state', 'sent',
               'batches', 'bytes_sent', 'state_requests', 'refused', 'retries', 'elapsed', 'log', 'server',
               'converged', 'error', 'warning')
STATE_COUNT_KEYS = ('documents_changed', 'added', 'changed', 'deleted', 'restored', 'reparsed', 'vectors_missing')
SENT_KEYS = ('documents', 'versions', 'units', 'chars', 'vectors', 'vector_bytes', 'events', 'copied_versions',
             'forgotten')
SERVER_KEYS = ('documents_sha256', 'vectors_sha256', 'vectors_missing')
REFUSED_KEYS = ('path', 'code')
TOO_MANY_PARTS = 'too_many_parts'          # refused here: a version needs more than sync.MAX_PARTS parts

# A transport sends one request and returns (HTTP status, response body bytes); it raises Unreachable when
# no answer came. The default is ``http_transport``; tests pass an in-memory one.
Transport = Callable[[str, str, bytes | None], tuple[int, bytes]]


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


# ---- requests ------------------------------------------------------------------------------------------

class Unreachable(Exception):
    """No answer from the server (connection refused or reset, timeout, name not found); the message is the
    exception class name only."""


class CertificateFailed(Exception):
    """The server's TLS certificate did not verify (never retried: the same certificate fails again)."""


class Stop(Exception):
    """The run ends here: ``exit_code`` and one line for the report (codes and counts, no text)."""

    def __init__(self, exit_code: int, message: str, code: str | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.code = code                           # the server's refusal code, if a refusal ended the run


def http_transport(token: str, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> Transport:
    """Requests through ``urllib`` with no redirect and no proxy; the token as an unredirected header."""

    def send(method: str, url: str, data: bytes | None) -> tuple[int, bytes]:
        import urllib.error
        import urllib.request

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None                        # a 3xx answer is an error, never another destination

        headers = {'Accept': 'application/json'}
        if data is not None:
            headers.update({'Content-Type': 'application/json', 'Content-Encoding': 'gzip'})
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        request.add_unredirected_header('Authorization', f'Bearer {token}')
        # urlopen through an opener without proxy and redirect handling, for this one request only
        urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:   # TimeoutError, ConnectionError are OSError
            reason = getattr(exc, 'reason', None)
            if CERTIFICATE_ERRORS[0] in (type(exc).__name__, type(reason).__name__):
                raise CertificateFailed(type(reason or exc).__name__) from None
            raise Unreachable(type(exc).__name__) from None
        finally:
            urllib.request.install_opener(None)

    return send


@dataclass
class _Run:
    url: str
    send: Transport
    sleep: Callable[[float], None]
    logger: logging.Logger
    report: dict
    collector: str = ''
    push_id: int | None = None
    budget: int | None = None                       # --max-mb in bytes
    stopped: str | None = None                      # why sending stopped early (--max-mb)
    note: str | None = None                         # what this run left for the next one (--limit-docs)
    lock: Path | None = None                        # push.lock held by this run, its nonce and last heartbeat
    nonce: str | None = None
    beat_at: datetime | None = None

    def heartbeat(self) -> None:
        """Before each request: check that ``push.lock`` still holds this run's nonce (one read; review M6 v1
        M6-4) and write ``heartbeat_at`` into it when ``PUSH_LOCK_HEARTBEAT`` has passed since the last time, so a
        live push never looks stale to another run (review M5 remote v3 V3-5). A lock that holds another run's
        nonce ends this run (exit 3) before it sends anything more; a read or write that fails now is tried again
        at the next request."""
        if self.lock is None:
            return
        at = store.lock_now()
        beat_due = self.beat_at is None or at - self.beat_at >= PUSH_LOCK_HEARTBEAT
        try:
            held = store.touch_lock(self.lock, self.nonce, at) if beat_due else store.lock_held(self.lock, self.nonce)
        except OSError as exc:
            self.logger.info(f'warning | push lock {"heartbeat not written" if beat_due else "not readable"} '
                             f'({type(exc).__name__}); trying again')
            return
        if not held:
            raise Stop(EXIT_LOCKED, f'another push took over {self.lock}; this run stops')
        if beat_due:
            self.beat_at = at

    def get(self, name: str, query: dict | None = None) -> dict:
        self.report['state_requests'] += 1
        return self._request('GET', sync.endpoint(self.url, name, query), None)

    def post(self, kind: str, items: list) -> dict:
        body = sync.envelope(kind, items, collector=self.collector, push_id=self.push_id,
                             client_version=CLIENT_VERSION)
        result = self._request('POST', sync.endpoint(self.url, 'batch'), sync.gzip_json(body))
        self.report['batches'] += 1
        return result

    def over_budget(self) -> bool:
        if self.budget is not None and self.report['bytes_sent'] >= self.budget:
            self.stopped = f'stopped after --max-mb ({self.report["bytes_sent"]} bytes sent)'
        return self.stopped is not None

    def _request(self, method: str, url: str, data: bytes | None) -> dict:
        reason = ''
        for attempt in range(len(RETRY_DELAYS) + 1):
            if attempt:
                self.report['retries'] += 1
                self.sleep(RETRY_DELAYS[attempt - 1])
            self.heartbeat()
            if data is not None:
                self.report['bytes_sent'] += len(data)
            try:
                status, raw = self.send(method, url, data)
            except CertificateFailed:
                host = sync.parse_url(self.url).host
                raise Stop(EXIT_TLS, f'TLS certificate verification failed for {host} (not retried): check the '
                                     'server name in [push] url, or set SSL_CERT_FILE to the CA that signed it') from None
            except Unreachable as exc:
                reason = f'server unreachable ({exc})'
                continue
            try:
                answer = json.loads(raw.decode('utf-8'))
                answer_ok = answer['ok'] is True
            except (UnicodeDecodeError, ValueError, TypeError, KeyError):
                answer, answer_ok = None, False
            if status == 200 and answer_ok:
                return answer['result']
            error = answer.get('error') if isinstance(answer, dict) and not answer_ok else None
            code = error.get('code') if isinstance(error, dict) else None
            retryable = status >= 500 or (isinstance(error, dict) and error.get('retryable') is True)
            reason = f'HTTP {status}' + (f' {code}' if code else '')
            if not retryable:
                exit_code = EXIT_USAGE if code in ('schema_version_mismatch', 'chunk_version_mismatch') else EXIT_FAILED
                message = error.get('message') if isinstance(error, dict) else ''
                raise Stop(exit_code, f'refused: {reason}' + (f' ({message})' if message else ''), code)
        unreachable = reason.startswith('server unreachable')
        raise Stop(EXIT_UNREACHABLE if unreachable else EXIT_FAILED, f'{reason} after {len(RETRY_DELAYS)} retries')


# ---- what this store holds -----------------------------------------------------------------------------

_DOCUMENT_COLUMNS = ('id', 'root_id', 'path', 'path_key', 'ext', 'status', 'skip_reason', 'first_seen_at',
                     'last_seen_at', 'deleted_at')
_VERSION_COLUMNS = ('id', 'seq', 'sha256', 'size', 'mtime_ns', 'observed_at', 'superseded_at', 'parser',
                    'parser_version', 'encoding', 'parse_status', 'parse_error', 'unit_count', 'text_chars')


def local_model_key(db) -> str | None:
    """The model key of this collector's vectors: that of its last finished ``index`` run."""
    last = store.last_index(db)
    return last['model_key'] if last else None


def document_item(db, document_id: int) -> tuple[dict, str]:
    """The ``documents`` item of one document (every version with its ``units_sha256``) and its path."""
    row = dict(zip(_DOCUMENT_COLUMNS, db.execute(f'SELECT {", ".join(_DOCUMENT_COLUMNS)} FROM documents WHERE id = ?',
                                                 (document_id,)).fetchone()))
    versions = []
    for values in db.execute(f'SELECT {", ".join(_VERSION_COLUMNS)} FROM versions WHERE document_id = ? ORDER BY seq',
                             (document_id,)).fetchall():
        version = dict(zip(_VERSION_COLUMNS, values))
        local = version.pop('id')
        has_units = version['parse_status'] == 'ok' and version['unit_count']
        longest = db.execute('SELECT MAX(LENGTH(text)) FROM units WHERE version_id = ?', (local,)).fetchone()[0]
        versions.append({'local_version_id': local, **version,
                         'units_sha256': sync.version_units_sha256(db, local) if has_units else None,
                         'unit_chars_max': longest})
    item = {'local_document_id': row['id'], 'local_root_id': row['root_id'], 'path': row['path'],
            'path_key': row['path_key'], 'ext': row['ext'], 'status': row['status'], 'skip_reason': row['skip_reason'],
            'first_seen_at': row['first_seen_at'], 'last_seen_at': row['last_seen_at'],
            'deleted_at': row['deleted_at'], 'versions': versions}
    return item, row['path']


def unit_parts(db, version_id: int) -> list[list[dict]] | None:
    """The wire units of one version cut into parts that each fit one batch (``UNITS_PER_BATCH`` entries,
    ``UNIT_CHARS_PER_BATCH`` characters, ``BATCH_MAX_BYTES`` less the envelope reserve); a part ends after each
    piece of a long unit that continues, so the server applies a long unit one piece at a time. None when the
    version needs more than ``sync.MAX_PARTS`` parts (the server would refuse it: ``_send_units`` refuses that
    version alone, review M5 remote v3 V3-6)."""
    budget = sync.BATCH_MAX_BYTES - sync.ENVELOPE_RESERVE
    parts, current, chars, size = [], [], 0, 0
    for ordinal, kind, locator_json, text, text_sha256 in db.execute(
            'SELECT ordinal, kind, locator_json, text, text_sha256 FROM units WHERE version_id = ? ORDER BY ordinal',
            (version_id,)):
        unit = {'ordinal': ordinal, 'kind': kind, 'locator_json': locator_json, 'text': text, 'text_sha256': text_sha256}
        pieces = sync.unit_pieces(unit, budget // 2)
        for number, piece in enumerate(pieces, start=1):
            piece_size, piece_chars = sync.canonical_size(piece) + 1, len(piece['text'])
            if current and (len(current) >= sync.UNITS_PER_BATCH or chars + piece_chars > sync.UNIT_CHARS_PER_BATCH
                            or size + piece_size > budget):
                parts.append(current)
                current, chars, size = [], 0, 0
            current.append(piece)
            chars += piece_chars
            size += piece_size
            if number < len(pieces):        # a unit that goes on: one of its pieces per part (a short server step)
                parts.append(current)
                current, chars, size = [], 0, 0
    if current:
        parts.append(current)
    return None if len(parts) > sync.MAX_PARTS else parts


def _batches(items, *, count: int, budget: int = sync.BATCH_MAX_BYTES - sync.ENVELOPE_RESERVE):
    """Consecutive lists of ``items`` with at most ``count`` entries and ``budget`` canonical bytes each."""
    current, size = [], 0
    for item in items:
        item_size = sync.canonical_size(item) + 1
        if current and (len(current) >= count or size + item_size > budget):
            yield current
            current, size = [], 0
        current.append(item)
        size += item_size
    if current:
        yield current


def _kind_of_change(local: dict, remote: dict | None) -> str:
    if remote is None:
        return 'added'
    if local['status'] == 'deleted' and remote['status'] != 'deleted':
        return 'deleted'
    if local['status'] == 'active' and remote['status'] in ('deleted', 'skipped'):
        return 'restored'
    if local['sha256'] == remote['sha256'] and local['superseded'] == remote['superseded']:
        return 'reparsed'
    return 'changed'


def _missing_targets(db, model_key: str | None) -> int:
    """Target chunk texts of this store without a vector for ``model_key`` (``--require-vectors``)."""
    sql = f'SELECT COUNT(DISTINCT c.text_sha256) FROM {store.latest_chunks_sql()} AND c.chunk_version = ?'
    if model_key is None:
        return db.execute(sql, (chunks.CHUNK_VERSION,)).fetchone()[0]
    return db.execute(sql + ' AND NOT EXISTS(SELECT 1 FROM chunk_vectors x WHERE x.model_key = ? AND '
                            'x.text_sha256 = c.text_sha256)', (chunks.CHUNK_VERSION, model_key)).fetchone()[0]


# ---- one run --------------------------------------------------------------------------------------------

def _file_logger(home: Path) -> tuple[logging.Logger, logging.Handler, Path]:
    log_file = home/'logs'/f'push-{datetime.now():%Y%m%d}.log'
    handler = logging.FileHandler(log_file, encoding='utf-8')
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger = logging.getLogger('workmcp.push.file')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return logger, handler, log_file


def new_report(url: str) -> dict:
    report = dict.fromkeys(REPORT_KEYS)
    report.update(exit_code=EXIT_OK, url=url, started_at=store.now(), up_to_date=False, dry_run=False,
                  state=dict.fromkeys(STATE_COUNT_KEYS, 0), sent=dict.fromkeys(SENT_KEYS, 0), batches=0, bytes_sent=0,
                  state_requests=0, refused=[], retries=0, converged=False)
    return report


def execute(home: Path, settings: config.Config, token: str, options: Options, *,
            transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """One push run (design §3.4) to ``settings.push.url``; returns ``REPORT_KEYS``. ``transport`` replaces the
    HTTP requests (tests), ``sleep`` the waits between retries."""
    begin = time.monotonic()
    url = settings.push.url
    report = new_report(url)
    report['dry_run'] = options.dry_run
    paths.ensure_home(home)
    nonce, taken_at = store.new_lock_nonce(), store.lock_now()   # this run's lock (review M5 remote v3 V3-5)
    try:
        lock, report['warning'] = store.acquire_lock(home, PUSH_LOCK, taken_at, stale_after=PUSH_LOCK_STALE,
                                                     nonce=nonce)
    except store.Locked as exc:
        report.update(exit_code=EXIT_LOCKED, error=str(exc), elapsed=round(time.monotonic() - begin, 3))
        return report
    try:
        return _locked_run(home, url, token, options, report, begin, transport, sleep, (lock, nonce, taken_at))
    finally:
        # whatever happened after the lock was taken (review v2 L-d); a lock another run took over stays
        if not store.release_lock(lock, nonce):
            try:
                left = store.lock_held(lock, nonce) and not store.release_lock(lock, nonce)   # read again: once more
            except OSError:
                left = True                     # unreadable now: it may still be this run's (review M6 v1 M6-4)
            if left:
                hours = PUSH_LOCK_STALE.total_seconds() / 3600
                note = (f'warning: {lock} was not removed (it could not be read just now); if no push runs, '
                        f'delete it, or the next push exits 3 for up to {hours:g} hours')
                report['warning'] = '\n  '.join(text for text in (report['warning'], note) if text)


def _locked_run(home: Path, url: str, token: str, options: Options, report: dict, begin: float,
                transport: Transport | None, sleep: Callable[[float], None],
                held: tuple[Path, str, datetime] | None = None) -> dict:
    logger, handler, log_file = _file_logger(home)
    report['log'] = str(log_file)
    if report['warning']:
        logger.info(report['warning'])
    run = _Run(url, transport or http_transport(token), sleep, logger, report,
               budget=None if options.max_mb is None else options.max_mb * 2**20)
    if held is not None:
        run.lock, run.nonce, run.beat_at = held
    db = None
    try:
        db = store.open_read(home)
        db.execute('BEGIN')                        # one read snapshot: summaries and items agree
        _push(db, run, options)
        if options.require_vectors and (missing := _missing_targets(db, local_model_key(db))):
            raise Stop(EXIT_FAILED, f'--require-vectors: {missing} target chunk texts have no vector here (run index)')
    except Stop as exc:
        report['exit_code'] = exc.exit_code
        report['error'] = str(exc)
        logger.info(f'stopped | {exc}')
    except FileNotFoundError as exc:
        report['exit_code'] = EXIT_USAGE
        report['error'] = f'{exc}; run workmcp scan first'
    finally:
        if db is not None:
            db.close()
        report['elapsed'] = round(time.monotonic() - begin, 3)
        logger.info(f"done | push #{report['push_id']} | exit {report['exit_code']} | batches {report['batches']} | "
                    f"bytes {report['bytes_sent']}")
        logger.removeHandler(handler)
        handler.close()
    if report['exit_code'] == EXIT_OK and report['refused']:
        report['exit_code'] = EXIT_FAILED
    return report


def _push(db, run: _Run, options: Options) -> None:
    report, logger = run.report, run.logger
    model_key = local_model_key(db)
    state = run.get('state', {'model_key': model_key} if model_key else None)
    run.collector = report['collector'] = state['collector']
    if (state['schema_version'], state['chunk_version']) != (store.SCHEMA_VERSION, chunks.CHUNK_VERSION):
        raise Stop(EXIT_USAGE, f"server schema {state['schema_version']} / chunks {state['chunk_version']}, this "
                               f'collector {store.SCHEMA_VERSION} / {chunks.CHUNK_VERSION}: update the server first')
    mine = sync.summary(db, model_key)
    # events: the same set (nothing to send), a prefix the server has (send the rest), or another history
    # (a rebuilt or restored store: the server maps them all again)
    if state['events_sha256'] == sync.events_sha256(db):
        events_after, events_reset = None, False
    elif state['events_sha256'] == sync.events_sha256(db, upto=state['events_max']):
        events_after, events_reset = state['events_max'], False
    else:
        events_after, events_reset = 0, True
    if all(state[key] == mine[key] for key in sync.SUMMARY_KEYS) and events_after is None:
        report['up_to_date'] = report['converged'] = True
        report['server'] = {**mine, 'vectors_missing': None}
        logger.info(f'up to date | {run.url} | {run.collector}')
        return

    remote, after = {}, 0
    while True:
        page = run.get('state', {'full': 1, 'after': after})
        remote.update({row['local_document_id']: row for row in page['documents']})
        if page['next_after'] is None:
            break
        after = page['next_after']
    changed = [row for row in sync.document_summaries(db) if remote.get(row['local_document_id']) != row]
    if options.limit_docs is not None and len(changed) > options.limit_docs:
        changed = changed[:options.limit_docs]
        run.note = f'--limit-docs {options.limit_docs}: the other changed documents go next time'
    counts = report['state']
    counts['documents_changed'] = len(changed)
    for row in changed:
        counts[_kind_of_change(row, remote.get(row['local_document_id']))] += 1
    if options.dry_run:
        if model_key is not None:
            counts['vectors_missing'] = len(_server_missing(run, model_key))
        return

    roots = [{'local_root_id': root_id, 'path': path, 'enabled': enabled}
             for root_id, path, enabled in db.execute('SELECT id, path, enabled FROM source_roots ORDER BY id')]
    begun = run.post('begin', [{'roots': roots, 'counts_expected': {'documents': len(changed)},
                                'started_at': datetime.now().astimezone().isoformat(timespec='microseconds'),
                                'nonce': os.urandom(8).hex(), 'model_key': model_key,
                                'events_reset': events_reset}])
    run.push_id = report['push_id'] = begun['push_id']
    logger.info(f'start | push #{run.push_id} | {run.url} | {run.collector} | documents changed {len(changed)}')

    # documents and versions (no text), then the units of the versions the server needs
    sent_docs, need, paths_by_doc, versions_by_local = set(), [], {}, {}
    items = []
    for row in changed:
        item, path = document_item(db, row['local_document_id'])
        paths_by_doc[item['local_document_id']] = path
        versions_by_local.update({v['local_version_id']: (path, v) for v in item['versions']})
        items.append(item)
    for batch in _batches(items, count=sync.DOCUMENTS_PER_BATCH):
        if run.over_budget():
            break
        answer = run.post('documents', batch)
        report['sent']['documents'] += len(batch)
        report['sent']['versions'] += sum(len(item['versions']) for item in batch)
        refused = {entry['local_document_id']: entry['code'] for entry in answer['refused']}
        for item in batch:
            local = item['local_document_id']
            if local in refused:
                report['refused'].append({'path': paths_by_doc[local], 'code': refused[local]})
                logger.info(f'refused | {paths_by_doc[local]} | {refused[local]}')
                continue
            sent_docs.add(local)
        for local, entry in answer['versions'].items():
            path, version = versions_by_local[int(local)]
            if entry['units'] == 'need':
                need.append(int(local))
            elif entry['units'] == 'copied':
                report['sent']['copied_versions'] += 1
                logger.info(f"copied | {path} | {version['sha256'][:12]} | units {version['unit_count']} "
                            '| on the server, nothing sent')
    # documents the server maps to local ids this store no longer has (a rebuilt or restored store); only after
    # every changed document went out (review v2 L-a): a cut run would forget files it has not sent yet
    complete = run.note is None and not run.over_budget()
    local_ids = {row[0] for row in db.execute('SELECT id FROM documents')}
    stale = [{'local_document_id': local} for local in sorted(set(remote) - local_ids)] if complete else []
    for batch in _batches(stale, count=sync.EVENTS_PER_BATCH):
        report['sent']['forgotten'] += run.post('forget', batch)['forgotten']
    if stale:
        logger.info(f"forgotten | {report['sent']['forgotten']} documents this store no longer has")
    _send_units(db, run, need, versions_by_local)

    # vectors the server lacks and this store has
    if model_key is not None and not run.over_budget():
        missing = _server_missing(run, model_key)
        counts['vectors_missing'] = len(missing)
        vectors = []
        for sha in missing:
            row = db.execute('SELECT dims, vector FROM chunk_vectors WHERE model_key = ? AND text_sha256 = ?',
                             (model_key, sha)).fetchone()
            if row is not None and row[0] == sync.VECTOR_DIMS:
                vectors.append({'text_sha256': sha, 'vector_b64': sync.vector_to_b64(row[1])})
        for at in range(0, len(vectors), sync.VECTORS_PER_BATCH):
            if run.over_budget():
                break
            part = vectors[at:at + sync.VECTORS_PER_BATCH]
            run.post('vectors', [{'model_key': model_key, 'dims': sync.VECTOR_DIMS, 'items': part}])
            report['sent']['vectors'] += len(part)
            report['sent']['vector_bytes'] += len(part) * sync.VECTOR_DIMS * 4
        if vectors:
            logger.info(f"vectors | {report['sent']['vectors']}")

    # document events after the prefix the server has (all of them after a reset), by this store's event ids
    events = [] if events_after is None else [
        {'local_event_id': event_id, 'local_document_id': doc, 'local_version_id': version, 'kind': kind, 'at': at}
        for event_id, doc, version, kind, at in sync.event_rows(db, after=events_after)]
    for batch in _batches(events, count=sync.EVENTS_PER_BATCH):
        if run.over_budget():
            break
        run.post('events', batch)
        report['sent']['events'] += len(batch)

    complete = complete and not run.over_budget()   # the server sweeps what this store does not have
    final = run.post('end', [{'counts': dict(report['sent']), 'bytes_sent': report['bytes_sent'],
                              'complete': complete}])
    report['sent']['forgotten'] += final.get('swept', 0)
    report['server'] = {'documents_sha256': final['documents_sha256'], 'vectors_sha256': final['vectors_sha256'],
                        'vectors_missing': final['vectors_missing_total']}
    report['converged'] = all(final[key] == mine[key] for key in sync.SUMMARY_KEYS)
    if run.stopped or run.note:
        report['error'] = '; '.join(text for text in (run.stopped, run.note) if text)


def _server_missing(run: _Run, model_key: str) -> list[str]:
    missing, after = [], ''
    while True:
        page = run.get('vectors', {'model_key': model_key, **({'after': after} if after else {})})
        missing += page['missing']
        if page['next_after'] is None:
            return missing
        after = page['next_after']


def _send_units(db, run: _Run, need: list[int], versions_by_local: dict) -> None:
    """The units of the versions in ``need``. A version is refused alone and the next one goes on (review M5
    remote v3 V3-6) when it needs more than ``sync.MAX_PARTS`` parts (``too_many_parts``, nothing sent) or the
    server refuses a units batch of it as ``version_too_large`` (a unit longer than the server takes that the
    documents step did not see; the server removes what it holds of the version). A refused batch of several
    short versions refuses them all; a collector that reports ``unit_chars_max`` never sends one."""
    report, logger = run.report, run.logger
    batch, entries, units, chars, size = [], 0, 0, 0, 0
    budget = sync.BATCH_MAX_BYTES - sync.ENVELOPE_RESERVE
    refused: set[int] = set()

    def refuse(local: int, code: str) -> None:
        refused.add(local)
        path, version = versions_by_local[local]
        report['refused'].append({'path': path, 'code': code})
        logger.info(f"refused | {path} | {version['sha256'][:12]} | {code}")

    def flush():
        nonlocal batch, entries, units, chars, size
        if batch:
            try:
                run.post('units', batch)
            except Stop as exc:
                if exc.code != 'version_too_large':
                    raise
                for local in dict.fromkeys(item['local_version_id'] for item in batch):
                    refuse(local, exc.code)
            else:
                report['sent']['units'] += units
                report['sent']['chars'] += chars
        batch, entries, units, chars, size = [], 0, 0, 0, 0

    for local in need:
        if run.over_budget():
            break
        path, version = versions_by_local[local]
        parts = unit_parts(db, local)
        if parts is None:
            refuse(local, TOO_MANY_PARTS)
            continue
        for number, part in enumerate(parts, start=1):
            item = {'local_version_id': local, 'units_sha256': version['units_sha256'], 'part': number,
                    'parts': len(parts), 'units': part}
            item_size = sync.canonical_size(item) + 1
            part_chars = sum(len(unit['text']) for unit in part)
            if batch and (entries + len(part) > sync.UNITS_PER_BATCH or chars + part_chars > sync.UNIT_CHARS_PER_BATCH
                          or size + item_size > budget or len(parts) > 1):
                flush()                     # each part of a split version is a batch of its own (a short step)
            if local in refused:            # the server refused an earlier part of it: no more parts
                break
            batch.append(item)
            entries += len(part)
            units += len({unit['ordinal'] for unit in part}
                         - ({parts[number - 2][-1]['ordinal']} if number > 1 else set()))
            chars += part_chars
            size += item_size
        if len(parts) > 1:
            flush()                         # its last part too: a refusal of it names this version only
        if local not in refused:
            logger.info(f"pushed | {path} | {version['sha256'][:12]} | units {version['unit_count']} "
                        f"| chars {version['text_chars']}")
    flush()


# ---- report ---------------------------------------------------------------------------------------------

def _short(value: str | None) -> str:
    return f'{value[:4]}…' if value else '-'


def report_lines(report: dict) -> list[str]:
    """The stdout report of ``execute`` (design §3.6)."""
    who = f"to {report['url']}" + (f" as {report['collector']}" if report['collector'] else '')
    head = f"push #{report['push_id']} {who}" if report['push_id'] is not None else f'push {who}'
    if report['up_to_date']:
        return [f"{head}  up to date (batches 0, bytes sent 0, state requests {report['state_requests']})",
                *([f"  {report['warning']}"] if report.get('warning') else [])]
    lines = [f"{head}  started {report['started_at']}" + ('  (dry run: nothing sent)' if report['dry_run'] else '')]
    c, s = report['state'], report['sent']
    lines.append(f"  state: documents changed {c['documents_changed']} (added {c['added']} changed {c['changed']} "
                 f"deleted {c['deleted']} restored {c['restored']} reparsed {c['reparsed']})  "
                 f"vectors missing on server {c['vectors_missing']}")
    if not report['dry_run']:
        lines.append(f"  sent: documents {s['documents']}  versions {s['versions']}  units {s['units']:,} "
                     f"({s['chars']:,} chars)  vectors {s['vectors']} ({s['vector_bytes'] // 1024:,} KB)  "
                     f"events {s['events']}  copied on the server {s['copied_versions']}"
                     + (f"  forgotten {s['forgotten']}" if s['forgotten'] else ''))
    lines.append(f"  batches {report['batches']}  bytes sent {report['bytes_sent']:,} (gzip)  refused "
                 f"{len(report['refused'])}  retries {report['retries']}  {report['elapsed']}s   log: {report['log']}")
    lines += [f"  refused: {entry['path']} : {entry['code']}" for entry in report['refused']]
    if report['server']:
        server = report['server']
        lines.append(f"  server now: documents_sha256 {_short(server['documents_sha256'])}  vectors_sha256 "
                     f"{_short(server['vectors_sha256'])}  vectors still missing {server['vectors_missing']}"
                     + ('' if report['converged'] else '  (not yet the same as this collector)'))
    if report['error']:
        lines.append(f"  error: {report['error']}")
    if report.get('warning'):
        lines.append(f"  {report['warning']}")
    return lines


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

"""Operating server: the MCP tools over Streamable HTTP with token authentication, the push endpoints, and
the token commands (design M5 remote §4, appendix B).

``serve --http`` binds loopback only (``127.0.0.1`` or ``::1``; a reverse proxy terminates TLS and forwards
``<http_path>/`` to it). Routes under ``[server] http_path`` (default ``/workmcp``): ``/mcp`` (the fifteen
tools of ``server.build_server``, stateful sessions, JSON responses), ``/push/state``, ``/push/state/vectors``,
``/push/batch`` (``receive``) and ``/healthz``. Every route needs a token: MCP through the SDK's
``TokenVerifier`` (``AuthSettings`` with no resource metadata route, so no unauthenticated route exists),
the custom routes by checking ``Authorization: Bearer`` here, before anything else (an unknown method or
push action answers only after the token); any other path under the prefix is a plain 404 (no redirect
naming a route). Two token kinds: ``mcp`` for clients, ``push`` for one collector
each; the other kind is refused (401). Only a token's SHA-256 and a 6-character prefix are stored
(``tokens``, schema v5); the plain token is printed once by ``token issue``/``rotate`` and never logged.
Request bodies and tokens never reach a log; refusals log the expected kind and the reason only
(``auth refused kind=<mcp|push|any> reason=<...> ip=<X-Forwarded-For or ->``, at most ``REFUSALS_LOGGED``
lines per ``REFUSAL_WINDOW_SECONDS``). Unknown and revoked tokens are logged by the verifier, which the
SDK's authentication middleware runs for every request with a Bearer header, whatever the path; a missing
token or a token of the other kind is logged by the push and health routes (the SDK answers those on the
MCP path with its 401 and no hook, so they are not logged there).

Push endpoints (``sync.ENDPOINTS``, answers in the ``sync.ok``/``sync.error`` envelope, HTTP status from
``sync.ERROR_HTTP_STATUS``): ``GET push/state[?model_key=K]`` -> ``receive.state`` (``K`` as the collector sent
it, None without one: the server's own key would count other collectors' vectors), ``GET push/state?full=1&after=N`` -> ``receive.state_page``,
``GET push/state/vectors?model_key=K[&after=SHA]`` -> ``receive.vectors_page``, ``POST push/batch`` (gzip JSON
body, at most ``[server] push_max_mb`` MiB before and after decompression, else 413) -> ``receive.apply``.
The three state functions return their result (or raise ``sync.SyncError``); ``apply`` returns the envelope.
They run on the push connection (``ServerState.push_db``); tokens are read on the call log connection.

Boundaries (``tests/test_network_boundary.py``): the only module that receives over the network, and only
through the SDK's ``run(transport="streamable-http", host=<loopback literal>)``; no ``uvicorn``, ``socket``,
``ssl`` or ``http`` import, no thread, no process. ``signal`` only to turn SIGTERM (and SIGBREAK on Windows)
into the clean stop SIGINT already is.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import config, paths, receive, search, server, store, sync

LOG = logging.getLogger('workmcp.remote')
REMOTE_INSTRUCTIONS = ('이 서버는 회사 PC 수집기가 올린 저장 텍스트(단위)로 답한다; 원본 파일은 회사 PC에만 있다. '
                       '응답의 delivered_sources가 이번 응답으로 서버를 떠난 조각의 출처 전부다. '
                       '검색 결과 없음 ≠ 회사에 그 정보가 없음.')
EXIT_OK, EXIT_FAILED, EXIT_HOME, EXIT_LOCKED, EXIT_USAGE, EXIT_PORT_IN_USE = 0, 1, 2, 3, 4, 5
TOKEN_KINDS = ('mcp', 'push')
TOKEN_BYTES = 32                  # secrets.token_urlsafe(32): 43 characters, 256 bits
TOKEN_PREFIX_CHARS = 6
TOKEN_NAME_MAX = 64
MCP_SCOPE = 'mcp'
MAX_SESSIONS = 16
SESSION_IDLE_SECONDS = 1800
REFUSAL_REASONS = ('missing', 'unknown', 'revoked', 'wrong_kind')
REFUSAL_WINDOW_SECONDS = 300.0
REFUSALS_LOGGED = 20              # per window; one more line says the rest are not logged
SERVER_LOG_LEVEL = 'WARNING'      # the SDK hands it to uvicorn: no access log (design §4.1)
PRELOADED_IDLE_SECONDS = 10**9    # [server] model_preload: loaded at start, never released
# token list rows and the issue/rotate result (the plain token only in the latter, printed once).
TOKEN_LIST_KEYS = ('id', 'kind', 'name', 'prefix', 'collector', 'created_at', 'revoked_at', 'last_used_at')
ISSUED_KEYS = ('id', 'kind', 'name', 'prefix', 'collector', 'token')
COLLECTOR_LIST_KEYS = ('name', 'created_at', 'last_push_at', 'retired_at', 'live_tokens', 'roots', 'documents')
ROUTE_NAMES = ('mcp', 'push', 'healthz')
ROUTE_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']   # every method meets the token check first
_IP_TEXT = re.compile(r'[0-9A-Fa-f.:, ]{1,64}')


def routes(settings: config.ServerSettings) -> dict[str, str]:
    """Paths under ``http_path``: ``mcp`` (the Streamable HTTP endpoint), ``push`` (prefix of the push
    endpoints, ``sync.ENDPOINTS`` below it), ``healthz``."""
    prefix = settings.http_path
    return {'mcp': f'{prefix}/mcp', 'push': f'{prefix}/push/', 'healthz': f'{prefix}/healthz'}


def new_token() -> tuple[str, str, str]:
    """``(plain token, sha256 hex, prefix)`` of a new random token; only the last two are stored."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, token_sha256(token), token[:TOKEN_PREFIX_CHARS]


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def verify_token_sha(stored_sha256: str, token: str) -> bool:
    """``token`` hashes to ``stored_sha256`` (constant-time comparison of the hex digests)."""
    return hmac.compare_digest(stored_sha256.encode('ascii'), token_sha256(token).encode('ascii'))


def _name_problem(name) -> str | None:
    if not isinstance(name, str) or not name or len(name) > TOKEN_NAME_MAX \
            or any(ch.isspace() or not ch.isprintable() for ch in name):
        return f'must be 1-{TOKEN_NAME_MAX} printable characters without spaces'
    return None


def check_token_args(args) -> str | None:
    """The first rule the ``token`` command's arguments break (exit 4), else None. No store access."""
    if args.token_command == 'issue':
        if args.kind not in TOKEN_KINDS:
            return f'--kind must be one of {", ".join(TOKEN_KINDS)}'
        if _name_problem(args.name):
            return f'--name {_name_problem(args.name)}'
        if args.collector is not None and args.kind != 'push':
            return '--collector belongs to --kind push only'
        if args.collector is not None and _name_problem(args.collector):
            return f'--collector {_name_problem(args.collector)}'
    elif args.token_command in ('revoke', 'rotate') and args.id < 1:
        return 'ID must be a positive integer (see workmcp token list)'
    return None


# ---- token store --------------------------------------------------------------------------------------------

class TokenError(Exception):
    """A token or collector command that cannot be done (exit 4); the message names ids and names only."""


def _rows(db, where: str = '', args: tuple = ()) -> list[dict]:
    sql = ('SELECT t.id, t.kind, t.name, t.prefix, c.name, t.created_at, t.revoked_at, t.last_used_at '
           f'FROM tokens t LEFT JOIN collectors c ON c.id = t.collector_id {where} ORDER BY t.id')
    return [dict(zip(TOKEN_LIST_KEYS, row)) for row in db.execute(sql, args)]


def _one(db, token_id: int) -> dict:
    rows = _rows(db, 'WHERE t.id = ?', (token_id,))
    if not rows:
        raise TokenError(f'no token with id {token_id} (see workmcp token list)')
    return rows[0]


def _transaction(db, work):
    """``work()`` inside one ``BEGIN IMMEDIATE`` transaction: committed, or rolled back and re-raised."""
    store.begin_immediate(db)
    try:
        result = work()
        store.commit(db)
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    return result


def _collector_row(db, name: str) -> dict | None:
    row = db.execute('SELECT id, name, retired_at FROM collectors WHERE name = ?', (name,)).fetchone()
    return dict(zip(receive.COLLECTOR_KEYS, row)) if row else None


def _insert_token(db, kind: str, name: str, collector_id: int | None, now: str) -> tuple[int, str]:
    token, digest, prefix = new_token()
    cursor = db.execute('INSERT INTO tokens(kind, name, token_sha256, prefix, collector_id, created_at) '
                        'VALUES (?, ?, ?, ?, ?, ?)', (kind, name, digest, prefix, collector_id, now))
    return cursor.lastrowid, token


def _issued(db, token_id: int, token: str) -> dict:
    row = _one(db, token_id)
    return {**{key: row[key] for key in ISSUED_KEYS[:-1]}, 'token': token}


def issue_token(db, kind: str, name: str, *, collector: str | None = None, now: str | None = None) -> dict:
    """New token row (a push token also creates or reuses the collector ``collector or name``); returns
    ``ISSUED_KEYS`` with the plain token, which is not stored anywhere."""
    if kind not in TOKEN_KINDS:
        raise TokenError(f'kind must be one of {", ".join(TOKEN_KINDS)}')
    for label, value in (('name', name), ('collector', collector)):
        if (label == 'name' or value is not None) and (problem := _name_problem(value)):
            raise TokenError(f'{label} {problem}')
    if collector is not None and kind != 'push':
        raise TokenError('a collector belongs to a push token only')
    now = now or store.now()

    def work():
        collector_id = None
        if kind == 'push':
            owner = _collector_row(db, collector or name)
            if owner is None:
                collector_id = db.execute('INSERT INTO collectors(name, created_at) VALUES (?, ?)',
                                          (collector or name, now)).lastrowid
            elif owner['retired_at'] is not None:
                raise TokenError(f"collector {owner['name']!r} is retired; it takes no new token")
            else:
                collector_id = owner['id']
        return _insert_token(db, kind, name, collector_id, now)

    token_id, token = _transaction(db, work)
    return _issued(db, token_id, token)


def list_tokens(db) -> list[dict]:
    """Every token row as ``TOKEN_LIST_KEYS`` (no plain token, no hash)."""
    return _rows(db)


def revoke_token(db, token_id: int, *, now: str | None = None) -> dict:
    """Set ``revoked_at`` once (effective at once: lookups read the store); returns ``TOKEN_LIST_KEYS``. A
    token already revoked stays as it is."""
    now = now or store.now()

    def work():
        if _one(db, token_id)['revoked_at'] is None:
            db.execute('UPDATE tokens SET revoked_at = ? WHERE id = ?', (now, token_id))

    _transaction(db, work)
    return _one(db, token_id)


def rotate_token(db, token_id: int, *, now: str | None = None) -> dict:
    """Issue a token of the same kind, name and collector, then revoke the old one; ``ISSUED_KEYS``."""
    now = now or store.now()

    def work():
        old = _one(db, token_id)
        if old['revoked_at'] is not None:
            raise TokenError(f'token {token_id} is revoked; issue a new one instead')
        kind, name, collector_id = db.execute('SELECT kind, name, collector_id FROM tokens WHERE id = ?',
                                              (token_id,)).fetchone()
        if collector_id is not None and db.execute('SELECT retired_at FROM collectors WHERE id = ?',
                                                   (collector_id,)).fetchone()[0] is not None:
            raise TokenError(f"collector {old['collector']!r} is retired; it takes no new token")
        new = _insert_token(db, kind, name, collector_id, now)
        db.execute('UPDATE tokens SET revoked_at = ? WHERE id = ?', (now, token_id))
        return new

    new_id, token = _transaction(db, work)
    return _issued(db, new_id, token)


def check_token(db, token: str | None, kind: str | None, *, now: str | None = None) -> tuple[dict | None, str | None]:
    """``(row, None)`` for a live token of ``kind`` (None: either kind) and its ``last_used_at`` brought to
    this minute, else ``(None, reason)`` with a ``REFUSAL_REASONS`` entry. Reads the store on every call, so a
    revocation holds at once; the comparison of the hex digests is constant-time."""
    if not token:
        return None, 'missing'
    digest = token_sha256(token)
    row = db.execute('SELECT id, kind, token_sha256, revoked_at, last_used_at FROM tokens WHERE token_sha256 = ?',
                     (digest,)).fetchone()
    if row is None or not hmac.compare_digest(row[2].encode('ascii'), digest.encode('ascii')):
        return None, 'unknown'
    token_id, token_kind, _, revoked_at, last_used_at = row
    if revoked_at is not None:
        return None, 'revoked'
    if kind is not None and token_kind != kind:
        return None, 'wrong_kind'
    now = now or store.now()
    if last_used_at is None or last_used_at[:16] != now[:16]:   # minutes only: no write per request
        try:
            db.execute('UPDATE tokens SET last_used_at = ? WHERE id = ?', (now, token_id))
        except sqlite3.OperationalError:  # store busy: the use time waits for the next request
            pass
    return _one(db, token_id), None


def lookup_token(db, token: str, kind: str) -> dict | None:
    """The live token row of ``kind`` for ``token`` (``TOKEN_LIST_KEYS``), else None (``check_token`` names
    the refusal reason)."""
    return check_token(db, token, kind)[0]


def collector_of(db, token_row: dict) -> dict | None:
    """The collector of a push token row (``receive.COLLECTOR_KEYS``)."""
    row = db.execute('SELECT c.id, c.name, c.retired_at FROM tokens t JOIN collectors c ON c.id = t.collector_id '
                     'WHERE t.id = ?', (token_row['id'],)).fetchone()
    return dict(zip(receive.COLLECTOR_KEYS, row)) if row else None


def retire_collector(db, name: str, *, now: str | None = None) -> dict:
    """``collectors retire NAME``: revoke its push tokens and set ``retired_at``; its data stays. Returns
    ``{name, retired_at, revoked}`` (the ids revoked now); a retired collector stays as it is."""
    now = now or store.now()

    def work():
        owner = _collector_row(db, name)
        if owner is None:
            raise TokenError(f'no collector named {name!r} (see workmcp collectors)')
        live = [row[0] for row in db.execute('SELECT id FROM tokens WHERE collector_id = ? AND revoked_at IS NULL '
                                             'ORDER BY id', (owner['id'],))]
        for token_id in live:
            db.execute('UPDATE tokens SET revoked_at = ? WHERE id = ?', (now, token_id))
        if owner['retired_at'] is None:
            db.execute('UPDATE collectors SET retired_at = ? WHERE id = ?', (now, owner['id']))
        return live

    revoked = _transaction(db, work)
    return {'name': name, 'retired_at': _collector_row(db, name)['retired_at'], 'revoked': revoked}


def list_collectors(db) -> list[dict]:
    """``COLLECTOR_LIST_KEYS`` per collector: dates, live push tokens, roots and documents (counts only)."""
    counts = {row['name']: row for row in store.collector_status(db)}
    rows = db.execute('SELECT c.name, c.created_at, c.last_push_at, c.retired_at, '
                      '(SELECT COUNT(*) FROM tokens t WHERE t.collector_id = c.id AND t.revoked_at IS NULL) '
                      'FROM collectors c ORDER BY c.id').fetchall()
    return [dict(zip(COLLECTOR_LIST_KEYS, (*row, counts[row[0]]['roots'], counts[row[0]]['documents'])))
            for row in rows]


def token_counts(db) -> dict:
    """``{mcp, push}``: live tokens of each kind (``doctor``, the start line)."""
    live = dict(db.execute('SELECT kind, COUNT(*) FROM tokens WHERE revoked_at IS NULL GROUP BY kind'))
    return {kind: live.get(kind, 0) for kind in TOKEN_KINDS}


# ---- refusal log ------------------------------------------------------------------------------------------

class RefusalLog:
    """``auth refused kind=... reason=... ip=...`` lines on stderr (no token, no prefix), at most
    ``REFUSALS_LOGGED`` per ``REFUSAL_WINDOW_SECONDS`` and one line when that is passed."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.times: list[float] = []

    def note(self, kind: str, reason: str, ip: str | None = None) -> None:
        now = self.clock()
        self.times = [at for at in self.times if now - at < REFUSAL_WINDOW_SECONDS]
        self.times.append(now)
        if len(self.times) <= REFUSALS_LOGGED:
            LOG.warning('auth refused kind=%s reason=%s ip=%s', kind, reason, ip or '-')
        elif len(self.times) == REFUSALS_LOGGED + 1:
            LOG.warning('auth refused: more than %d refusals in %d s; further ones are not logged for now',
                        REFUSALS_LOGGED, int(REFUSAL_WINDOW_SECONDS))


def client_ip(request: Request) -> str | None:
    """The proxy's ``X-Forwarded-For`` when it looks like addresses, else None (logged as ``-``)."""
    value = request.headers.get('x-forwarded-for', '')
    return value if _IP_TEXT.fullmatch(value) else None


class TokenVerifier:
    """The SDK's ``TokenVerifier`` protocol over the ``tokens`` table (``mcp`` tokens only). The SDK runs it
    for every request with a Bearer header, so it logs the unknown and revoked tokens of every route."""

    def __init__(self, db, refusals: RefusalLog | None = None):
        self.db = db
        self.refusals = refusals

    async def verify_token(self, token: str) -> AccessToken | None:
        """``AccessToken(token='', client_id=<token name>, scopes=['mcp'], subject=token-<id>)`` for a live mcp
        token, else None. The subject binds a session to the token that opened it (a rotated token with the
        same name does not take over the old one's sessions)."""
        try:
            row, reason = check_token(self.db, token, 'mcp')
        except sqlite3.Error as exc:
            LOG.error('token lookup failed (%s)', type(exc).__name__)
            return None
        if row is None:
            if reason in ('unknown', 'revoked') and self.refusals is not None:
                self.refusals.note('any', reason)
            return None
        return AccessToken(token='', client_id=row['name'], scopes=[MCP_SCOPE], subject=f"token-{row['id']}")


# ---- server -----------------------------------------------------------------------------------------------

def _origin(settings: config.ServerSettings) -> str:
    host, port = config.parse_http(settings.http)
    return f"http://{'[::1]' if host == '::1' else host}:{port}"


def auth_settings(settings: config.ServerSettings) -> AuthSettings:
    """``AuthSettings`` with ``required_scopes=['mcp']`` and ``resource_server_url=None`` (no metadata route).
    ``issuer_url`` is required by the SDK but unused without an authorization server: the loopback origin."""
    return AuthSettings(issuer_url=f'{_origin(settings)}{settings.http_path}', resource_server_url=None,
                        required_scopes=[MCP_SCOPE])


def transport_security(settings: config.ServerSettings) -> TransportSecuritySettings:
    """DNS rebinding protection: ``allowed_hosts`` plus ``127.0.0.1:*``/``localhost:*``/``[::1]:*``, no
    browser origin (a request with an ``Origin`` header is refused, 403)."""
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=[*settings.allowed_hosts, '127.0.0.1:*', 'localhost:*', '[::1]:*'],
                                     allowed_origins=[])


@dataclass
class RemoteContext:
    """What the custom routes need besides the ``ServerState`` (``ServerState.remote``)."""
    settings: config.Config
    refusals: RefusalLog = field(default_factory=RefusalLog)
    guard: TransportSecurityMiddleware | None = None

    @property
    def max_bytes(self) -> int:
        return self.settings.server.push_max_mb * 2**20


class StoreLockedError(Exception):
    """A migration was needed and another process held the store (``serve --http`` exit 3)."""


def open_state(home: Path, settings: config.Config) -> server.ServerState:
    """The stores (call log, knowledge and push write connections; ``StoreLockedError`` when a migration meets
    a lock, ``RuntimeError`` from ``store`` when the schema checksum changed) and the server state with
    ``role='server'``. ``[server] model_preload`` loads the model now and never releases it."""
    try:
        write_db = store.open_home(home)
    except sqlite3.OperationalError as exc:
        raise StoreLockedError(str(exc)) from None
    know_db = store.connect(paths.db_path(home))    # migrated by write_db: no lock needed
    push_db = store.connect(paths.db_path(home))    # busy timeout 5000 ms (store.connect)
    engine = None
    if settings.server.model_preload:
        engine = search.Engine(home, replace(settings.search, model_idle_seconds=PRELOADED_IDLE_SECONDS))
        engine.embedder()   # a failure is recorded once and searches stay lexical (design M2 §3.4)
    state = server.make_state(home, engine=engine, write_db=write_db, know_db=know_db, settings=settings)
    state.role, state.push_db = 'server', push_db
    return state


def close_state(state: server.ServerState) -> None:
    server.close_log(state)
    for db in (state.push_db, state.know_db, state.write_db):
        if db is not None:
            db.close()


def build_remote(home: Path, state: server.ServerState, settings: config.Config):
    """``server.build_server`` with ``REMOTE_INSTRUCTIONS``, ``auth_settings`` and a ``TokenVerifier``, plus
    the custom routes ``push_endpoint`` and ``healthz_endpoint`` under ``routes``."""
    context = RemoteContext(settings=settings, guard=TransportSecurityMiddleware(transport_security(settings.server)))
    state.remote = context
    built = server.build_server(home, state=state, instructions=REMOTE_INSTRUCTIONS,
                                auth=auth_settings(settings.server),
                                token_verifier=TokenVerifier(state.write_db, context.refusals),
                                log_level=SERVER_LOG_LEVEL)
    paths_ = routes(settings.server)

    @built.custom_route(paths_['push'] + '{action:path}', methods=ROUTE_METHODS)
    async def push_route(request: Request) -> Response:
        return await push_endpoint(state, request)

    @built.custom_route(paths_['healthz'], methods=ROUTE_METHODS)
    async def healthz_route(request: Request) -> Response:
        return await healthz_endpoint(state, request)

    # Last: any other path under the prefix is 404, so the router never answers a slash variant of a route
    # (``/mcp/``, ``/push``, the prefix itself) with an unauthenticated redirect naming it.
    @built.custom_route(settings.server.http_path + '{rest:path}', methods=ROUTE_METHODS)
    async def not_found_route(request: Request) -> Response:
        return Response('Not Found', status_code=404)

    return built


def bearer(request: Request) -> str | None:
    """The token of ``Authorization: Bearer <token>`` (the SDK's parsing), else None."""
    header = request.headers.get('authorization')
    if not header or not header.lower().startswith('bearer '):
        return None
    return header[7:]


def _unauthorized(code: str) -> Response:
    return JSONResponse(sync.error(code, 'a valid push token is required'), status_code=401,
                        headers={'WWW-Authenticate': 'Bearer error="invalid_token"'})


def _answer(envelope: dict) -> Response:
    status = 200 if envelope.get('ok') else sync.ERROR_HTTP_STATUS.get(envelope['error']['code'], 400)
    return JSONResponse(envelope, status_code=status)


def _query(request: Request, name: str, pattern: str) -> str | None:
    value = request.query_params.get(name)
    if value is not None and not re.fullmatch(pattern, value):
        raise sync.SyncError('bad_batch', f'query {name} is malformed')
    return value


async def _body(request: Request, limit: int) -> bytes:
    """The request body, refused (``batch_too_large``) once it passes ``limit`` bytes."""
    declared = request.headers.get('content-length')
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise sync.SyncError('batch_too_large', f'over {limit} bytes')
    parts, size = [], 0
    async for part in request.stream():
        size += len(part)
        if size > limit:
            raise sync.SyncError('batch_too_large', f'over {limit} bytes')
        parts.append(part)
    return b''.join(parts)


async def push_endpoint(state: server.ServerState, request: Request) -> Response:
    """``<push>/*``: Bearer push token (401/403), Host check (421), body size (413), ``sync.ungzip_json`` ->
    ``receive``; the answer envelope with the status of its error code."""
    context: RemoteContext = state.remote
    ip = client_ip(request)
    row, reason = check_token(state.write_db, bearer(request), 'push')
    if row is None:
        if reason in ('missing', 'wrong_kind'):      # unknown and revoked: logged by the verifier
            context.refusals.note('push', reason, ip)
        return _unauthorized('wrong_token_kind' if reason == 'wrong_kind' else 'invalid_token')
    collector = collector_of(state.write_db, row)
    if collector['retired_at'] is not None:
        context.refusals.note('push', 'collector_retired', ip)
        return _answer(sync.error('collector_retired', f"collector {collector['name']} is retired"))
    if (refused := await context.guard.validate_request(request)) is not None:
        return refused
    action, method = request.path_params['action'], request.method
    known = {'push/state': 'GET', 'push/state/vectors': 'GET', 'push/batch': 'POST'}
    endpoint = f'push/{action}'
    if endpoint not in known:
        return Response('Not Found', status_code=404)
    if method != known[endpoint]:
        return Response('Method Not Allowed', status_code=405, headers={'Allow': known[endpoint]})
    db = state.push_db
    try:
        if endpoint == sync.ENDPOINTS['batch']:
            raw = await _body(request, context.max_bytes)
            body = sync.ungzip_json(raw, context.max_bytes)
            return _answer(receive.apply(db, collector, body, raw_bytes=len(raw)))
        if endpoint == sync.ENDPOINTS['vectors']:
            model_key = _query(request, 'model_key', '[0-9a-f]{64}')
            if model_key is None:
                raise sync.SyncError('bad_batch', 'query model_key is required')
            after = _query(request, 'after', '[0-9a-f]{64}') or ''
            return _answer(sync.ok(receive.vectors_page(db, collector, model_key=model_key, after=after)))
        if _query(request, 'full', '1') == '1':
            after = int(_query(request, 'after', '[0-9]{1,18}') or 0)
            return _answer(sync.ok(receive.state_page(db, collector, after=after)))
        model_key = _query(request, 'model_key', '[0-9a-f]{64}')
        return _answer(sync.ok(receive.state(db, collector, model_key=model_key)))
    except sync.SyncError as exc:
        return _answer(sync.error(exc.code, exc.message))
    except store.Busy:
        return _answer(sync.error('store_busy', 'the store is busy; try again'))


async def healthz_endpoint(state: server.ServerState, request: Request) -> Response:
    """``<healthz>``: any live token -> 200 ``{"ok":true}``; else 401."""
    context: RemoteContext = state.remote
    row, reason = check_token(state.write_db, bearer(request), None)
    if row is None:
        if reason == 'missing':
            context.refusals.note('any', reason, client_ip(request))
        return JSONResponse({'error': 'invalid_token'}, status_code=401,
                            headers={'WWW-Authenticate': 'Bearer error="invalid_token"'})
    if (refused := await context.guard.validate_request(request)) is not None:
        return refused
    if request.method not in ('GET', 'HEAD'):
        return Response('Method Not Allowed', status_code=405, headers={'Allow': 'GET'})
    return JSONResponse({'ok': True})


def _stop_on_terminate() -> dict:
    """SIGTERM (and SIGBREAK on Windows) raise ``KeyboardInterrupt`` like SIGINT, so a service stop ends
    with exit 0 (uvicorn shuts down first and then re-raises the signal it caught). Returns the old handlers."""
    signals = [signal.SIGTERM, *([signal.SIGBREAK] if sys.platform == 'win32' else [])]
    return {sig: signal.signal(sig, signal.default_int_handler) for sig in signals}


def main(home: Path, host: str, port: int, settings: config.Config) -> int:
    """``serve --http HOST:PORT`` (``config.parse_http`` already allowed only loopback): exit 0 on SIGTERM or
    SIGINT, 2 data home (a changed schema checksum), 3 store locked while a migration is needed, 4 settings,
    5 port in use (or another failure to listen)."""
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format=server.LOG_FORMAT)
    paths.ensure_home(home)
    print(f'workmcp: {paths.virtualization_line(home, paths.virtualization_probe(home, os.environ))}',
          file=sys.stderr)
    try:
        state = open_state(home, settings)
    except StoreLockedError as exc:
        print(f'workmcp: cannot open the store ({exc}): another process holds it while it needs a schema update; '
              f'start the server again when that process ends', file=sys.stderr)
        return EXIT_LOCKED
    except RuntimeError as exc:
        if not store.schema_changed(exc):
            raise
        print(store.DEV_SCHEMA_HINT.format(db=paths.db_path(home)), file=sys.stderr)
        return EXIT_HOME
    previous = _stop_on_terminate()
    try:
        built = build_remote(home, state, settings)
        security = transport_security(settings.server)
        path = routes(settings.server)['mcp']
        live = token_counts(state.write_db)
        print(f"workmcp: serving {'[::1]' if host == '::1' else host}:{port}{path} (loopback only; live tokens "
              f"mcp {live['mcp']}, push {live['push']})", file=sys.stderr)
        if not live['mcp']:
            print('workmcp: no live mcp token: issue one with workmcp token issue --kind mcp --name NAME',
                  file=sys.stderr)
        try:
            if host == '::1':
                built.run(transport='streamable-http', host='::1', port=port, streamable_http_path=path,
                          json_response=True, stateless_http=False, max_sessions=MAX_SESSIONS,
                          session_idle_timeout=SESSION_IDLE_SECONDS, transport_security=security)
            else:
                built.run(transport='streamable-http', host='127.0.0.1', port=port, streamable_http_path=path,
                          json_response=True, stateless_http=False, max_sessions=MAX_SESSIONS,
                          session_idle_timeout=SESSION_IDLE_SECONDS, transport_security=security)
        except KeyboardInterrupt:
            pass
        except SystemExit:   # uvicorn could not start: the bind failed (its error line is above)
            print(f'workmcp: cannot listen on {host}:{port} (port in use?)', file=sys.stderr)
            return EXIT_PORT_IN_USE
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        close_state(state)
    return EXIT_OK


# ---- commands --------------------------------------------------------------------------------------------

def _command_store():
    """``(store, None)`` of the data home, or ``(None, exit code)`` after printing why."""
    try:
        home = paths.resolve_home()
    except paths.DataPathError as exc:
        print(exc, file=sys.stderr)
        return None, EXIT_HOME
    paths.ensure_home(home)
    try:
        return store.open_home(home), None
    except sqlite3.OperationalError as exc:
        print(f'workmcp: cannot open the store ({exc})', file=sys.stderr)
        return None, EXIT_LOCKED
    except RuntimeError as exc:
        if not store.schema_changed(exc):
            raise
        print(store.DEV_SCHEMA_HINT.format(db=paths.db_path(home)), file=sys.stderr)
        return None, EXIT_HOME


def token_line(row: dict) -> str:
    return (f"{row['id']:>4}  {row['kind']:<4}  {row['name']}  prefix {row['prefix']}  "
            f"collector {row['collector'] or '-'}  created {row['created_at']}  "
            f"revoked {row['revoked_at'] or '-'}  last used {row['last_used_at'] or '-'}")


def _print_issued(issued: dict, verb: str) -> None:
    print(f"{verb} token {issued['id']}: kind {issued['kind']}  name {issued['name']}  prefix {issued['prefix']}  "
          f"collector {issued['collector'] or '-'}")
    print(f"workmcp token (shown once): {issued['token']}")


def token_command(args) -> int:
    """``workmcp token issue|list|revoke|rotate`` (exit 0, 2 data home, 3 store locked, 4 arguments). The plain
    token goes to stdout once, from ``issue`` and ``rotate``; nothing else prints it."""
    if problem := check_token_args(args):
        print(problem, file=sys.stderr)
        return EXIT_USAGE
    db, code = _command_store()
    if code:
        return code
    try:
        if args.token_command == 'issue':
            _print_issued(issue_token(db, args.kind, args.name, collector=args.collector), 'issued')
        elif args.token_command == 'list':
            rows = list_tokens(db)
            print('\n'.join(token_line(row) for row in rows) if rows else 'no tokens')
        elif args.token_command == 'revoke':
            before = _one(db, args.id)['revoked_at']
            row = revoke_token(db, args.id)
            print(('already revoked: ' if before else 'revoked: ') + token_line(row))
        else:
            _print_issued(rotate_token(db, args.id), f'rotated token {args.id} (revoked) ->')
    except (TokenError, store.Busy) as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE
    finally:
        db.close()
    return EXIT_OK


def collectors_command(args) -> int:
    """``workmcp collectors [retire NAME]`` (exit 0, 2, 3, 4)."""
    if args.collectors_command == 'retire' and (problem := _name_problem(args.name)):
        print(f'NAME {problem}', file=sys.stderr)
        return EXIT_USAGE
    db, code = _command_store()
    if code:
        return code
    try:
        if args.collectors_command == 'retire':
            result = retire_collector(db, args.name)
            revoked = ', '.join(map(str, result['revoked'])) or 'none'
            print(f"retired collector {result['name']} at {result['retired_at']}; push tokens revoked now: {revoked}; "
                  f'its data stays')
        else:
            rows = list_collectors(db)
            print('\n'.join(f"{row['name']}  created {row['created_at']}  last push {row['last_push_at'] or '-'}  "
                            f"retired {row['retired_at'] or '-'}  live tokens {row['live_tokens']}  "
                            f"roots {row['roots']}  documents {row['documents']}" for row in rows)
                  if rows else 'no collectors')
    except (TokenError, store.Busy) as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE
    finally:
        db.close()
    return EXIT_OK

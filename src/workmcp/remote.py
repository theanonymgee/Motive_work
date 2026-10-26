"""Operating server: the MCP tools over Streamable HTTP with token authentication, the push endpoints, and
the token commands (design M5 remote §4, appendix B).

``serve --http`` binds loopback only (``127.0.0.1`` or ``::1``; a reverse proxy terminates TLS and forwards
``<http_path>/`` to it). Routes under ``[server] http_path`` (default ``/workmcp``): ``/mcp`` (the fifteen
tools of ``server.build_server``, stateful sessions, JSON responses), ``/push/state``, ``/push/state/vectors``,
``/push/batch`` (``receive``) and ``/healthz``. Every route needs a token: MCP through the SDK's
``TokenVerifier`` (``AuthSettings`` with no resource metadata route, so no unauthenticated route exists),
the custom routes by checking ``Authorization: Bearer`` here. Two token kinds: ``mcp`` for clients, ``push``
for one collector each; the other kind is refused (401). Only a token's SHA-256 and a 6-character prefix
are stored (``tokens``, schema v5); the plain token is printed once by ``token issue``/``rotate`` and never
logged. Request bodies and tokens never reach a log; refusals log the kind and reason only.

Boundaries (``tests/test_network_boundary.py``): the only module that receives over the network, and only
through the SDK's ``run(transport="streamable-http", host=<loopback literal>)``; no ``uvicorn``, ``socket``,
``ssl`` or ``http`` import, no thread, no process.

Stage 0 (design M5 remote §11.1) fixes the names, the token helpers and the route table; the server, the
token store functions and the commands come in stage 1.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sys
from pathlib import Path

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import Response

from . import config, server

LOG = logging.getLogger('workmcp.remote')
STAGE1 = 'workmcp operating server: not implemented yet (M5 stage 1)'
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
# token list rows and the issue/rotate result (the plain token only in the latter, printed once).
TOKEN_LIST_KEYS = ('id', 'kind', 'name', 'prefix', 'collector', 'created_at', 'revoked_at', 'last_used_at')
ISSUED_KEYS = ('id', 'kind', 'name', 'prefix', 'collector', 'token')
ROUTE_NAMES = ('mcp', 'push', 'healthz')


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


def check_token_args(args) -> str | None:
    """The first rule the ``token`` command's arguments break (exit 4), else None. No store access."""
    if args.token_command == 'issue':
        if args.kind not in TOKEN_KINDS:
            return f'--kind must be one of {", ".join(TOKEN_KINDS)}'
        if not args.name or len(args.name) > TOKEN_NAME_MAX or any(ch.isspace() or not ch.isprintable()
                                                                   for ch in args.name):
            return f'--name must be 1-{TOKEN_NAME_MAX} printable characters without spaces'
        if args.collector is not None and args.kind != 'push':
            return '--collector belongs to --kind push only'
    elif args.token_command in ('revoke', 'rotate') and args.id < 1:
        return 'ID must be a positive integer (see workmcp token list)'
    return None


# ---- token store (stage 1) -------------------------------------------------------------------------------

def issue_token(db, kind: str, name: str, *, collector: str | None = None, now: str | None = None) -> dict:
    """New token row (a push token also creates or reuses the collector ``collector or name``); returns
    ``ISSUED_KEYS`` with the plain token, which is not stored anywhere."""
    raise NotImplementedError(STAGE1)


def list_tokens(db) -> list[dict]:
    """Every token row as ``TOKEN_LIST_KEYS`` (no plain token, no hash)."""
    raise NotImplementedError(STAGE1)


def revoke_token(db, token_id: int, *, now: str | None = None) -> dict:
    """Set ``revoked_at`` once (effective at once: lookups read the store); returns ``TOKEN_LIST_KEYS``."""
    raise NotImplementedError(STAGE1)


def rotate_token(db, token_id: int, *, now: str | None = None) -> dict:
    """Issue a token of the same kind, name and collector, then revoke the old one; ``ISSUED_KEYS``."""
    raise NotImplementedError(STAGE1)


def lookup_token(db, token: str, kind: str) -> dict | None:
    """The live token row of ``kind`` for ``token`` (``TOKEN_LIST_KEYS``), else None; the refusal reason
    (``REFUSAL_REASONS``) goes to the log without the token."""
    raise NotImplementedError(STAGE1)


def retire_collector(db, name: str, *, now: str | None = None) -> dict:
    """``collectors retire NAME``: revoke its push tokens and set ``retired_at``; its data stays."""
    raise NotImplementedError(STAGE1)


class TokenVerifier:
    """The SDK's ``TokenVerifier`` protocol over the ``tokens`` table (``mcp`` tokens only)."""

    def __init__(self, db):
        self.db = db

    async def verify_token(self, token: str) -> AccessToken | None:
        """``AccessToken(token='', client_id=<token name>, scopes=['mcp'])`` for a live mcp token, else None."""
        raise NotImplementedError(STAGE1)


# ---- server (stage 1) -------------------------------------------------------------------------------------

def auth_settings(settings: config.ServerSettings) -> AuthSettings:
    """``AuthSettings`` with ``required_scopes=['mcp']`` and ``resource_server_url=None`` (no metadata route)."""
    raise NotImplementedError(STAGE1)


def transport_security(settings: config.ServerSettings) -> TransportSecuritySettings:
    """DNS rebinding protection: ``allowed_hosts`` plus ``127.0.0.1:*``/``localhost:*``, no browser origin."""
    raise NotImplementedError(STAGE1)


def open_state(home: Path, settings: config.Config) -> server.ServerState:
    """The stores (call log, knowledge and push write connections; exit 3 when a migration meets a lock) and
    the server state with ``role='server'``."""
    raise NotImplementedError(STAGE1)


def build_remote(home: Path, state: server.ServerState, settings: config.Config):
    """``server.build_server`` with ``REMOTE_INSTRUCTIONS``, ``auth_settings`` and a ``TokenVerifier``, plus
    the custom routes ``push_endpoint`` and ``healthz_endpoint`` under ``routes``."""
    raise NotImplementedError(STAGE1)


async def push_endpoint(state: server.ServerState, request: Request) -> Response:
    """``<push>/*``: Bearer push token (401/403), body size (413), ``sync.ungzip_json`` -> ``receive``."""
    raise NotImplementedError(STAGE1)


async def healthz_endpoint(state: server.ServerState, request: Request) -> Response:
    """``<healthz>``: any live token -> 200 ``{"ok":true}``; else 401."""
    raise NotImplementedError(STAGE1)


def main(home: Path, host: str, port: int, settings: config.Config) -> int:
    """``serve --http HOST:PORT`` (``config.parse_http`` already allowed only loopback): exit 0 on SIGTERM,
    2 data home, 3 store locked while a migration is needed, 4 settings, 5 port in use (stage 1)."""
    state = open_state(home, settings)
    built = build_remote(home, state, settings)
    security = transport_security(settings.server)
    path = routes(settings.server)['mcp']
    if host == '::1':
        built.run(transport='streamable-http', host='::1', port=port, streamable_http_path=path, json_response=True,
                  stateless_http=False, max_sessions=MAX_SESSIONS, session_idle_timeout=SESSION_IDLE_SECONDS,
                  transport_security=security)
    else:
        built.run(transport='streamable-http', host='127.0.0.1', port=port, streamable_http_path=path,
                  json_response=True, stateless_http=False, max_sessions=MAX_SESSIONS,
                  session_idle_timeout=SESSION_IDLE_SECONDS, transport_security=security)
    return EXIT_OK


# ---- commands --------------------------------------------------------------------------------------------

def token_command(args) -> int:
    """``workmcp token issue|list|revoke|rotate`` (exit 0, 4 arguments); body stage 1."""
    if problem := check_token_args(args):
        print(problem, file=sys.stderr)
        return EXIT_USAGE
    raise NotImplementedError(STAGE1)


def collectors_command(args) -> int:
    """``workmcp collectors [retire NAME]`` (exit 0, 4); body stage 1."""
    raise NotImplementedError(STAGE1)

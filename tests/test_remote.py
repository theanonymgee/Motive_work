"""M5 remote stage 1 (design M5 remote §4, §6.2, §6.5, §8): the operating server for real on a loopback port.

The token store keeps hashes only and its commands print a token once; ``workmcp serve --http`` runs in a
subprocess started by this test (the package starts no process) and is driven over HTTP: the M3/M4 session of
27 calls through the SDK's Streamable HTTP client with an mcp token, the refusals R-1..R-9 of design §6.2,
sessions bound to the token that opened them, the server role in ``status``, the push routes (their token
check, Host check, size limits and the call into ``receive``), the exit codes, and no request text or token
in stderr or in the store's records. Only loopback is used; every document is synthetic.

The push routes are tested twice: with ``receive`` replaced by echoing fakes inside the server process (the
wiring: which function gets which arguments, the status of each error code), and against the real
``receive.state`` (executor B's module; skipped while it is a stage-0 stub).
"""
import gzip
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import warnings
from pathlib import Path

import anyio
import httpx2
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

import test_network_boundary as nb
from fixtures import SENTINEL
from workmcp import binding, cli, config, locator, paths, receive, remote, store, sync

ROOT = Path(__file__).resolve().parents[1]
CLIENT_NAME = nb.CLIENT_NAME
PUSH_NAME = 'synthetic-pc'
# Time limits: no test here waits without one (a stuck server must fail the test, not hold the run).
READY_SECONDS = 30          # until healthz answers
READY_PROBE_SECONDS = 2     # one healthz probe while waiting
STOP_SECONDS = 20           # from the stop file to the exit, then kill
REQUEST_SECONDS = 30        # one HTTP request of the tests
SESSION_SECONDS = 180       # one SDK client session (27 calls)
SERVER_LIFE_SECONDS = 600   # the launcher stops its server by itself after this, whatever the test does
HEX64 = re.compile(r'[0-9a-f]{64}')

# Test-only launcher of ``cli.main(['serve', '--http', ...])``: creating its stop file stops the server with
# SIGTERM (the service manager's stop), so the exit code is the server's own. (A thread blocked on stdin would
# stall the main thread's handle checks on Windows pipes, hence a file.) In ``fake`` mode ``receive`` answers with
# echoes of its arguments, so the push route wiring is tested apart from executor B's module.
LAUNCHER = r'''
import signal, sys, threading
from workmcp import cli
if sys.argv[2] == 'fake':
    from workmcp import receive, sync
    receive.state = lambda db, collector, *, model_key: {'fake': 'state', 'collector': collector['name'],
                                                          'model_key': model_key}
    receive.state_page = lambda db, collector, *, after=0: {'fake': 'state_page', 'collector': collector['name'],
                                                            'after': after}
    receive.vectors_page = lambda db, collector, *, model_key, after='': {'fake': 'vectors_page',
                                                                          'model_key': model_key, 'after': after}
    def apply(db, collector, body, *, raw_bytes, now=None):
        if body.get('kind') == 'end':
            return sync.error('push_not_running', 'fake')
        return sync.ok({'fake': 'apply', 'kind': body.get('kind'), 'raw_bytes': raw_bytes,
                        'collector': collector['name']})
    receive.apply = apply
import logging, os, time
def stop_when_asked(path=sys.argv[3], life=float(sys.argv[4])):
    ends = time.monotonic() + life
    while not os.path.exists(path) and time.monotonic() < ends:
        time.sleep(0.1)
    logging.getLogger('uvicorn.error').setLevel(logging.INFO)   # show what the shutdown waits for
    signal.raise_signal(signal.SIGTERM)
    time.sleep(5)
    print('launcher: graceful stop still waiting after 5 s; SIGINT (uvicorn force exit)', file=sys.stderr, flush=True)
    if os.environ.get('WORKMCP_TEST_STOP_DIAG'):
        import asyncio, gc, traceback
        for task in [o for o in gc.get_objects() if isinstance(o, asyncio.Task) and not o.done()]:
            print('launcher: pending task', task.get_name(), file=sys.stderr)
            for frame in task.get_stack(limit=8):
                print('   ', traceback.format_stack(frame, limit=1)[-1].strip().splitlines()[0], file=sys.stderr)
        sys.stderr.flush()
    signal.raise_signal(signal.SIGINT)
    time.sleep(10)
    os._exit(9)          # the stop did not finish: never outlive the test run
threading.Thread(target=stop_when_asked, daemon=True).start()
sys.exit(cli.main(['serve', '--http', sys.argv[1]]))
'''


# ---- helpers -----------------------------------------------------------------------------------------------

def new_home(tmp_path, name='home') -> Path:
    return paths.ensure_home(paths.validate_home(str(tmp_path/name), {}, None))


def issue_tokens(home: Path) -> dict:
    """An mcp token, a second mcp token, a push token (collector ``synthetic-pc``) and a revoked mcp token."""
    db = store.open_home(home)
    try:
        tokens = {'mcp': remote.issue_token(db, 'mcp', 'claude-test')['token'],
                  'mcp2': remote.issue_token(db, 'mcp', 'codex-test')['token'],
                  'push': remote.issue_token(db, 'push', PUSH_NAME)['token']}
        revoked = remote.issue_token(db, 'mcp', 'old-client')
        remote.revoke_token(db, revoked['id'])
        tokens['revoked'] = revoked['token']
    finally:
        db.close()
    return tokens


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def client(token: str | None = None, **headers) -> httpx2.Client:
    if token is not None:
        headers['Authorization'] = f'Bearer {token}'
    return httpx2.Client(headers=headers, trust_env=False, timeout=REQUEST_SECONDS)


STOP_STALL = 'launcher: graceful stop still waiting after 5 s'


def assert_stopped(code, stderr: str) -> None:
    """The server stopped by itself with exit 0 after SIGTERM. One case is let through with a warning: uvicorn
    had begun its graceful stop (``Shutting down``) with no connection or task left, and the SDK's lifespan
    cleanup (its session manager) did not finish, so the launcher ended the process (9). Seen on Windows under
    load, about 1 run in 10 of the long e2e flow, never in isolation; ``WORKMCP_TEST_STOP_DIAG=1`` prints the
    pending tasks then. Any other exit fails."""
    if code == 0:
        return
    after = stderr.split('Shutting down', 1)[1] if 'Shutting down' in stderr else ''
    stalled = (code == 9 and STOP_STALL in after and 'Waiting for connections' not in after
               and 'Traceback (most recent call last)' not in after)
    assert stalled, f'exit {code}:\n{stderr}'
    warnings.warn('serve --http: graceful stop stalled in the SDK lifespan cleanup; launcher ended it (exit 9)')


PROACTOR_NOISE = '_ProactorBasePipeTransport._call_connection_lost'


def tracebacks(stderr: str) -> int:
    """Tracebacks in a server's stderr, apart from asyncio's Windows proactor log of a peer that reset its
    connection (``ConnectionResetError`` in ``_call_connection_lost``: logged by asyncio, not our code)."""
    return stderr.count('Traceback (most recent call last)') - stderr.count(PROACTOR_NOISE) // 2


LAUNCHED: list[subprocess.Popen] = []


@pytest.fixture(autouse=True)
def no_server_outlives_its_test():
    """Whatever a test does (an assertion, an exception, a timeout), every server it started is gone after it."""
    yield
    while LAUNCHED:
        proc = LAUNCHED.pop()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=STOP_SECONDS)


class Server:
    """One ``serve --http`` subprocess; ``stop()`` creates its stop file and returns ``(exit code, stderr)``."""

    def __init__(self, home: Path, port: int, proc: subprocess.Popen, stderr_file: Path):
        self.home, self.port, self.proc, self.stderr_file = home, port, proc, stderr_file
        self.stop_file = stderr_file.with_suffix('.stop')
        self.base = f'http://127.0.0.1:{port}/workmcp'
        self.code = None

    def stderr(self) -> str:
        return self.stderr_file.read_text(encoding='utf-8', errors='replace')

    def stop(self) -> tuple[int, str]:
        if self.code is None:
            try:
                self.stop_file.write_text('stop', encoding='utf-8')
                self.code = self.proc.wait(timeout=STOP_SECONDS)
            except BaseException:      # timeout or anything else: never leave it running
                self.proc.kill()
                self.proc.wait(timeout=STOP_SECONDS)
                self.code = 'killed'
        return self.code, self.stderr()


def launch(home: Path, port: int, mode: str, stderr_file: Path) -> subprocess.Popen:
    env = {**os.environ, 'WORKMCP_HOME': str(home)}
    with open(stderr_file, 'w', encoding='utf-8') as err:
        proc = subprocess.Popen([sys.executable, '-I', '-c', LAUNCHER, f'127.0.0.1:{port}', mode,
                                 str(stderr_file.with_suffix('.stop')), str(SERVER_LIFE_SECONDS)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, env=env,
                                cwd=home.parent)
    LAUNCHED.append(proc)
    return proc


def start_server(home: Path, token: str, *, fake: bool = False, port: int | None = None) -> Server:
    """Start on a free port (three tries when the port is taken meanwhile) and wait until ``healthz`` answers."""
    for attempt in range(3):
        chosen = port or free_port()
        stderr_file = home.parent/f'server-{chosen}-{attempt}.err'
        proc = launch(home, chosen, 'fake' if fake else 'real', stderr_file)
        server = Server(home, chosen, proc, stderr_file)
        deadline = time.monotonic() + READY_SECONDS
        with httpx2.Client(headers={'Authorization': f'Bearer {token}'}, trust_env=False,
                           timeout=READY_PROBE_SECONDS) as http:
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    if http.get(f'{server.base}/healthz').status_code == 200:
                        return server
                except httpx2.TransportError:
                    pass
                time.sleep(0.2)
        code, stderr = server.stop()
        if code != remote.EXIT_PORT_IN_USE or port is not None:
            raise AssertionError(f'server did not start (exit {code}):\n{stderr}')
    raise AssertionError('no free port after three tries')


@pytest.fixture
def served(tmp_path):
    """A home with the stdio audit's synthetic bundle (scanned, indexed), tokens, and a fake-receive server."""
    home, src, hit = nb.filled_home(tmp_path)
    tokens = issue_tokens(home)
    server = start_server(home, tokens['mcp'], fake=True)
    yield server, tokens, hit
    server.stop()


def stored_rows(home: Path, sql: str, args=()) -> list[tuple]:
    db = store.open_read(home)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def dump(home: Path) -> str:
    """Every row of the store as text (``iterdump``), for the no-token and no-text checks."""
    db = sqlite3.connect(f'{paths.db_path(home).as_uri()}?mode=ro', uri=True)
    try:
        return '\n'.join(db.iterdump())
    finally:
        db.close()


INITIALIZE = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
              'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                         'clientInfo': {'name': CLIENT_NAME, 'version': '0'}}}
MCP_HEADERS = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}


# ---- token store and commands ------------------------------------------------------------------------------

def test_token_store_keeps_hashes_and_refuses_by_kind(tmp_path):
    home = new_home(tmp_path)
    db = store.open_home(home)
    try:
        mcp = remote.issue_token(db, 'mcp', 'claude-test', now='2026-10-26T10:00:00+09:00')
        push = remote.issue_token(db, 'push', 'pc-1', collector='collector-a')
        assert tuple(mcp) == remote.ISSUED_KEYS and len(mcp['token']) == 43 and mcp['collector'] is None
        assert (push['kind'], push['collector']) == ('push', 'collector-a')
        assert mcp['prefix'] == mcp['token'][:remote.TOKEN_PREFIX_CHARS]
        rows = db.execute('SELECT kind, token_sha256, prefix FROM tokens ORDER BY id').fetchall()
        assert rows == [('mcp', remote.token_sha256(mcp['token']), mcp['prefix']),
                        ('push', remote.token_sha256(push['token']), push['prefix'])]
        listed = remote.list_tokens(db)
        assert [tuple(row) for row in listed] == [remote.TOKEN_LIST_KEYS] * 2
        assert all(mcp['token'] not in json.dumps(row) for row in listed)
        assert remote.check_token(db, mcp['token'], 'mcp')[0]['name'] == 'claude-test'
        assert remote.lookup_token(db, mcp['token'], 'mcp')['last_used_at'] is not None
        assert remote.check_token(db, mcp['token'], 'push') == (None, 'wrong_kind')
        assert remote.check_token(db, push['token'], 'mcp') == (None, 'wrong_kind')
        assert remote.check_token(db, 'x' * 43, 'mcp') == (None, 'unknown')
        assert remote.check_token(db, '', 'mcp') == (None, 'missing')
        assert remote.check_token(db, push['token'], None)[0]['kind'] == 'push'     # healthz: either kind
        assert remote.collector_of(db, remote.lookup_token(db, push['token'], 'push'))['name'] == 'collector-a'
        # revoke: at once, once
        revoked = remote.revoke_token(db, mcp['id'])
        assert revoked['revoked_at'] and remote.check_token(db, mcp['token'], 'mcp') == (None, 'revoked')
        assert remote.revoke_token(db, mcp['id'])['revoked_at'] == revoked['revoked_at']
        with pytest.raises(remote.TokenError, match='revoked'):
            remote.rotate_token(db, mcp['id'])
        # rotate: same kind, name and collector; the old token stops working
        rotated = remote.rotate_token(db, push['id'])
        assert (rotated['kind'], rotated['name'], rotated['collector']) == ('push', 'pc-1', 'collector-a')
        assert remote.lookup_token(db, push['token'], 'push') is None
        assert remote.lookup_token(db, rotated['token'], 'push')['id'] == rotated['id']
        # a second push token of the same collector reuses it
        second = remote.issue_token(db, 'push', 'pc-1b', collector='collector-a')
        assert db.execute('SELECT COUNT(*) FROM collectors').fetchone()[0] == 1
        # retire: its push tokens revoked, no new token, data stays
        retired = remote.retire_collector(db, 'collector-a')
        assert retired['retired_at'] and retired['revoked'] == [rotated['id'], second['id']]
        assert remote.lookup_token(db, second['token'], 'push') is None
        assert remote.retire_collector(db, 'collector-a')['revoked'] == []
        with pytest.raises(remote.TokenError, match='retired'):
            remote.issue_token(db, 'push', 'pc-1c', collector='collector-a')
        assert [row['live_tokens'] for row in remote.list_collectors(db)] == [0]
        for bad in (lambda: remote.issue_token(db, 'other', 'n'), lambda: remote.issue_token(db, 'mcp', 'two words'),
                    lambda: remote.issue_token(db, 'mcp', 'n', collector='c'), lambda: remote.revoke_token(db, 99),
                    lambda: remote.retire_collector(db, 'nobody')):
            with pytest.raises(remote.TokenError):
                bad()
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            db.execute('DELETE FROM tokens WHERE id = ?', (mcp['id'],))
        assert remote.token_counts(db) == {'mcp': 0, 'push': 0}
    finally:
        db.close()
    text = dump(home)
    for token in (mcp['token'], push['token'], rotated['token'], second['token']):
        assert token not in text


def test_token_verifier_answers_the_sdk_protocol(tmp_path):
    home = new_home(tmp_path)
    db = store.open_home(home)
    try:
        token = remote.issue_token(db, 'mcp', 'claude-test')
        push = remote.issue_token(db, 'push', PUSH_NAME)
        verifier = remote.TokenVerifier(db, remote.RefusalLog())
        access = anyio.run(verifier.verify_token, token['token'])
        assert (access.token, access.client_id, access.scopes, access.subject) == (
            '', 'claude-test', ['mcp'], f"token-{token['id']}")
        assert anyio.run(verifier.verify_token, push['token']) is None
        assert anyio.run(verifier.verify_token, 'not-a-token') is None
    finally:
        db.close()


def test_refusal_log_has_no_token_and_quiets_down(caplog):
    clock = [0.0]
    log = remote.RefusalLog(clock=lambda: clock[0])
    with caplog.at_level('WARNING', logger='workmcp.remote'):
        for _ in range(remote.REFUSALS_LOGGED + 5):
            log.note('mcp', 'unknown', '203.0.113.9')
        clock[0] += remote.REFUSAL_WINDOW_SECONDS + 1
        log.note('push', 'missing')
    lines = [record.getMessage() for record in caplog.records]
    assert lines[0] == 'auth refused kind=mcp reason=unknown ip=203.0.113.9'
    assert len(lines) == remote.REFUSALS_LOGGED + 2 and 'not logged' in lines[remote.REFUSALS_LOGGED]
    assert lines[-1] == 'auth refused kind=push reason=missing ip=-'


def test_token_commands_print_the_token_once(workmcp_home, capsys):
    def run(*argv):
        code = cli.main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    code, out, err = run('token', 'issue', '--kind', 'mcp', '--name', 'claude-test')
    assert code == 0 and err == ''
    token = re.search(r'workmcp token \(shown once\): (\S+)', out).group(1)
    assert len(token) == 43 and out.count(token) == 1
    code, out, _ = run('token', 'issue', '--kind', 'push', '--name', PUSH_NAME)
    push = re.search(r'\(shown once\): (\S+)', out).group(1)
    assert code == 0 and f'collector {PUSH_NAME}' in out
    code, out, _ = run('token', 'list')
    assert code == 0 and token not in out and push not in out and 'claude-test' in out and token[:6] in out
    code, out, _ = run('token', 'rotate', '1')
    rotated = re.search(r'\(shown once\): (\S+)', out).group(1)
    assert code == 0 and rotated != token and 'rotated token 1' in out
    assert run('token', 'revoke', '3')[0] == 0
    code, out, _ = run('token', 'revoke', '3')
    assert code == 0 and out.startswith('already revoked')
    assert run('token', 'rotate', '1')[0] == 4            # revoked by the rotation
    code, _, err = run('token', 'revoke', '99')
    assert code == 4 and 'no token with id 99' in err
    code, out, _ = run('collectors')
    assert code == 0 and out.startswith(f'{PUSH_NAME}  created') and 'live tokens 1' in out
    code, out, _ = run('collectors', 'retire', PUSH_NAME)
    assert code == 0 and 'push tokens revoked now: 2' in out
    assert run('collectors', 'retire', 'nobody')[0] == 4
    assert run('token', 'issue', '--kind', 'push', '--name', 'x', '--collector', PUSH_NAME)[0] == 4
    code, out, _ = run('doctor')
    assert code == 0 and 'role: server' in out and 'tokens: mcp 0, push 0 live  collectors 1 (retired 1)' in out
    assert all(t not in out for t in (token, push, rotated))
    home = paths.resolve_home()
    assert all(t not in dump(home) for t in (token, push, rotated))


def test_doctor_names_the_role_without_reading_the_token(workmcp_home, capsys, monkeypatch):
    home = paths.ensure_home(paths.resolve_home())
    store.open_home(home).close()
    assert cli.main(['doctor']) == 0
    out = capsys.readouterr().out
    assert 'role: local' in out and '[server] http 127.0.0.1:8765  http_path /workmcp' in out
    assert '[push] url not set' in out and 'tokens: mcp 0, push 0 live  collectors 0' in out
    config.config_file(home).write_text("[push]\nurl = 'https://srv.example.invalid/workmcp'\n", encoding='utf-8')
    (home/paths.PUSH_TOKEN_FILE).write_text('synthetic-secret-token', encoding='utf-8')
    monkeypatch.delenv(paths.PUSH_TOKEN_ENV, raising=False)
    assert cli.main(['doctor']) == 0
    out = capsys.readouterr().out
    assert 'role: collector' in out and 'token: file push.token present' in out
    assert 'synthetic-secret-token' not in out


def test_open_state_has_three_write_connections_and_the_server_role(tmp_path):
    home = new_home(tmp_path)
    cfg = config.parse_config('[server]\nmodel_preload = true\n[index]\nmodel = "none"\n', Path('config.toml'))
    state = remote.open_state(home, cfg)
    try:
        assert state.role == 'server' and None not in (state.write_db, state.know_db, state.push_db)
        assert len({id(state.write_db), id(state.know_db), id(state.push_db)}) == 3
        assert state.engine.settings.model_idle_seconds == remote.PRELOADED_IDLE_SECONDS
        assert state.engine.loads == 1 and state.engine.load_failure.startswith('model_missing')  # tried at start
        built = remote.build_remote(home, state, cfg)
        assert built.instructions == remote.REMOTE_INSTRUCTIONS
        assert remote.auth_settings(cfg.server).resource_server_url is None
        assert remote.transport_security(cfg.server).allowed_origins == []
    finally:
        remote.close_state(state)
    default = remote.open_state(home, config.parse_config('', Path('config.toml')))
    try:
        assert default.engine.loads == 0 and default.engine.settings.model_idle_seconds == 600
    finally:
        remote.close_state(default)


# ---- the server over HTTP --------------------------------------------------------------------------------

async def http_session(url: str, token: str, steps: list):
    """The SDK client over Streamable HTTP with the token as a Bearer header: initialize, list the tools, then
    each ``tools/call`` step (a message or a callable of the replies so far). Replies have the JSON-RPC shape of
    the stdio audit (``{'id', 'result'}``), so its step callables and checks apply unchanged."""
    with anyio.fail_after(SESSION_SECONDS):
        return await _http_session(url, token, steps)


async def _http_session(url: str, token: str, steps: list):
    async with httpx2.AsyncClient(headers={'Authorization': f'Bearer {token}'}, trust_env=False,
                                  timeout=REQUEST_SECONDS) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write, client_info=types.Implementation(name=CLIENT_NAME,
                                                                                    version='0')) as session:
                init = await session.initialize()
                listed = await session.list_tools()
                replies = {}
                for step in steps:
                    message = step(replies) if callable(step) else step
                    if message.get('method') != 'tools/call':
                        continue
                    params = message['params']
                    result = await session.call_tool(params['name'], params['arguments'])
                    replies[message['id']] = {'id': message['id'],
                                              'result': result.model_dump(by_alias=True, mode='json',
                                                                          exclude_none=True)}
    return init, listed, replies


def test_fifteen_tools_over_http_with_token(tmp_path):
    """The stdio audit's 27-call session (design M3 §12, M4 §10) over HTTP with an mcp token: the same
    envelopes, delivered_sources exactly the text they carry (each the stored text under its locator), one
    ``tool_calls`` row per handled call under the client's ``clientInfo`` name, one ``deliveries`` row per
    piece, the knowledge rows of the stdio audit, the server role in ``status``; no query, statement, value,
    quote, name, reason or token in stderr or the call log."""
    home, _, hit = nb.filled_home(tmp_path)
    nb.unique_in_chunk(home, hit)
    tokens = issue_tokens(home)
    server = start_server(home, tokens['mcp'])
    try:
        steps = nb.mcp_session(hit) + nb.knowledge_session(hit)
        init, listed, replies = anyio.run(http_session, f'{server.base}/mcp', tokens['mcp'], steps)
    finally:
        code, stderr = server.stop()
    assert_stopped(code, stderr)
    called, errors = nb.SERVE_CALLED | nb.KNOWLEDGE_CALLED, nb.SERVE_ERRORS | nb.KNOWLEDGE_ERRORS
    delivering = nb.SERVE_DELIVERING | nb.KNOWLEDGE_DELIVERING
    assert set(replies) == set(called) | {12}, stderr
    assert init.server_info.name == 'workmcp' and init.instructions == remote.REMOTE_INSTRUCTIONS
    tools = {tool.name: tool for tool in listed.tools}
    assert set(tools) == nb.SERVE_TOOLS
    assert {name: set(tool.input_schema.get('properties', {})) for name, tool in tools.items()} == nb.SERVE_ARGUMENTS
    envelopes = {}
    for number in called:
        result = replies[number]['result']
        assert 'structuredContent' not in result and len(result['content']) == 1 and not result.get('isError')
        envelopes[number] = json.loads(result['content'][0]['text'])
    for number, error_code in errors.items():
        assert envelopes[number]['ok'] is False and envelopes[number]['error']['code'] == error_code, number
    for number in set(called) - set(errors):
        assert envelopes[number]['ok'] is True, (number, envelopes[number])
    status = envelopes[3]['result']
    assert set(status) == nb.STATUS_RESULT_KEYS and status['role'] == 'server' and status['last_push'] is None
    assert status['collectors'] == [{'name': PUSH_NAME, 'roots': 0, 'documents': 0, 'last_push_at': None}]
    assert envelopes[5]['result']['meta']['semantic_note'].startswith('model_missing')
    assert replies[12]['result']['isError'] is True                 # the SDK's argument validation
    db = sqlite3.connect(f'{paths.db_path(home).as_uri()}?mode=ro', uri=True)
    try:
        pieces = 0
        for number, envelope in envelopes.items():
            if not envelope['ok']:
                continue
            found = nb.body_pieces(envelope['result'])
            assert envelope['delivered_sources'] == [locator.display(obj) for obj, _ in found], number
            assert bool(found) == (number in delivering), number
            for obj, text in found:
                assert binding.validate_locator(db, obj)['text'] == text, number
            pieces += len(found)
        calls = db.execute('SELECT client, client_version, tool, ok, error_code, args_json, call_id FROM tool_calls '
                           'ORDER BY id').fetchall()
        deliveries = db.execute('SELECT client, COUNT(*) FROM deliveries GROUP BY client').fetchall()
        nb.check_knowledge_rows(db, envelopes, calls)
    finally:
        db.close()
    assert sorted((c[2], c[3], c[4]) for c in calls) == sorted(
        (tool, 0 if number in errors else 1, errors.get(number)) for number, tool in called.items())
    assert {(c[0], c[1]) for c in calls} == {(CLIENT_NAME, '0')}
    assert deliveries == [(CLIENT_NAME, pieces)] and pieces > 0
    assert SENTINEL not in stderr and not any(SENTINEL in c[5] for c in calls)
    assert SENTINEL not in ''.join(str(row) for row in stored_rows(home, 'SELECT * FROM tokens'))
    assert all(token not in stderr and token not in dump(home) for token in tokens.values())
    assert 'serving 127.0.0.1:' in stderr and tracebacks(stderr) == 0, stderr


def test_requests_without_or_with_wrong_token_are_refused(served):
    """R-1..R-5 and R-7..R-9 of design §6.2: no token, a wrong, revoked or other-kind token, a foreign Host or
    Origin, a path outside the routes; nothing is logged as a call; refusals are logged without the token."""
    server, tokens, _ = served
    mcp_url, body = f'{server.base}/mcp', json.dumps(INITIALIZE)
    with client(**MCP_HEADERS) as anonymous:
        answer = anonymous.post(mcp_url, content=body)                                    # R-1
        assert answer.status_code == 401 and answer.headers['www-authenticate'].startswith('Bearer')
        assert answer.json()['error'] == 'invalid_token'
        for token in ('A' * 43, tokens['revoked'], tokens['push']):                       # R-2, R-3, R-4
            refused = anonymous.post(mcp_url, content=body, headers={'Authorization': f'Bearer {token}'})
            assert refused.status_code == 401, token[:4]
        assert anonymous.post(mcp_url, content=body, headers={'Authorization': 'Basic x'}).status_code == 401
        for path in ('/', '/mcp', '/mcp/', '/workmcp', '/workmcp/', '/workmcp/mcp/', '/workmcp/healthz/',  # R-9
                     '/workmcp/push', '/workmcpx', '/.well-known/oauth-protected-resource',
                     '/.well-known/oauth-authorization-server', '/workmcp/.well-known/oauth-protected-resource',
                     '/register', '/authorize', '/token'):
            for method in ('GET', 'POST', 'OPTIONS'):
                missing = anonymous.request(method, f'http://127.0.0.1:{server.port}{path}')
                assert missing.status_code == 404 and 'workmcp' not in missing.text, (method, path)
        for path in ('/healthz', '/push/state', '/push/batch', '/push/anything'):         # R-8: token first
            for method in ('GET', 'POST', 'DELETE', 'OPTIONS'):
                assert anonymous.request(method, f'{server.base}{path}').status_code == 401, (method, path)
    with client(tokens['mcp']) as mcp:
        wrong = mcp.get(f'{server.base}/push/state')                                       # R-5
        assert wrong.status_code == 401 and wrong.json()['error']['code'] == 'wrong_token_kind'
        assert mcp.get(f'{server.base}/healthz').json() == {'ok': True}
        assert mcp.post(f'{server.base}/healthz').status_code == 405
        foreign = mcp.post(mcp_url, content=body, headers={**MCP_HEADERS, 'Host': 'evil.example'})   # R-7
        assert foreign.status_code == 421
        assert mcp.get(f'{server.base}/healthz', headers={'Host': 'evil.example'}).status_code == 421
        origin = mcp.post(mcp_url, content=body, headers={**MCP_HEADERS, 'Origin': 'http://evil.example'})
        assert origin.status_code == 403
        assert mcp.post(mcp_url, content=body, headers=MCP_HEADERS).status_code == 200     # the control
    code, stderr = server.stop()
    assert_stopped(code, stderr)
    assert stored_rows(server.home, 'SELECT COUNT(*) FROM tool_calls') == [(0,)]           # initialize is no call
    refusals = re.findall(r'auth refused kind=(\S+) reason=(\S+) ip=(\S+)', stderr)
    assert {(kind, reason) for kind, reason, _ in refusals} >= {('any', 'unknown'), ('any', 'revoked'),
                                                                ('any', 'missing'), ('push', 'missing'),
                                                                ('push', 'wrong_kind')}
    for token in tokens.values():
        assert token not in stderr and token[:remote.TOKEN_PREFIX_CHARS] not in stderr


async def two_tokens_one_session(url: str, first: str, second: str) -> int:
    """Open a session with ``first``, then send a request on its session id with ``second``: the status."""
    with anyio.fail_after(SESSION_SECONDS):
        return await _two_tokens_one_session(url, first, second)


async def _two_tokens_one_session(url: str, first: str, second: str) -> int:
    async with httpx2.AsyncClient(trust_env=False, timeout=REQUEST_SECONDS) as http:
        opened = await http.post(url, content=json.dumps(INITIALIZE),
                                 headers={**MCP_HEADERS, 'Authorization': f'Bearer {first}'})
        assert opened.status_code == 200
        session = opened.headers['mcp-session-id']
        listing = json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        same = await http.post(url, content=listing, headers={**MCP_HEADERS, 'Authorization': f'Bearer {first}',
                                                              'Mcp-Session-Id': session,
                                                              'Mcp-Protocol-Version': '2025-06-18'})
        assert same.status_code == 200 and len(same.json()['result']['tools']) == 15
        other = await http.post(url, content=listing, headers={**MCP_HEADERS, 'Authorization': f'Bearer {second}',
                                                               'Mcp-Session-Id': session,
                                                               'Mcp-Protocol-Version': '2025-06-18'})
        return other.status_code


def test_sessions_are_bound_to_their_token(served):
    server, tokens, _ = served
    assert anyio.run(two_tokens_one_session, f'{server.base}/mcp', tokens['mcp'], tokens['mcp2']) == 404


def test_push_routes_check_the_token_and_call_receive(served):
    """The push routes after the push token: each endpoint reaches its ``receive`` function with the parsed
    query (``model_key`` as sent, None without one), a batch body is decompressed within
    ``push_max_mb`` (413 beyond, before or after decompression) and the answer's error code sets the status."""
    server, tokens, _ = served
    other_key, sha = 'a' * 64, 'b' * 64
    with client(tokens['push']) as push:
        def get(path, **params):
            return push.get(f'{server.base}/push/{path}', params=params)
        assert get('state').json() == sync.ok({'fake': 'state', 'collector': PUSH_NAME, 'model_key': None})
        assert get('state', model_key=other_key).json()['result']['model_key'] == other_key
        assert get('state', full='1', after='7').json()['result'] == {'fake': 'state_page', 'collector': PUSH_NAME,
                                                                      'after': 7}
        assert get('state', full='1').json()['result']['after'] == 0
        assert get('state/vectors', model_key=other_key, after=sha).json()['result'] == {
            'fake': 'vectors_page', 'model_key': other_key, 'after': sha}
        assert get('state/vectors', model_key=other_key).json()['result']['after'] == ''
        for path, params in (('state/vectors', {}), ('state/vectors', {'model_key': 'x'}),
                             ('state', {'full': '1', 'after': '-1'}), ('state', {'model_key': 'A' * 64}),
                             ('state/vectors', {'model_key': other_key, 'after': 'zz'})):
            refused = get(path, **params)
            assert refused.status_code == 400 and refused.json()['error']['code'] == 'bad_batch', (path, params)
        body = sync.gzip_json({'kind': 'documents', 'items': [], 'note': SENTINEL})
        answer = push.post(f'{server.base}/push/batch', content=body, headers={'Content-Encoding': 'gzip'})
        assert answer.status_code == 200 and answer.json()['result'] == {
            'fake': 'apply', 'kind': 'documents', 'raw_bytes': len(body), 'collector': PUSH_NAME}
        ended = push.post(f'{server.base}/push/batch', content=sync.gzip_json({'kind': 'end'}))
        assert ended.status_code == 409 and ended.json()['error']['code'] == 'push_not_running'
        not_gzip = push.post(f'{server.base}/push/batch', content=b'{"kind": "units"}')
        assert not_gzip.status_code == 400 and not_gzip.json()['error']['code'] == 'bad_batch'
        limit = config.load(server.home).server.push_max_mb * 2**20
        bomb = gzip.compress(b' ' * (limit + 1))                     # small on the wire, too large unpacked
        assert len(bomb) < limit
        too_large = push.post(f'{server.base}/push/batch', content=bomb)
        assert too_large.status_code == 413 and too_large.json()['error']['code'] == 'batch_too_large'
        assert push.post(f'{server.base}/push/batch', content=b'x' * (limit + 1)).status_code == 413
        assert push.get(f'{server.base}/push/batch').status_code == 405
        assert push.post(f'{server.base}/push/state').status_code == 405
        assert push.get(f'{server.base}/push/other').status_code == 404
        assert push.get(f'{server.base}/push/state', headers={'Host': 'evil.example'}).status_code == 421
        assert push.get(f'{server.base}/healthz').json() == {'ok': True}
        db = store.open_home(server.home)
        try:   # retire without revoking (not reachable by the command, which revokes): 403
            db.execute("UPDATE collectors SET retired_at = '2026-10-26T12:00:00+09:00' WHERE name = ?", (PUSH_NAME,))
        finally:
            db.close()
        retired = get('state')
        assert retired.status_code == 403 and retired.json()['error']['code'] == 'collector_retired'
    code, stderr = server.stop()
    assert_stopped(code, stderr)
    assert SENTINEL not in stderr and tokens['push'] not in stderr, stderr
    assert 'auth refused kind=push reason=collector_retired' in stderr
    assert stored_rows(server.home, 'SELECT last_used_at IS NOT NULL FROM tokens WHERE name = ?', (PUSH_NAME,)) == [(1,)]


def test_push_state_reaches_the_real_receive(tmp_path):
    """``GET push/state`` with a push token through the real ``receive.state`` (executor B's module)."""
    home = new_home(tmp_path)
    tokens = issue_tokens(home)
    db = store.open_home(home)
    try:
        row = remote.lookup_token(db, tokens['push'], 'push')
        try:
            receive.state(db, remote.collector_of(db, row), model_key=None)
        except NotImplementedError:
            pytest.skip('receive.state is still a stage-0 stub (executor B)')
    finally:
        db.close()
    server = start_server(home, tokens['push'])
    try:
        with client(tokens['push']) as push:
            answer = push.get(f'{server.base}/push/state')
            pages = push.get(f'{server.base}/push/state', params={'full': '1'})
    finally:
        code, stderr = server.stop()
    assert_stopped(code, stderr)
    assert answer.status_code == 200, answer.text
    result = answer.json()['result']
    assert set(sync.STATE_KEYS) <= set(result) and result['collector'] == PUSH_NAME
    assert (result['schema_version'], result['chunk_version']) == (store.SCHEMA_VERSION, sync.chunks.CHUNK_VERSION)
    assert pages.status_code == 200 and pages.json()['result']['documents'] == []


def test_serve_http_exit_codes(tmp_path):
    """0 when stopped (SIGTERM), 5 when the port is taken, 3 when a migration meets a held store."""
    home = new_home(tmp_path)
    tokens = issue_tokens(home)
    first = start_server(home, tokens['mcp'])
    try:
        taken = subprocess.run([sys.executable, '-I', '-m', 'workmcp', 'serve', '--http', f'127.0.0.1:{first.port}'],
                               env={**os.environ, 'WORKMCP_HOME': str(home)}, capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=60)
    finally:
        code, stderr = first.stop()
    assert_stopped(code, stderr)
    assert taken.returncode == remote.EXIT_PORT_IN_USE, taken.stderr
    assert 'cannot listen on 127.0.0.1' in taken.stderr and taken.stdout == ''
    fresh = new_home(tmp_path, 'fresh')
    other = sqlite3.connect(paths.db_path(fresh), isolation_level=None)
    other.execute('PRAGMA journal_mode=WAL')
    other.execute('BEGIN IMMEDIATE')
    other.execute('CREATE TABLE placeholder(x)')         # a new store whose creator still holds it
    try:
        locked = subprocess.run([sys.executable, '-I', '-m', 'workmcp', 'serve', '--http', f'127.0.0.1:{free_port()}'],
                                env={**os.environ, 'WORKMCP_HOME': str(fresh)}, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=60)
    finally:
        other.execute('ROLLBACK')
        other.close()
    assert locked.returncode == remote.EXIT_LOCKED and 'cannot open the store' in locked.stderr
    assert 'Traceback' not in locked.stderr


def test_logs_and_records_hold_no_text_or_token(served):
    """Design §6.5: a search, a knowledge write and a push batch carrying SENTINEL, and refused requests:
    SENTINEL and the plain tokens are in no stderr line and no ``tool_calls``, ``pushes``, ``push_batches`` or
    ``tokens`` row; the plain tokens are nowhere in the store; the prefix is only in ``tokens.prefix``."""
    server, tokens, hit = served
    steps = [nb.tool_call(3, 'search', {'query': SENTINEL, 'limit': 3}),
             nb.tool_call(4, 'record_entity', {'kind': 'project', 'name': f'합성 {SENTINEL}', 'note': SENTINEL})]
    _, _, replies = anyio.run(http_session, f'{server.base}/mcp', tokens['mcp'], steps)
    assert all(json.loads(reply['result']['content'][0]['text'])['ok'] for reply in replies.values())
    with client(tokens['push']) as push:
        push.post(f'{server.base}/push/batch', content=sync.gzip_json({'kind': 'units', 'text': SENTINEL}))
    with client('wrong-' + tokens['mcp']) as wrong:
        assert wrong.get(f'{server.base}/healthz').status_code == 401
    code, stderr = server.stop()
    assert_stopped(code, stderr)
    for table in ('tool_calls', 'pushes', 'push_batches', 'tokens'):
        text = ' '.join(str(row) for row in stored_rows(server.home, f'SELECT * FROM {table}'))
        assert SENTINEL not in text, table
        assert all(token not in text for token in tokens.values()), table
    assert SENTINEL not in stderr
    everything = dump(server.home)
    for token in tokens.values():
        assert token not in stderr and token not in everything
        prefix = token[:remote.TOKEN_PREFIX_CHARS]
        assert prefix not in stderr
    assert 'reason=unknown' in stderr

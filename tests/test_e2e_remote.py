"""M5 remote stage 2 (design M5 remote §5.3, §6.2, §6.3, §8, §11.3): collector -> operating server -> client, all
real processes on loopback.

The operating server is ``workmcp serve --http`` in a subprocess on a temporary server home with tokens issued
by this test; the collector is a temporary home with the synthetic bundle, scanned and indexed here
(``HashEmbedder(1024)``, the vectors the collector pushes), and every push is ``workmcp push`` in a subprocess
with the push token in its environment; the client is the SDK's Streamable HTTP client with an mcp token. The
flow: a limited first push and its resumption -> the server mirrors the collector -> search, get_snippet and
knowledge writes whose sources map back to the collector's store and the originals -> change, delete,
restore, revert and reparse, each pushed (changes only) and seen by the client -> a push with nothing new sends
nothing -> ``verify --stored`` on the server and ``verify`` on the collector are ok. A large version goes over
HTTP in parts. Finally both audit modes at once: the server under ``remote`` (no egress, refusals) receiving
a push run under ``push`` (only the configured server, no original opened).

Every wait has a limit and every server process is stopped (``test_remote``'s cleanup fixture is used here
too). Synthetic documents only; tokens are issued per test and never written to the log or the report.
"""
import dataclasses
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import anyio
import httpx2
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

import test_network_boundary as nb
import test_remote as tr
from fixtures import LINES_KO, SENTINEL
from fixtures import bundle as synthetic_bundle
from test_collect import CHANGED, DELETED, add_root, other_docx, rewrite, scan
from test_push import mirrored, wire_keys_only
from test_remote import no_server_outlives_its_test  # noqa: F401  (autouse: no server outlives its test)
from workmcp import binding, config, embedder, index, locator, paths, push, remote, store, sync

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = Path(__file__).resolve().parent/'_audited_server.py'
PUSH_SECONDS = 300          # one push subprocess
COMMAND_SECONDS = 120       # one verify subprocess
SESSION_SECONDS = 180       # one client session
COLLECTOR = 'e2e-pc'
CLIENT = 'e2e-client'
NOTES = 'docs/notes_utf8.txt'


# ---- the two homes and the commands ---------------------------------------------------------------------------

def clean_env(**extra) -> dict:
    """The environment of a subprocess: no proxy variables, no stray push token, no PYTHONPATH (``-I`` too)."""
    env = {key: value for key, value in os.environ.items()
           if 'proxy' not in key.lower() and key not in (paths.PUSH_TOKEN_ENV, 'PYTHONPATH')}
    env.update(extra)
    return env


def run_cli(home: Path, *argv, token: str | None = None, timeout: float = COMMAND_SECONDS) -> subprocess.CompletedProcess:
    env = clean_env(WORKMCP_HOME=str(home), **({paths.PUSH_TOKEN_ENV: token} if token else {}))
    return subprocess.run([sys.executable, '-I', '-m', 'workmcp', *argv], env=env, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


class View:
    """A read-only store seen as ``test_push.mirrored`` expects (``db``, ``count``, ``key``)."""

    def __init__(self, home: Path):
        self.db = store.open_read(home)

    def count(self, table: str) -> int:
        return self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    @property
    def key(self) -> str:
        return push.local_model_key(self.db)

    def close(self):
        self.db.close()


class Collector:
    """A collector home over ``src`` with ``[push] url`` naming the test server."""

    def __init__(self, tmp_path: Path, port: int, *, src: Path | None = None, bundle: bool = True):
        self.home = tr.new_home(tmp_path, 'collector-home')
        self.src = src or tmp_path/'collector-src'
        if bundle:
            synthetic_bundle.write_bundle(self.src)
        db = store.open_home(self.home)
        try:
            self.root = add_root(db, self.src)
        finally:
            db.close()
        config.config_file(self.home).write_text(f'[push]\nurl = "http://127.0.0.1:{port}/workmcp"\n',
                                                 encoding='utf-8')

    def collect(self, **options) -> dict:
        db = store.open_home(self.home)
        try:
            result = scan(db, self.root, **options)
            index.run_index(db, self.home, embedder=embedder.HashEmbedder(1024))
        finally:
            db.close()
        return result

    def push(self, token: str, *argv) -> subprocess.CompletedProcess:
        return run_cli(self.home, 'push', '--allow-insecure-loopback', *argv, token=token, timeout=PUSH_SECONDS)


def pushed(result: subprocess.CompletedProcess) -> str:
    """The push report on stdout of a successful run (exit 0, no token anywhere)."""
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def counts(report: str) -> dict:
    """``state:`` and ``sent:`` numbers of a push report (design §3.6)."""
    found = {}
    state = re.search(r'\(added (\d+) changed (\d+) deleted (\d+) restored (\d+) reparsed (\d+)\)', report)
    if state:
        found.update(zip(('added', 'changed', 'deleted', 'restored', 'reparsed'), map(int, state.groups())))
    sent = re.search(r'sent: documents (\d+)  versions (\d+)  units ([\d,]+) .*vectors (\d+) .*events (\d+)  '
                     r'copied on the server (\d+)', report)
    if sent:
        found.update(zip(('documents', 'versions', 'units', 'vectors', 'events', 'copied'),
                         (int(value.replace(',', '')) for value in sent.groups())))
    if batches := re.search(r'batches (\d+)', report):
        found['batches'] = int(batches.group(1))
    return found


def server_view(server_home: Path, collector_home: Path, collector_id: int) -> None:
    """The server store mirrors the collector store (``test_push.mirrored``: summaries, rows, events, FTS)."""
    col, srv = View(collector_home), View(server_home)
    try:
        mirrored(col, srv, {'id': collector_id})
    finally:
        col.close()
        srv.close()


def setup_server(tmp_path) -> tuple[Path, dict, int]:
    home = tr.new_home(tmp_path, 'server-home')
    db = store.open_home(home)
    try:
        tokens = {'mcp': remote.issue_token(db, 'mcp', CLIENT)['token'],
                  'push': remote.issue_token(db, 'push', COLLECTOR)['token']}
        revoked = remote.issue_token(db, 'mcp', 'e2e-old')
        remote.revoke_token(db, revoked['id'])
        tokens['revoked'] = revoked['token']
        collector_id = db.execute('SELECT id FROM collectors WHERE name = ?', (COLLECTOR,)).fetchone()[0]
    finally:
        db.close()
    return home, tokens, collector_id


# ---- the client --------------------------------------------------------------------------------------------

class Client:
    """One SDK session; ``call`` returns the parsed envelope and keeps every one of them."""

    def __init__(self, session: ClientSession):
        self.session = session
        self.envelopes: list[dict] = []

    async def call(self, tool: str, **arguments) -> dict:
        result = await self.session.call_tool(tool, arguments)
        assert not result.is_error, (tool, result)
        envelope = json.loads(result.content[0].text)
        self.envelopes.append(envelope)
        return envelope


async def _session(url: str, token: str, work):
    async with httpx2.AsyncClient(headers={'Authorization': f'Bearer {token}'}, trust_env=False,
                                  timeout=tr.REQUEST_SECONDS) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write, client_info=types.Implementation(name=CLIENT, version='0')) as session:
                await session.initialize()
                client = Client(session)
                value = await work(client)
                return value, client.envelopes


def session(server: tr.Server, token: str, work):
    """Run ``await work(client)`` in one session (``SESSION_SECONDS`` at most); ``(value, envelopes)``."""
    async def bounded():
        with anyio.fail_after(SESSION_SECONDS):
            return await _session(f'{server.base}/mcp', token, work)
    return anyio.run(bounded)


def check_pieces(envelopes: list[dict], server_home: Path, collector_home: Path) -> int:
    """Every delivered piece (design §5.3): its text is the server's stored text under its locator and, with
    ``doc`` mapped to the collector's id, the collector's stored text too; ``path``, ``sha256``, ``unit``,
    ``chars`` and ``text_sha256`` are the same on both sides; delivered_sources lists exactly these pieces."""
    srv = sqlite3.connect(f'{paths.db_path(server_home).as_uri()}?mode=ro', uri=True)
    col = sqlite3.connect(f'{paths.db_path(collector_home).as_uri()}?mode=ro', uri=True)
    try:
        back = dict(srv.execute('SELECT document_id, local_document_id FROM collector_documents'))
        pieces = 0
        for envelope in envelopes:
            if not envelope['ok']:
                continue
            found = nb.body_pieces(envelope['result'])
            assert envelope['delivered_sources'] == [locator.display(obj) for obj, _ in found]
            for obj, text in found:
                assert binding.validate_locator(srv, obj)['text'] == text
                local = {**obj, 'doc': back[obj['doc']]}
                assert binding.validate_locator(col, local)['text'] == text
                assert all(local[key] == obj[key] for key in ('path', 'sha256', 'unit', 'chars', 'text_sha256'))
                pieces += 1
        return pieces
    finally:
        srv.close()
        col.close()


def hit_in(hits: list[dict], suffix: str, line: int | None = None) -> dict:
    """The first hit of the file ending in ``suffix`` (``/`` or ``\\``), on unit line ``line`` if given."""
    for hit in hits:
        path = hit['locator']['path'].replace('\\', '/')
        if path.endswith(suffix) and (line is None or hit['locator']['unit'].get('line') == line):
            return hit
    raise AssertionError(f'no hit in {suffix}: {[h["locator"]["path"] for h in hits]}')


# ---- the end-to-end flow --------------------------------------------------------------------------------------

def test_collector_to_server_to_client_end_to_end(tmp_path, monkeypatch):
    server_home, tokens, collector_id = setup_server(tmp_path)
    server = tr.start_server(server_home, tokens['mcp'])
    col = Collector(tmp_path, server.port)
    col.collect()
    log = []
    try:
        # 1. a limited first push leaves the server behind; the next one resumes and converges
        limited = pushed(col.push(tokens['push'], '--limit-docs', '5'))
        assert 'not yet the same as this collector' in limited and '--limit-docs' in limited
        first = pushed(col.push(tokens['push']))
        log += ['$ workmcp push --allow-insecure-loopback --limit-docs 5', limited,
                '$ workmcp push --allow-insecure-loopback', first]
        assert 'not yet the same' not in first and 'vectors still missing 0' in first
        server_view(server_home, col.home, collector_id)
        again = pushed(col.push(tokens['push']))
        assert 'up to date (batches 0, bytes sent 0, state requests 1)' in again
        log.append(again)

        # 2. the client: search, snippets and knowledge with sources, all binding back to the collector
        async def read_and_write(client: Client):
            status = await client.call('status')
            hits = []
            for query in (SENTINEL, LINES_KO[1], LINES_KO[2][:8], 'schedule', '견적'):
                for mode in ('lexical', 'hybrid'):
                    hits += (await client.call('search', query=query, mode=mode, limit=10))['result']['hits']
            for hit in hits[:3]:
                await client.call('get_snippet', chunk_id=hit['chunk_id'], context_units=1)
            await client.call('get_snippet', locator=hits[0]['locator'])
            docx = (await client.call('search', query=LINES_KO[1], ext=['.docx'], limit=10))['result']['hits']
            notes = (await client.call('search', query=SENTINEL, ext=['.txt'], limit=10))['result']['hits']
            cited_docx, cited_notes = hit_in(docx, CHANGED), hit_in(notes, NOTES, line=1)
            entity = (await client.call('record_entity', kind='project', name=f'합성 과제 {SENTINEL}'))['result']
            facts = []
            for hit, predicate in ((cited_docx, '견적'), (cited_notes, '회의')):
                answer = await client.call('record_fact', entity_id=entity['entity']['id'], predicate=predicate,
                                           value=f'{SENTINEL} 값',
                                           sources=[{'locator': hit['locator'], 'quote': hit['snippet']}])
                assert answer['ok'], answer
                facts.append(answer['result']['assertion']['id'])
            return status['result'], hits, entity['entity']['id'], facts

        (status, hits, entity_id, facts), envelopes = session(server, tokens['mcp'], read_and_write)
        assert status['role'] == 'server' and status['last_push']['collector'] == COLLECTOR
        assert status['collectors'][0]['name'] == COLLECTOR and status['collectors'][0]['documents'] > 0
        assert len(hits) >= 10 and all(envelope['ok'] for envelope in envelopes)
        pieces = check_pieces(envelopes, server_home, col.home)
        assert pieces >= 10
        log.append(f'session 1: {len(envelopes)} calls, {pieces} delivered pieces bind to the server store, the '
                   f'collector store and its originals (search {len(hits)} hits, get_snippet 4, record_fact 2)')

        # 3. change one file, delete another: only those go, the client sees them, the cited source is changed
        original = {rel: (col.src/rel).read_bytes() for rel in (CHANGED, DELETED)}
        rewrite(col.src/CHANGED, other_docx())
        (col.src/DELETED).unlink()
        col.collect()
        changed = pushed(col.push(tokens['push']))
        c = counts(changed)
        assert (c['added'], c['changed'], c['deleted'], c['documents'], c['units']) == (0, 1, 1, 2, 1), changed
        assert 'up to date' in pushed(col.push(tokens['push']))
        server_view(server_home, col.home, collector_id)
        log += ['# after changing docs/report.docx and deleting mail/plain_utf8.eml', changed]

        async def after_change(client: Client):
            recent = (await client.call('recent_changes', kinds=['changed', 'deleted']))['result']['changes']
            entity = (await client.call('get_entity', entity_id=entity_id))['result']
            found = (await client.call('search', query=SENTINEL, ext=['.docx'], limit=10))['result']['hits']
            return recent, entity, found

        (recent, entity, found), envelopes = session(server, tokens['mcp'], after_change)
        assert {(row['kind'], row['doc']['path'].replace('\\', '/').split('/collector-src/')[-1]) for row in recent} >= {
            ('changed', CHANGED), ('deleted', DELETED)}
        states = {item['id']: item['source_state'] for item in entity['facts']}
        assert states == {facts[0]: 'changed', facts[1]: 'current'}
        assert any(hit['snippet'].startswith(SENTINEL) and hit['locator']['path'].endswith('report.docx')
                   for hit in found)                                # the new text is searchable on the server
        check_pieces(envelopes, server_home, col.home)

        # 4. restore the deleted file and revert the changed one: the server copies units it holds (0 sent)
        (col.src/DELETED).write_bytes(original[DELETED])
        rewrite(col.src/CHANGED, original[CHANGED])
        col.collect()
        reverted = pushed(col.push(tokens['push']))
        c = counts(reverted)
        assert (c['restored'], c['changed'], c['units'], c['copied']) == (1, 1, 0, 2), reverted
        assert 'up to date' in pushed(col.push(tokens['push']))
        server_view(server_home, col.home, collector_id)
        log += ['# after restoring the deleted file and reverting report.docx', reverted]

        # 5. both binding checks: the collector re-derives from the originals, the server checks its stored text
        local = run_cli(col.home, 'verify')
        stored = run_cli(server_home, 'verify', '--stored')
        assert local.returncode == 0 and 'binding ok' in local.stdout, local.stdout + local.stderr
        assert stored.returncode == 0 and stored.stdout.startswith('verify (stored text only') \
            and 'binding ok' in stored.stdout, stored.stdout + stored.stderr
        log += ['$ workmcp verify   (collector)', local.stdout, '$ workmcp verify --stored   (server)', stored.stdout]

        # 6. a reparse refills line 1 of the notes in place: units go again, the cited quote is stale on the server
        from workmcp.parsers import text as text_parser
        real = text_parser.parse

        def parse(data, limits, budget):
            units, encoding = real(data, limits, budget)
            return [dataclasses.replace(u, text='합성 재파싱 ' + u.text) if u.unit.get('line') == 1 else u
                    for u in units], encoding
        monkeypatch.setattr(text_parser, 'parse', parse)
        monkeypatch.setattr(text_parser, 'VERSION', 'text-reparse-e2e')
        col.collect(reparse=True)
        reparsed = pushed(col.push(tokens['push']))
        assert counts(reparsed)['reparsed'] >= 2 and counts(reparsed)['units'] > 0, reparsed
        assert 'up to date' in pushed(col.push(tokens['push']))
        server_view(server_home, col.home, collector_id)
        stale = run_cli(server_home, 'verify', '--stored')
        assert stale.returncode == 0 and 'stale after reparse 1' in stale.stdout, stale.stdout + stale.stderr
        log += ['# after scan --reparse (line 1 of the notes changed by a parser change)', reparsed,
                '$ workmcp verify --stored   (server)', stale.stdout]

        async def after_reparse(client: Client):
            return (await client.call('get_entity', entity_id=entity_id))['result']
        entity, _ = session(server, tokens['mcp'], after_reparse)
        assert {item['id']: item['source_state'] for item in entity['facts']}[facts[1]] == 'changed'
    finally:
        code, stderr = server.stop()
    tr.assert_stopped(code, stderr)
    assert tr.tracebacks(stderr) == 0 and SENTINEL not in stderr, stderr
    everything = '\n'.join(log) + stderr
    assert all(token not in everything for token in tokens.values())
    assert all(token not in tr.dump(server_home) for token in tokens.values())
    print('\n'.join(log))


def test_a_large_version_goes_over_http_in_parts(tmp_path):
    """One text of ~2.6 Mi characters in 25,000 units: more than one units part (design §3.1-4), assembled on the
    server (no part left over), the same units and chunks there, and searchable through the client."""
    server_home, tokens, collector_id = setup_server(tmp_path)
    server = tr.start_server(server_home, tokens['mcp'])
    src = tmp_path/'big-src'
    src.mkdir()
    line = '합성 문장 하나가 길게 이어진다. ' * 5
    (src/'big.txt').write_text('\n'.join(f'{n:06d} {line}' for n in range(25_000)), encoding='utf-8')
    col = Collector(tmp_path, server.port, src=src, bundle=False)
    col.collect()
    try:
        report = pushed(col.push(tokens['push']))
        assert 'not yet the same' not in report and counts(report)['units'] == 25_000, report
        srv = store.open_read(server_home)
        try:
            parts = [json.loads(row[0]) for row in srv.execute(
                "SELECT response_json FROM push_batches WHERE kind = 'units' ORDER BY applied_at")]
            assert len(parts) >= 2 and srv.execute('SELECT COUNT(*) FROM push_parts').fetchone()[0] == 0
            largest = srv.execute('SELECT MAX(bytes_in) FROM push_batches').fetchone()[0]
        finally:
            srv.close()
        assert largest <= sync.BATCH_MAX_BYTES
        server_view(server_home, col.home, collector_id)

        async def find(client: Client):
            return (await client.call('search', query='024999 합성 문장', mode='lexical', limit=3))['result']['hits']
        hits, envelopes = session(server, tokens['mcp'], find)
        assert hits and hits[0]['locator']['unit']['line'] == 25_000
        check_pieces(envelopes, server_home, col.home)
    finally:
        code, stderr = server.stop()
    tr.assert_stopped(code, stderr)
    print(report.strip(), f'\nunits batches {len(parts)}, largest batch on the wire {largest:,} bytes')


# ---- both audit modes on one push ---------------------------------------------------------------------------

def test_audited_server_receives_an_audited_push(tmp_path):
    """Design §6.2 and §6.3 at once: ``serve --http`` under the ``remote`` audit receives ``workmcp push`` (twice)
    run under the ``push`` audit, then answers an MCP session and the refusals. Server: every socket event on
    loopback, its own listener the only fixed-port bind, no name resolution but the numeric loopback host, no
    HTTP client, no process, no original opened. Collector: destinations == the configured server, no original
    opened, its store opened read-only, the second run sends nothing, no token in its audit log."""
    server_home, tokens, collector_id = setup_server(tmp_path)
    col = Collector(tmp_path, port := nb._free_port())
    col.collect()
    (col.home/paths.PUSH_TOKEN_FILE).write_text(tokens['push'], encoding='utf-8')   # the push mode reads the file
    server_log, stop, server_err = tmp_path/'audit-remote.json', tmp_path/'remote.stop', tmp_path/'remote.err'
    with open(server_err, 'w', encoding='utf-8') as err:
        proc = subprocess.Popen([sys.executable, str(WRAPPER), str(server_log), 'remote', str(port), str(stop),
                                 str(col.src)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                env=clean_env(WORKMCP_HOME=str(server_home), PYTHONPATH=str(ROOT/'src')),
                                cwd=tmp_path)
    tr.LAUNCHED.append(proc)
    base = f'http://127.0.0.1:{port}/workmcp'
    try:
        deadline = time.monotonic() + tr.READY_SECONDS
        with tr.client(tokens['mcp']) as http:
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    if http.get(f'{base}/healthz', timeout=tr.READY_PROBE_SECONDS).status_code == 200:
                        break
                except httpx2.TransportError:
                    time.sleep(0.2)
        assert proc.poll() is None, server_err.read_text(encoding='utf-8', errors='replace')
        push_log = tmp_path/'audit-push.json'
        audited_push = subprocess.run([sys.executable, str(WRAPPER), str(push_log), 'push', str(port), str(col.src)],
                                      env=clean_env(WORKMCP_HOME=str(col.home), PYTHONPATH=str(ROOT/'src')),
                                      cwd=tmp_path, capture_output=True, text=True, encoding='utf-8',
                                      errors='replace', timeout=PUSH_SECONDS)
        assert audited_push.returncode == 0, audited_push.stderr
        seen = nb._remote_requests(base, tokens)
        stop.write_text('stop', encoding='utf-8')
        code = proc.wait(timeout=tr.STOP_SECONDS * 3)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=tr.STOP_SECONDS)
    stderr = server_err.read_text(encoding='utf-8', errors='replace')
    server_report = nb.remote_report(code, server_log, stderr)
    push_report = json.loads(push_log.read_text(encoding='utf-8'))
    # the collector: the configured server only, no original opened, read-only store, second run sends nothing
    assert push_report['completed'] is True and [e for e in push_report['events'] if e['violation']] == []
    assert push_report['exit_codes'] == [0, 0] and push_report['source_opens'] == 0
    assert push_report['connects'] and all('mode=ro' in target for target in push_report['connects'])
    assert push_report['destinations'] == [f'127.0.0.1:{port}']
    assert push_report['first']['batches'] > 0 and push_report['first']['converged']
    assert push_report['second']['up_to_date'] and (push_report['second']['batches'],
                                                    push_report['second']['bytes_sent']) == (0, 0)
    # the server: no egress while receiving the push and answering the client; refusals
    events = server_report['events']
    assert [e for e in events if e['violation']] == [], stderr
    for e in events:
        if e['event'] in _audited().ADDRESS_EVENTS:
            assert nb.LOOPBACK_DETAIL.search(e['detail']), e
    binds = [e for e in events if e['event'] == 'socket.bind']
    assert not [e for e in binds if not re.search(rf"', (0|{port})[,)]", e['detail'])]
    lookups = [e for e in events if e['event'].startswith(('socket.getaddrinfo', 'socket.gethostbyname'))]
    assert all(nb.LOOPBACK_LOOKUP.match(e['detail']) for e in lookups)
    assert not [e for e in events if e['event'].startswith(_audited().PROCESS_EVENTS + _audited().NETWORK_EVENTS)]
    assert server_report['source_opens'] == 0
    assert {name: seen[name] for name in ('R-1', 'R-2', 'R-3', 'R-4', 'R-5', 'R-7', 'R-8', 'R-9')} == {
        'R-1': 401, 'R-2': 401, 'R-3': 401, 'R-4': 401, 'R-5': 401, 'R-7': 421, 'R-8': 401, 'R-9': [404]}
    replies = [json.loads(reply['result']['content'][0]['text']) for reply in seen['R-6']]
    assert replies[0]['result']['last_push']['collector'] == COLLECTOR and replies[1]['result']['hits']
    server_view(server_home, col.home, collector_id)
    for text in (push_log.read_text(encoding='utf-8'), server_log.read_text(encoding='utf-8'), stderr,
                 audited_push.stdout, audited_push.stderr):
        assert all(token not in text for token in tokens.values())
    summary = {
        'push audit': {key: push_report[key] for key in ('exit_codes', 'source_opens', 'destinations')}
        | {'connects': sorted({re.sub(r'^.*[\\/]', '', t) for t in push_report['connects']}),
           'first': push_report['first'], 'second': push_report['second'],
           'violations': len([e for e in push_report['events'] if e['violation']]),
           'events': len(push_report['events'])},
        'remote audit': {'exit_code': server_report.get('exit_code', 'stalled'), 'source_opens': server_report['source_opens'],
                         'violations': len([e for e in events if e['violation']]), 'events': len(events),
                         'event kinds': sorted({e['event'] for e in events}),
                         'binds': sorted({re.sub(r'^\(<[^>]*>, ', '(', e['detail'])[:40] for e in binds}),
                         'lookups': sorted({e['detail'][:30] for e in lookups})},
        'requests': {name: seen[name] for name in ('R-1', 'R-2', 'R-3', 'R-4', 'R-5', 'R-7', 'R-8', 'R-9')}
        | {'R-6 status last_push': replies[0]['result']['last_push'], 'R-6 search hits': len(replies[1]['result']['hits']),
           'push_state': seen['push_state']},
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def _audited():
    import _audited_server
    return _audited_server

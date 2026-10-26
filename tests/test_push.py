"""``workmcp push`` against an in-memory operating server (design M5 remote §3, §6.3, §6.4, §8).

The server side is ``receive.dispatch`` on its own store, called directly by a fake transport (no HTTP, no
socket); the collector side is ``push.execute`` over a store filled by ``collect.scan`` and
``index.run_index(HashEmbedder(1024))`` from the synthetic bundle. The runtime audit at the end runs the real
``workmcp push`` under the audit hook against a loopback test server. Synthetic data only; the push token here
is a fixed synthetic string, never a real one.
"""
import dataclasses
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from fixtures import LINES_EN, LINES_KO, SENTINEL
from fixtures import bundle as synthetic_bundle
from test_collect import CHANGED, DELETED, add_root, other_docx, rewrite, scan
from workmcp import (binding, chunks, cli, config, embedder, index, knowledge_query, locator, push, receive, snippet,
                     store, sync)
from workmcp import paths as paths_module
from workmcp import search as search_module

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = Path(__file__).resolve().parent/'_audited_server.py'
URL = 'https://srv.example.invalid/workmcp'
TOKEN = 'synthetic-push-token-0000000000000000000000'
TABLES = ('documents', 'versions', 'units', 'chunks', 'document_events', 'source_roots')
NO_SLEEP = []


def make_home(path: Path) -> Path:
    return paths_module.ensure_home(paths_module.validate_home(str(path), {}, None))


def settings(url: str = URL) -> config.Config:
    return config.Config(file=Path('config.toml'), exists=False, scan=config.ScanSettings(), sources=(),
                         push=config.PushSettings(url=url))


class Server:
    """An operating server store and a fake transport into ``receive.dispatch``."""

    def __init__(self, tmp_path: Path):
        self.home = make_home(tmp_path/'server-home')
        self.db = store.open_home(self.home)
        self.bodies: list[dict] = []          # every batch body as the server received it
        self.requests: list[tuple[str, str]] = []
        self.hook = None                      # hook(method, name, body, call) -> (status, bytes)

    def collector(self, name: str) -> dict:
        cid = self.db.execute('INSERT INTO collectors(name, created_at) VALUES(?, ?)', (name, store.now())).lastrowid
        return {'id': cid, 'name': name, 'retired_at': None}

    def transport(self, collector: dict, url: str = URL):
        def call(method, name, query, data):
            status, answer = receive.dispatch(self.db, collector, method, name, query, data)
            return status, json.dumps(answer, ensure_ascii=False).encode('utf-8')

        def send(method: str, target: str, data: bytes | None):
            assert target.startswith(url + '/'), target
            path, _, query = target[len(url) + 1:].partition('?')
            name = {'push/state': 'state', 'push/state/vectors': 'vectors', 'push/batch': 'batch'}[path]
            values = dict(pair.split('=', 1) for pair in query.split('&') if pair)
            body = sync.ungzip_json(data) if data is not None else None
            self.requests.append((method, name))
            if body is not None:
                self.bodies.append(body)
            if self.hook is not None:
                return self.hook(method, name, body, lambda: call(method, name, values, data))
            return call(method, name, values, data)
        return send

    def count(self, table: str) -> int:
        return self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def close(self):
        self.db.close()


class Collector:
    def __init__(self, tmp_path: Path, name: str = 'collector', src: Path | None = None):
        self.name = name
        self.home = make_home(tmp_path/f'{name}-home')
        self.db = store.open_home(self.home)
        self.src = src or tmp_path/f'{name}-src'
        self.root = add_root(self.db, self.src)
        self.sleeps: list[float] = []

    def write_bundle(self):
        return synthetic_bundle.write_bundle(self.src)

    def collect(self, **options):
        result = scan(self.db, self.root, **options)
        index.run_index(self.db, self.home, embedder=embedder.HashEmbedder(1024))
        return result

    @property
    def key(self) -> str:
        return push.local_model_key(self.db)

    def push(self, server: Server, collector: dict, **options) -> dict:
        return push.execute(self.home, settings(), TOKEN, push.Options(**options),
                            transport=server.transport(collector), sleep=self.sleeps.append)

    def count(self, table: str) -> int:
        return self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def close(self):
        self.db.close()


@pytest.fixture
def pair(tmp_path):
    server = Server(tmp_path)
    col = Collector(tmp_path)
    col.write_bundle()
    col.collect()
    yield col, server, server.collector('합성-수집기')
    col.close()
    server.close()


def mirrored(col: Collector, server: Server, collector: dict) -> None:
    """The server holds what the collector holds: summaries, row counts, and every row's content."""
    cid = collector['id']
    assert sync.summary(server.db, col.key, collector_id=cid) == sync.summary(col.db, col.key)
    for table in TABLES:
        assert server.count(table) == col.count(table), table
    assert server.db.execute('SELECT COUNT(*) FROM chunk_vectors WHERE model_key=?', (col.key,)).fetchone()[0] == \
        col.db.execute('SELECT COUNT(*) FROM chunk_vectors WHERE model_key=?', (col.key,)).fetchone()[0]
    docs = dict(server.db.execute('SELECT local_document_id, document_id FROM collector_documents WHERE collector_id=?',
                                  (cid,)))
    vers = dict(server.db.execute('SELECT local_version_id, version_id FROM collector_versions WHERE collector_id=?',
                                  (cid,)))
    columns = 'path, path_key, ext, status, skip_reason, first_seen_at, deleted_at'
    for local, remote in docs.items():
        assert col.db.execute(f'SELECT {columns} FROM documents WHERE id=?', (local,)).fetchone() == \
            server.db.execute(f'SELECT {columns} FROM documents WHERE id=?', (remote,)).fetchone()
    columns = ', '.join(receive.VERSION_COLUMNS)
    unit_sql = 'SELECT ordinal, kind, locator_json, text, text_sha256 FROM units WHERE version_id=? ORDER BY ordinal'
    chunk_sql = ('SELECT chunk_version, ordinal, unit_first, unit_last, char_start, char_end, text, text_sha256 FROM chunks '
                 'WHERE version_id=? ORDER BY chunk_version, ordinal')
    for local, remote in vers.items():
        for sql in (f'SELECT {columns} FROM versions WHERE id=?', unit_sql, chunk_sql):
            assert col.db.execute(sql, (local,)).fetchall() == server.db.execute(sql, (remote,)).fetchall(), (sql, local)
    back_doc = {v: k for k, v in docs.items()}
    back_ver = {v: k for k, v in vers.items()}
    local_events = sorted(col.db.execute('SELECT document_id, version_id, kind, at FROM document_events'),
                          key=repr)
    server_events = sorted(((back_doc[d], back_ver.get(v), k, a) for d, v, k, a in server.db.execute(
        'SELECT document_id, version_id, kind, at FROM document_events')), key=repr)
    assert server_events == local_events
    assert store.fts_status(server.db)['consistent']


def walk_strings(value, path=''):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from walk_strings(inner, key)
    elif isinstance(value, list):
        for inner in value:
            yield from walk_strings(inner, path)
    elif isinstance(value, str):
        yield path, value


def wire_keys_only(body: dict) -> None:
    assert sync.body_problems(body) == []
    for key, text in walk_strings(body['items']):
        if len(text) >= 64 and not sync.is_sha256(text):
            assert key in ('text', 'locator_json', 'vector_b64', 'path', 'path_key'), key
        if key == 'vector_b64':
            assert len(text) == sync.VECTOR_B64_CHARS


def test_first_push_sends_everything_second_push_sends_nothing(pair, tmp_path):
    col, server, collector = pair
    first = col.push(server, collector)
    assert set(first) == set(push.REPORT_KEYS)
    assert first['exit_code'] == 0 and first['converged'] and not first['refused'] and first['error'] is None
    documents = col.count('documents')
    assert first['state']['added'] == first['state']['documents_changed'] == documents
    sent = first['sent']
    assert sent['documents'] == documents and sent['versions'] == col.count('versions')
    assert sent['units'] == col.count('units')
    assert sent['chars'] == col.db.execute('SELECT SUM(LENGTH(text)) FROM units').fetchone()[0]
    assert sent['vectors'] == col.db.execute('SELECT COUNT(*) FROM chunk_vectors').fetchone()[0] > 0
    assert sent['vector_bytes'] == sent['vectors'] * 4096 and sent['events'] == col.count('document_events')
    assert first['bytes_sent'] > 0 and first['batches'] == len(server.bodies)
    assert [body['kind'] for body in server.bodies][0] == 'begin' and server.bodies[-1]['kind'] == 'end'
    mirrored(col, server, collector)
    assert first['server']['vectors_missing'] == 0
    # what left the PC: wire keys only, text only as unit text, no file bytes (design §6.4)
    unit_text = 0
    for body in server.bodies:
        wire_keys_only(body)
        raw = sync.canonical(body)
        assert b'%PDF' not in raw and b'PK\x03\x04' not in raw and b'\xd0\xcf\x11\xe0' not in raw
        if body['kind'] == 'units':
            unit_text += sum(len(unit['text'].encode('utf-8')) for item in body['items'] for unit in item['units'])
    assert unit_text == col.db.execute('SELECT SUM(LENGTH(CAST(text AS BLOB))) FROM units').fetchone()[0]
    # the server rebuilt the chunks itself: no chunk text on the wire
    assert all(body['kind'] != 'chunks' for body in server.bodies)

    server.bodies.clear()
    second = col.push(server, collector)
    assert second['up_to_date'] and second['exit_code'] == 0
    assert (second['batches'], second['bytes_sent'], second['state_requests']) == (0, 0, 1)
    assert server.bodies == [] and server.requests[-1] == ('GET', 'state')
    lines = push.report_lines(second)
    assert lines == [f'push to {URL} as 합성-수집기  up to date (batches 0, bytes sent 0, state requests 1)']
    # the log and the report name paths, hashes and counts only: no unit text, no token
    log = Path(first['log']).read_text(encoding='utf-8')
    assert 'pushed | ' in log and 'vectors | ' in log
    for text in (log, '\n'.join(push.report_lines(first)), json.dumps(first, ensure_ascii=False)):
        assert SENTINEL not in text and TOKEN not in text
    assert store.last_push(server.db)['collector'] == '합성-수집기'
    assert store.last_scan(server.db)['added'] == documents


def test_summary_hashes_match_across_sides_and_one_change_moves_documents_only(pair):
    col, server, collector = pair
    col.push(server, collector)
    cid = collector['id']
    before = sync.summary(col.db, col.key)
    assert sync.summary(server.db, col.key, collector_id=cid) == before
    rewrite(col.src/'docs/notes_utf8.txt', '합성 변경 한 줄\n'.encode('utf-8'))
    scan(col.db, col.root)                            # no index yet: the chunk set and vectors stay as they were
    after = sync.summary(col.db, col.key)
    assert after['documents_sha256'] != before['documents_sha256']
    col.push(server, collector)
    assert sync.summary(server.db, col.key, collector_id=cid) == sync.summary(col.db, col.key)


def units_sent(server: Server) -> int:
    return sum(len(item['units']) for body in server.bodies if body['kind'] == 'units' for item in body['items'])


def kinds_of(server: Server) -> list[str]:
    return [body['kind'] for body in server.bodies]


def test_change_delete_restore_revert_reparse_sync(pair, monkeypatch):
    col, server, collector = pair
    col.push(server, collector)
    src = col.src
    original = {rel: (src/rel).read_bytes() for rel in (CHANGED, DELETED)}

    def step(expect_kinds):
        server.bodies.clear()
        col.collect()
        report = col.push(server, collector)
        assert report['exit_code'] == 0 and report['converged'], report
        mirrored(col, server, collector)
        events, _ = store.document_events_query(server.db, {}, 5, 0)
        assert expect_kinds <= {event['kind'] for event in events}
        assert col.push(server, collector)['up_to_date']
        return report

    # change: a new version (seq 2), units sent
    rewrite(src/CHANGED, other_docx())
    changed = step({'changed'})
    assert changed['state']['changed'] == 1 and changed['sent']['units'] == 1
    # delete: status only, no units
    (src/DELETED).unlink()
    deleted = step({'deleted'})
    assert deleted['state']['deleted'] == 1 and deleted['sent']['units'] == 0 and 'units' not in kinds_of(server)
    doc = server.db.execute("SELECT status FROM documents WHERE path_key LIKE ?", ('%plain_utf8.eml',)).fetchone()
    assert doc == ('deleted',)
    # restore: seq + 1 with the same bytes; the server copies the units it already has (nothing sent)
    (src/DELETED).write_bytes(original[DELETED])
    restored = step({'restored'})
    assert restored['state']['restored'] == 1 and restored['sent']['copied_versions'] == 1
    assert units_sent(server) == 0 and 'units' not in kinds_of(server)
    # revert A -> B -> A: a third version, units copied on the server
    rewrite(src/CHANGED, original[CHANGED])
    reverted = step({'changed'})
    assert reverted['sent']['copied_versions'] == 1 and units_sent(server) == 0

    # a source on the server cites line 1 of the notes; a reparse changes that line in place
    notes = server.db.execute("SELECT d.id, d.path, v.id, v.sha256 FROM documents d JOIN versions v ON "
                              "v.document_id = d.id WHERE d.path_key LIKE ?", ('%notes_utf8.txt',)).fetchone()
    doc_id, path, version_id, sha = notes
    unit_json, text = server.db.execute('SELECT locator_json, text FROM units WHERE version_id=? AND ordinal=1',
                                        (version_id,)).fetchone()
    quote = text[:10]
    obj = locator.make(doc_id, path, sha, json.loads(unit_json), text, [0, 10])
    server.db.execute("INSERT INTO assertions(kind, statement, recorded_from, status, source_kind, recorded_by) "
                      "VALUES('decision', '합성 결정', '2026-01-01T09:00:00+09:00', 'active', 'evidence', 'test')")
    server.db.execute('INSERT INTO evidence(assertion_id, version_id, chunk_id, locator_json, quote, quote_sha256) '
                      'VALUES(1, ?, NULL, ?, ?, ?)', (version_id, json.dumps(obj, ensure_ascii=False), quote,
                                                       binding._sha(quote)))
    assert binding.verify(server.db, stored_only=True)['ok']
    from workmcp.parsers import text as text_parser
    real = text_parser.parse

    def parse(data, limits, budget):
        units, encoding = real(data, limits, budget)
        return [dataclasses.replace(u, text='합성 재파싱 ' + u.text) if u.unit.get('line') == 1 else u
                for u in units], encoding
    monkeypatch.setattr(text_parser, 'parse', parse)
    monkeypatch.setattr(text_parser, 'VERSION', 'text-reparse-push-test')
    server.bodies.clear()
    result = col.collect(reparse=True)
    assert result['total']['reparsed'] >= 2                   # the notes and their copy
    report = col.push(server, collector)
    assert report['exit_code'] == 0 and report['converged'] and report['state']['reparsed'] >= 2
    assert units_sent(server) > 0
    mirrored(col, server, collector)
    assert {e['kind'] for e in store.document_events_query(server.db, {'kinds': ['reparsed']}, 10, 0)[0]} == \
        {'reparsed'}
    stored = binding.verify(server.db, stored_only=True)
    assert stored['ok'] and stored['evidence_stale_after_reparse'] == 1
    assert not knowledge_query.still_matches(server.db, version_id, obj, quote)
    assert knowledge_query.source_state('active', None, matches=False) == 'changed'
    assert col.push(server, collector)['up_to_date']


def test_interrupted_push_resumes_idempotently(pair):
    col, server, collector = pair
    posts = []

    def lost_answer(method, name, body, call):
        """The third batch is applied but its answer is lost (500): the retry gets the stored answer."""
        posts.append(body and body['batch_id'])
        status, raw = call()
        if body is not None and len([p for p in posts if p]) == 3 and posts.count(body['batch_id']) == 1:
            return 500, b'{"ok": false}'
        return status, raw
    server.hook = lost_answer
    report = col.push(server, collector)
    assert report['exit_code'] == 0 and report['retries'] == 1 and col.sleeps == [1]
    batch_ids = [body['batch_id'] for body in server.bodies]
    assert len(batch_ids) == report['batches'] + 1 == len(set(batch_ids)) + 1     # one batch sent twice
    assert server.count('push_batches') == report['batches']                       # ... and applied once
    mirrored(col, server, collector)

    # a run that dies after a few batches (server gone) leaves a running push; the next run converges
    rewrite(col.src/CHANGED, other_docx())
    (col.src/DELETED).unlink()
    col.collect()
    sent = []

    def gone(method, name, body, call):
        if body is not None:
            sent.append(body['kind'])
            if len(sent) > 2:
                raise push.Unreachable('ConnectionRefusedError')
        return call()
    server.hook = gone
    col.sleeps.clear()
    dead = col.push(server, collector)
    assert dead['exit_code'] == push.EXIT_UNREACHABLE and 'unreachable' in dead['error']
    assert col.sleeps == list(push.RETRY_DELAYS)
    assert server.db.execute("SELECT COUNT(*) FROM pushes WHERE status='running'").fetchone()[0] == 1
    server.hook = None
    again = col.push(server, collector)
    assert again['exit_code'] == 0 and again['converged']
    assert server.db.execute("SELECT status FROM pushes ORDER BY id").fetchall()[-2:] == [('aborted',), ('done',)]
    mirrored(col, server, collector)
    assert col.push(server, collector)['up_to_date']


def test_large_version_is_split_into_parts_and_long_units_into_pieces(tmp_path):
    server = Server(tmp_path)
    collector = server.collector('합성-큰문서')
    col = Collector(tmp_path)
    line = '합성 문장 하나가 길게 이어진다. ' * 5                 # 100 characters
    lines = [f'{n:06d} {line}' for n in range(30_000)]              # ~3.2 Mi characters in 30,000 units
    lines.append('긴줄' * (sync.UNIT_PIECE_CHARS // 2 + 1000))     # one unit longer than a piece
    (col.src/'big.txt').write_text('\n'.join(lines), encoding='utf-8')
    col.collect()
    try:
        report = col.push(server, collector)
        assert report['exit_code'] == 0 and report['converged'], report
        items = [item for body in server.bodies if body['kind'] == 'units' for item in body['items']]
        assert {item['parts'] for item in items} == {len(items)} and len(items) >= 2
        assert all(len(item['units']) <= sync.UNITS_PER_BATCH for item in items)
        assert all(sum(len(u['text']) for u in item['units']) <= sync.UNIT_CHARS_PER_BATCH for item in items)
        assert all(sync.canonical_size(body) <= sync.BATCH_MAX_BYTES for body in server.bodies)
        ordinals = [unit['ordinal'] for item in items for unit in item['units']]
        assert len(ordinals) > len(set(ordinals)) == col.count('units')         # the long unit went in pieces
        assert server.count('push_parts') == 0                                  # assembled and cleared
        mirrored(col, server, collector)
    finally:
        col.close()
        server.close()


def test_dry_run_limit_docs_and_max_mb(pair):
    col, server, collector = pair
    dry = col.push(server, collector, dry_run=True)
    assert dry['exit_code'] == 0 and dry['dry_run'] and dry['batches'] == dry['bytes_sent'] == 0
    assert dry['state']['added'] == col.count('documents') and server.count('documents') == 0
    assert server.bodies == [] and '(dry run: nothing sent)' in push.report_lines(dry)[0]
    limited = col.push(server, collector, limit_docs=5)
    assert limited['exit_code'] == 0 and limited['sent']['documents'] == 5 and '--limit-docs' in limited['error']
    assert server.count('documents') == 5 and not limited['converged']
    small = col.push(server, collector, max_mb=1)
    assert small['exit_code'] == 0
    final = col.push(server, collector)
    assert final['exit_code'] == 0 and final['converged']
    mirrored(col, server, collector)


def answer(code: str, status: int | None = None):
    return lambda method, name, body, call: (status or sync.ERROR_HTTP_STATUS[code],
                                             json.dumps(sync.error(code, 'synthetic')).encode())


def test_retry_only_what_may_succeed_later(pair):
    col, server, collector = pair
    server.hook = answer('store_busy')
    busy = col.push(server, collector)
    assert busy['exit_code'] == push.EXIT_FAILED and busy['retries'] == len(push.RETRY_DELAYS)
    assert col.sleeps == [1, 2, 4, 8, 16] and 'store_busy' in busy['error']
    col.sleeps.clear()
    for code, exit_code in (('invalid_token', push.EXIT_FAILED), ('schema_version_mismatch', push.EXIT_USAGE),
                            ('collector_retired', push.EXIT_FAILED)):
        server.hook = answer(code)
        refused = col.push(server, collector)
        assert refused['exit_code'] == exit_code and refused['retries'] == 0 and code in refused['error']
    assert col.sleeps == []
    server.hook = lambda method, name, body, call: (502, b'<html>bad gateway</html>')
    gateway = col.push(server, collector)
    assert gateway['exit_code'] == push.EXIT_FAILED and gateway['retries'] == 5 and 'HTTP 502' in gateway['error']
    server.hook = None
    assert col.push(server, collector)['converged']


def test_a_server_of_another_chunk_version_is_refused_before_any_batch(pair, monkeypatch):
    col, server, collector = pair
    real = receive.state
    monkeypatch.setattr(receive, 'state', lambda *a, **k: {**real(*a, **k), 'chunk_version': 'other-v0'})
    report = col.push(server, collector)
    assert report['exit_code'] == push.EXIT_USAGE and 'update the server first' in report['error']
    assert server.bodies == []


def test_require_vectors(tmp_path):
    server = Server(tmp_path)
    collector = server.collector('합성-벡터없음')
    col = Collector(tmp_path)
    (col.src/'a.txt').write_text('\n'.join(LINES_KO), encoding='utf-8')
    scan(col.db, col.root)
    index.run_index(col.db, col.home)                               # chunks only: no vectors
    try:
        assert col.push(server, collector)['exit_code'] == 0
        report = col.push(server, collector, require_vectors=True)
        assert report['exit_code'] == push.EXIT_FAILED and '--require-vectors' in report['error']
    finally:
        col.close()
        server.close()


def test_server_hits_bind_to_collector_originals(pair):
    """Design §5.3 (in memory): a server search hit -> its locator -> the collector's locator -> the original."""
    col, server, collector = pair
    assert col.push(server, collector)['converged']
    back = dict(server.db.execute('SELECT document_id, local_document_id FROM collector_documents'))
    engine = search_module.Engine(server.home, embedder_factory=lambda: embedder.HashEmbedder(1024))
    hits = []
    for query in (LINES_KO[1][:8], LINES_KO[2][:6], LINES_EN[1][:12], '견적', 'schedule'):
        for mode in ('lexical', 'hybrid', 'semantic'):
            hits += engine.search(query, mode=mode, limit=10)['hits']
    assert len(hits) >= 10
    for hit in hits:
        obj = hit['locator']
        served = binding.validate_locator(server.db, obj)['text']
        assert served == hit['snippet']
        local = {**obj, 'doc': back[obj['doc']]}
        assert binding.validate_locator(col.db, local)['text'] == hit['snippet']
        assert snippet.by_locator(server.db, obj)['pieces'][0]['text'] == hit['snippet']
        for key in ('path', 'sha256', 'unit', 'chars', 'text_sha256'):
            assert local[key] == obj[key]
    assert binding.verify(col.db)['ok']                             # the collector re-derives from the originals
    stored = binding.verify(server.db, stored_only=True)
    assert stored['ok'] and stored['versions']['checked'] == col.db.execute(
        "SELECT COUNT(*) FROM versions WHERE parse_status='ok'").fetchone()[0]


# ---- CLI --------------------------------------------------------------------------------------------------

def test_cli_push_exit_codes_and_report(tmp_path, workmcp_home, monkeypatch, capsys):
    home = make_home(workmcp_home)
    db = store.open_home(home)
    src = tmp_path/'src'
    (src/'a').mkdir(parents=True)
    (src/'a'/'notes.txt').write_text('\n'.join(LINES_KO), encoding='utf-8')
    scan(db, add_root(db, src))
    db.close()
    assert cli.main(['push']) == 4 and '[push] url is not set' in capsys.readouterr().err
    (home/'config.toml').write_text('[push]\nurl = "http://127.0.0.1:9/workmcp"\n', encoding='utf-8')
    assert cli.main(['push']) == 4 and 'allow-insecure-loopback' in capsys.readouterr().err
    (home/'config.toml').write_text(f'[push]\nurl = "{URL}"\n', encoding='utf-8')
    monkeypatch.delenv(paths_module.PUSH_TOKEN_ENV, raising=False)
    assert cli.main(['push']) == 4 and 'push token file not found' in capsys.readouterr().err
    assert cli.main(['push', '--max-mb', '0']) == 4
    (home/paths_module.PUSH_TOKEN_FILE).write_text(TOKEN + '\n', encoding='utf-8')
    server = Server(tmp_path)
    collector = server.collector('합성-cli')
    seen = []
    monkeypatch.setattr(push, 'http_transport', lambda token, **k: seen.append(token) or server.transport(collector))
    assert cli.main(['push']) == 0
    out = capsys.readouterr().out
    assert out.startswith(f'push #1 to {URL} as 합성-cli  started ') and 'sent: documents 1' in out
    assert seen == [TOKEN] and TOKEN not in out
    assert cli.main(['push']) == 0 and 'up to date (batches 0, bytes sent 0' in capsys.readouterr().out
    server.close()


def test_http_transport_sends_the_token_unredirected_without_proxy(monkeypatch):
    """The request the real transport builds (no network: urlopen is replaced)."""
    import urllib.request
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true, "result": {}}'

    def fake_urlopen(request, timeout):
        captured.update(request=request, timeout=timeout, opener=urllib.request._opener)
        return Response()
    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.example.invalid:3128')
    send = push.http_transport(TOKEN)
    assert send('POST', f'{URL}/push/batch', b'x') == (200, b'{"ok": true, "result": {}}')
    request = captured['request']
    assert request.headers == {'Accept': 'application/json', 'Content-type': 'application/json',
                               'Content-encoding': 'gzip'}                 # the audited headers: no token
    assert request.unredirected_hdrs == {'Authorization': f'Bearer {TOKEN}'}
    handlers = captured['opener'].handlers
    assert not [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]   # no proxy, not even the env's
    redirect = [h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert redirect and redirect[0].redirect_request(request, None, 302, 'Found', {}, 'https://other.invalid/') is None
    assert urllib.request._opener is None                                  # installed for that request only


# ---- a rebuilt, restored or taken-over collector store converges (review M5 remote v1 M-1) -----------------

def cite(server: Server, suffix: str) -> tuple[int, dict, str]:
    """A decision on the server citing line 1 of the current version of the document ending in ``suffix``."""
    doc_id, path, version_id, sha = server.db.execute(
        'SELECT d.id, d.path, v.id, v.sha256 FROM documents d JOIN versions v ON v.document_id = d.id '
        'WHERE d.path_key LIKE ? AND v.superseded_at IS NULL', (f'%{suffix}',)).fetchone()
    unit_json, text = server.db.execute('SELECT locator_json, text FROM units WHERE version_id=? AND ordinal=1',
                                        (version_id,)).fetchone()
    obj, quote = locator.make(doc_id, path, sha, json.loads(unit_json), text, [0, 10]), text[:10]
    assertion = server.db.execute(
        "INSERT INTO assertions(kind, statement, recorded_from, status, source_kind, recorded_by) "
        "VALUES('decision', '합성 결정', '2026-01-01T09:00:00+09:00', 'active', 'evidence', 'test')").lastrowid
    server.db.execute('INSERT INTO evidence(assertion_id, version_id, chunk_id, locator_json, quote, quote_sha256) '
                      'VALUES(?, ?, NULL, ?, ?, ?)', (assertion, version_id, json.dumps(obj, ensure_ascii=False),
                                                      quote, binding._sha(quote)))
    return version_id, obj, quote


def source_state(server: Server, version_id: int, obj: dict, quote: str) -> str:
    status, superseded = server.db.execute('SELECT d.status, v.superseded_at FROM versions v JOIN documents d ON '
                                           'd.id = v.document_id WHERE v.id = ?', (version_id,)).fetchone()
    return knowledge_query.source_state(status, superseded,
                                        knowledge_query.still_matches(server.db, version_id, obj, quote))


def converges(col: Collector, server: Server, collector: dict) -> dict:
    """One push that converges with nothing refused, then a second that sends nothing."""
    report = col.push(server, collector)
    assert report['exit_code'] == 0 and report['converged'] and report['refused'] == [], report
    second = col.push(server, collector)
    assert second['up_to_date'] and (second['batches'], second['bytes_sent']) == (0, 0), second
    assert sync.summary(server.db, col.key, collector_id=collector['id']) == sync.summary(col.db, col.key)
    assert sync.document_summaries(server.db, collector_id=collector['id']) == sync.document_summaries(col.db)
    assert sync.events_sha256(server.db, collector_id=collector['id']) == sync.events_sha256(col.db)
    assert binding.verify(server.db, stored_only=True)['ok']
    return report


def served_paths(server: Server, status: str = 'active') -> set[str]:
    return {row[0] for row in server.db.execute('SELECT path_key FROM documents WHERE status = ?', (status,))}


def test_a_rebuilt_collector_store_converges_and_keeps_the_sources(pair, tmp_path):
    col, server, collector = pair
    rewrite(col.src/CHANGED, other_docx())                     # some history before the rebuild
    (col.src/DELETED).unlink()
    col.collect()
    converges(col, server, collector)
    cited = cite(server, 'notes_utf8.txt')
    documents, versions = server.count('documents'), server.count('versions')
    active = served_paths(server)
    # the collector's home is lost: a new store scans the same root, pushes with the same collector (token)
    fresh = Collector(tmp_path, 'rebuilt', src=col.src)
    try:
        fresh.collect()
        assert fresh.count('documents') == col.count('documents') - 1    # the deleted file is not seen again
        report = converges(fresh, server, collector)
        assert report['sent']['units'] == 0                     # nothing to re-send but ids and events
        assert server.count('documents') == documents and server.count('versions') == versions
        assert served_paths(server) == active                                     # same documents served
        assert source_state(server, *cited) == 'current'                          # the decision's source holds
        doc = server.db.execute("SELECT status FROM documents WHERE path_key LIKE ?", (f'%{DELETED.split("/")[-1]}',))
        assert doc.fetchone() == ('deleted',)
        # and it keeps converging as files change
        rewrite(fresh.src/'docs/notes_utf8.txt', '합성 재구성 뒤 변경\n'.encode('utf-8'))
        fresh.collect()
        converges(fresh, server, collector)
        assert source_state(server, *cited) == 'changed'
    finally:
        fresh.close()


def test_a_store_restored_from_an_old_backup_converges(pair, tmp_path):
    import sqlite3
    col, server, collector = pair
    col.push(server, collector)
    backup = sqlite3.connect(tmp_path/'backup.sqlite3')
    col.db.backup(backup)
    # after the backup: a change, a deletion and a new file, all pushed
    rewrite(col.src/CHANGED, other_docx())
    (col.src/DELETED).unlink()
    (col.src/'docs'/'새 파일.txt').write_text('\n'.join(LINES_KO[:2]), encoding='utf-8')
    col.collect()
    converges(col, server, collector)
    cited = cite(server, 'report.docx')
    # the store goes back to the backup (rowids of the later rows are given again), first pushed as it is ...
    col.db.close()
    target = sqlite3.connect(paths_module.db_path(col.home))
    backup.backup(target)
    target.close()
    backup.close()
    col.db = store.open_home(col.home)
    converges(col, server, collector)
    assert served_paths(server) == {row[0] for row in col.db.execute(
        "SELECT path_key FROM documents WHERE status = 'active'")}
    # ... then scanned again: the files on disk are the truth again
    col.collect()
    converges(col, server, collector)
    # the report the decision cited is that content again, in a newer server version: changed, with a requote
    version_id, obj, quote = cited
    assert source_state(server, *cited) == 'changed'
    assert knowledge_query.requote(server.db, obj['doc'], obj['path'], obj, quote)['found']
    assert binding.verify(col.db)['ok']


def test_a_retired_collectors_root_is_taken_over_by_a_new_collector(pair, tmp_path):
    from workmcp import remote
    col, server, collector = pair
    converges(col, server, collector)
    cited = cite(server, 'notes_utf8.txt')
    documents = server.count('documents')
    successor = server.collector('합성-후임')
    fresh = Collector(tmp_path, 'successor', src=col.src)
    try:
        fresh.collect()
        # while the first collector is live, its root (in any spelling) is refused to another
        refused_ = fresh.push(server, successor)
        assert refused_['exit_code'] == push.EXIT_FAILED and 'root_owned_by_other_collector' in refused_['error']
        remote.retire_collector(server.db, collector['name'])
        report = converges(fresh, server, successor)
        assert report['sent']['units'] == 0                     # the server has every unit already
        assert server.count('documents') == documents
        assert source_state(server, *cited) == 'current'
        owners = {row[0] for row in server.db.execute('SELECT collector_id FROM source_roots')}
        assert owners == {successor['id']}
        assert server.db.execute('SELECT COUNT(*) FROM collector_documents WHERE collector_id = ?',
                                 (collector['id'],)).fetchone()[0] == 0
    finally:
        fresh.close()


def test_root_spelling_and_nesting_do_not_get_around_ownership(tmp_path):
    server = Server(tmp_path)
    first, second = server.collector('합성-하나'), server.collector('합성-둘')
    a = Collector(tmp_path, 'pc-a')
    (a.src/'notes.txt').write_text('\n'.join(LINES_KO), encoding='utf-8')
    a.collect()
    try:
        assert a.push(server, first)['converged']
        root = a.db.execute('SELECT path FROM source_roots').fetchone()[0]
        for path in (root.upper(), root.replace('\\', '/'), root + '\\sub', str(Path(root).parent)):
            begin = sync.envelope('begin', [{'roots': [{'local_root_id': 1, 'path': path, 'enabled': 1}],
                                             'started_at': store.now()}], collector=second['name'], push_id=None,
                                  client_version='0')
            answer = receive.apply(server.db, second, begin, raw_bytes=1)
            assert answer['error']['code'] == 'root_owned_by_other_collector', path
        assert server.count('source_roots') == 1
    finally:
        a.close()
        server.close()


def test_one_push_per_data_home_and_tls_failures_are_not_retried(pair, monkeypatch):
    col, server, collector = pair
    lock, _ = store.acquire_lock(col.home, push.PUSH_LOCK)
    try:
        locked = col.push(server, collector)
        assert locked['exit_code'] == push.EXIT_LOCKED == 3 and 'another push is running' in locked['error']
        assert server.requests == []
    finally:
        store.release_lock(lock)
    import ssl
    import urllib.error
    import urllib.request

    def refuse(request, timeout):
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1, 'certificate verify failed'))
    monkeypatch.setattr(urllib.request, 'urlopen', refuse)
    report = push.execute(col.home, settings(), TOKEN, push.Options(), sleep=col.sleeps.append)
    assert report['exit_code'] == push.EXIT_TLS == 7 and report['retries'] == 0 and col.sleeps == []
    assert 'TLS certificate verification failed for srv.example.invalid' in report['error']
    monkeypatch.setattr(urllib.request, 'urlopen', lambda request, timeout: (_ for _ in ()).throw(
        urllib.error.URLError(ConnectionRefusedError())))
    assert push.execute(col.home, settings(), TOKEN, push.Options(), sleep=col.sleeps.append)['exit_code'] == \
        push.EXIT_UNREACHABLE


# ---- review v2: what a rebuilt store no longer has is swept at a complete end (N-1, L-a, L-e), L-d --------

def active_keys(db) -> set[str]:
    return {row[0] for row in db.execute("SELECT path_key FROM documents WHERE status = 'active'")}


def four_files(src: Path) -> None:
    for name in 'abcd':
        (src/f'{name}.txt').write_text(f'{name} 합성 고유어{name * 3}\n' + '\n'.join(LINES_KO), encoding='utf-8')


def test_a_file_deleted_before_a_rebuild_whose_id_is_reused_is_swept(tmp_path):
    """Review v2 N-1 (probe R1): a.txt (id 1) is deleted, the store is rebuilt without a scan in between, and
    b, c, d now have ids 1..3. The complete push's end marks a.txt deleted: the server serves what the
    collector has, with a ``deleted`` event, and the next push sends nothing."""
    server = Server(tmp_path)
    collector = server.collector('합성-재생성')
    col = Collector(tmp_path)
    four_files(col.src)
    col.collect()
    fresh = None
    try:
        assert col.push(server, collector)['converged']
        (col.src/'a.txt').unlink()
        fresh = Collector(tmp_path, 'rebuilt', src=col.src)
        fresh.collect()
        assert sorted(fresh.db.execute('SELECT id FROM documents')) == [(1,), (2,), (3,)]
        report = converges(fresh, server, collector)
        assert report['sent']['forgotten'] == 1                   # swept by the server's end
        assert active_keys(server.db) == active_keys(fresh.db)
        events = store.document_events_query(server.db, {'kinds': ['deleted']}, 10, 0)[0]
        assert [event['doc']['path'].endswith('a.txt') for event in events] == [True]
        hits = search_module.Engine(server.home).search('고유어aaa', mode='lexical')['hits']
        assert hits == []
    finally:
        col.close()
        if fresh:
            fresh.close()
        server.close()


def test_a_cut_push_neither_forgets_nor_sweeps(tmp_path):
    """Review v2 L-a (probe R2): after a rebuild a ``--limit-docs`` run sends some documents only; it forgets
    and sweeps nothing (no file still on disk is shown deleted); the next full run converges."""
    server = Server(tmp_path)
    collector = server.collector('합성-잘림')
    col = Collector(tmp_path)
    four_files(col.src)
    col.collect()
    fresh = None
    try:
        assert col.push(server, collector)['converged']
        (col.src/'a.txt').unlink()
        col.collect()
        assert col.push(server, collector)['converged']           # a.txt deleted on the server, by the collector
        before = {row[0] for row in server.db.execute("SELECT path_key FROM documents WHERE status = 'deleted'")}
        fresh = Collector(tmp_path, 'rebuilt', src=col.src)
        fresh.collect()
        cut = fresh.push(server, collector, limit_docs=1)
        assert cut['exit_code'] == 0 and cut['sent']['forgotten'] == 0 and not cut['converged']
        assert {row[0] for row in server.db.execute("SELECT path_key FROM documents WHERE status = 'deleted'")} == before
        assert store.document_events_query(server.db, {'kinds': ['deleted']}, 10, 0)[1] == 1
        converges(fresh, server, collector)
        assert active_keys(server.db) == active_keys(fresh.db)
    finally:
        col.close()
        if fresh:
            fresh.close()
        server.close()


def test_a_successor_sweeps_the_retired_collectors_files_it_does_not_have(tmp_path):
    """Review v2 L-e: after ``collectors retire`` the successor takes the root over; a file only the retired
    collector had (deleted meanwhile) is swept at the successor's complete end."""
    from workmcp import remote
    server = Server(tmp_path)
    first, successor = server.collector('합성-전임'), server.collector('합성-후임')
    col = Collector(tmp_path)
    four_files(col.src)
    col.collect()
    fresh = None
    try:
        assert col.push(server, first)['converged']
        remote.retire_collector(server.db, first['name'])
        (col.src/'b.txt').unlink()
        fresh = Collector(tmp_path, 'successor', src=col.src)
        fresh.collect()
        converges(fresh, server, successor)
        assert active_keys(server.db) == active_keys(fresh.db)
        assert server.db.execute("SELECT status FROM documents WHERE path_key LIKE '%b.txt'").fetchone() == ('deleted',)
    finally:
        col.close()
        if fresh:
            fresh.close()
        server.close()


def test_the_push_lock_is_released_after_any_failure_and_a_stale_one_is_replaced(pair, monkeypatch):
    """Review v2 L-d."""
    col, server, collector = pair
    lock_file = col.home/push.PUSH_LOCK

    def broken(home):
        raise OSError('synthetic: the log folder is not writable')
    monkeypatch.setattr(push, '_file_logger', broken)
    with pytest.raises(OSError):
        col.push(server, collector)
    assert not lock_file.exists()
    monkeypatch.undo()
    old = store.now()
    lock_file.write_text(json.dumps({'pid': 1, 'started_at': '2026-01-01T00:00:00+09:00'}), encoding='utf-8')
    report = col.push(server, collector)
    assert report['exit_code'] == 0 and 'removed a stale push lock older than 6 hours' in report['warning']
    assert report['warning'] in '\n'.join(push.report_lines(report)) and not lock_file.exists() and old


# ---- a large version is applied part by part (review M5 remote v1 M-2) ---------------------------------------

def big_text(chars: int) -> str:
    line = '합성 대량 문장이 길게 이어진다, synthetic bulk line for the part test. ' * 2
    lines, total, n = [], 0, 0
    while total < chars:
        text = f'{n:07d} {line}'
        lines.append(text)
        total += len(text) + 1
        n += 1
    return '\n'.join(lines)


def test_a_split_version_is_applied_part_by_part_with_a_bounded_python_peak(tmp_path):
    """8 M characters in parts: every part is its own short transaction (chunks and FTS rows grow part by part),
    nothing waits in ``push_parts`` but a continuing unit, and the Python allocation peak of applying one part
    stays near one batch (tracemalloc over ``receive.apply``; SQLite's own cache is outside it)."""
    import tracemalloc
    server = Server(tmp_path)
    collector = server.collector('합성-큰문서')
    col = Collector(tmp_path)
    (col.src/'big.txt').write_text(big_text(8_300_000), encoding='utf-8')
    col.collect()
    peaks, chunk_counts, waiting = [], [], []
    real_apply = receive.apply

    def measured(db, owner, body, **kwargs):
        tracemalloc.start()
        try:
            return real_apply(db, owner, body, **kwargs)
        finally:
            peaks.append((body['kind'], tracemalloc.get_traced_memory()[1]))
            tracemalloc.stop()
            chunk_counts.append(db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0])
            waiting.append(db.execute('SELECT COALESCE(SUM(LENGTH(body_json)), 0) FROM push_parts').fetchone()[0])
    receive.apply = measured
    try:
        report = col.push(server, collector)
    finally:
        receive.apply = real_apply
    try:
        assert report['exit_code'] == 0 and report['converged'], report
        units_batches = [i for i, (kind, _) in enumerate(peaks) if kind == 'units']
        assert len(units_batches) >= 4
        counts = [chunk_counts[i] for i in units_batches]
        assert counts == sorted(counts) and counts[0] > 0         # chunks are built as the parts come
        assert max(peak for kind, peak in peaks if kind == 'units') < 48 * 2**20
        assert max(waiting) < 2**20 and server.count('push_parts') == 0
        mirrored(col, server, collector)
    finally:
        col.close()
        server.close()


def test_unfinished_and_out_of_order_parts_are_cleared(tmp_path):
    server = Server(tmp_path)
    collector = server.collector('합성-중단')
    col = Collector(tmp_path)
    (col.src/'big.txt').write_text(big_text(5_000_000), encoding='utf-8')
    col.collect()
    sent = []

    def stop_after_two_parts(method, name, body, call):
        if body is not None and body['kind'] == 'units':
            sent.append(body)
            if len(sent) >= 3:
                raise push.Unreachable('ConnectionResetError')
        return call()
    server.hook = stop_after_two_parts
    try:
        assert col.push(server, collector)['exit_code'] == push.EXIT_UNREACHABLE
        assert server.count('units') > 0 and server.count('push_parts') >= 1      # two parts applied
        server.hook = None
        # a part out of order, and the first part of a second split version while one is open, are refused
        push_id = server.db.execute("SELECT id FROM pushes WHERE status = 'running'").fetchone()[0]
        item = sent[1]['items'][0]
        skipped = sync.envelope('units', [{**item, 'part': item['part'] + 2}], collector=collector['name'],
                                push_id=push_id, client_version='0')
        assert receive.apply(server.db, collector, skipped, raw_bytes=1)['error']['code'] == 'bad_batch'
        # the next push aborts the old one, removes its partial version and converges
        report = col.push(server, collector)
        assert report['exit_code'] == 0 and report['converged']
        assert server.count('push_parts') == 0
        mirrored(col, server, collector)
        # an end with a split version still open removes it too
        rewrite(col.src/'big.txt', big_text(4_000_000).encode('utf-8'))
        col.collect()
        server.hook = lambda method, name, body, call: (200, json.dumps(sync.ok(
            {'versions': [], 'vectors_missing': []})).encode()) if body and body['kind'] == 'units' and \
            body['items'][0]['part'] > 1 else call()
        partial = col.push(server, collector)
        assert server.count('push_parts') == 0 and not partial['converged']
        version = server.db.execute('SELECT id FROM versions WHERE superseded_at IS NULL').fetchone()[0]
        assert server.db.execute('SELECT COUNT(*) FROM units WHERE version_id = ?', (version,)).fetchone()[0] == 0
        server.hook = None
        assert col.push(server, collector)['converged']
        mirrored(col, server, collector)
    finally:
        col.close()
        server.close()


# ---- runtime audit (design §6.3) ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = 'synthetic'

    def _answer(self, method: str):
        state = self.server.state
        path, _, query = self.path.partition('?')
        state['paths'].append(f'{method} {path}')
        names = {'/workmcp/push/state': 'state', '/workmcp/push/state/vectors': 'vectors', '/workmcp/push/batch': 'batch'}
        if self.headers.get('Authorization') != f'Bearer {TOKEN}' or path not in names:
            status, payload = 401, sync.error('invalid_token')
        else:
            data = self.rfile.read(int(self.headers.get('Content-Length') or 0)) if method == 'POST' else None
            if data is not None:
                state['bodies'].append(data)
            db = store.open_home(state['home'])
            try:
                values = dict(pair.split('=', 1) for pair in query.split('&') if pair)
                status, payload = receive.dispatch(db, state['collector'], method, names[path], values, data)
            finally:
                db.close()
        raw = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._answer('GET')

    def do_POST(self):
        self._answer('POST')

    def log_message(self, *args):
        pass


def test_push_mode_reaches_only_the_configured_server(tmp_path):
    """``_audited_server.py LOG push PORT ROOT``: the real ``workmcp push`` (twice) under the audit hook with
    ``judge_push``: destinations are the configured server only, no original file is opened, the collector's
    store is opened read-only, and the second run sends nothing."""
    server = Server(tmp_path)
    collector = server.collector('합성-감사')
    server.close()
    col = Collector(tmp_path)
    col.write_bundle()
    col.collect()
    col.close()
    http = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    port = http.server_address[1]
    http.state = {'home': server.home, 'collector': collector, 'paths': [], 'bodies': []}
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        (col.home/'config.toml').write_text(f'[push]\nurl = "http://127.0.0.1:{port}/workmcp"\n', encoding='utf-8')
        (col.home/paths_module.PUSH_TOKEN_FILE).write_text(TOKEN, encoding='utf-8')
        log = tmp_path/'audit-push.json'
        env = {key: value for key, value in os.environ.items() if 'proxy' not in key.lower()}
        env.update(WORKMCP_HOME=str(col.home), PYTHONPATH=str(ROOT/'src'))
        env.pop(paths_module.PUSH_TOKEN_ENV, None)
        run = subprocess.run([sys.executable, str(WRAPPER), str(log), 'push', str(port), str(col.src)], env=env,
                             cwd=tmp_path, capture_output=True, text=True, encoding='utf-8', timeout=300)
    finally:
        http.shutdown()
        http.server_close()
    assert run.returncode == 0, run.stderr
    report = json.loads(log.read_text(encoding='utf-8'))
    assert report['completed'] is True, run.stderr
    assert [e for e in report['events'] if e['violation']] == []
    assert report['exit_codes'] == [0, 0]
    assert report['source_opens'] == 0                               # push never opens an original
    assert report['connects'] and all('mode=ro' in target for target in report['connects'])
    assert set(report['destinations']) == {f'127.0.0.1:{port}'}
    assert report['first']['batches'] > 0 and report['first']['bytes_sent'] > 0
    assert report['second']['up_to_date'] and (report['second']['batches'], report['second']['bytes_sent']) == (0, 0)
    assert TOKEN not in log.read_text(encoding='utf-8') and TOKEN not in run.stdout + run.stderr
    assert SENTINEL not in run.stdout + run.stderr
    assert all(path.startswith(('GET /workmcp/push/', 'POST /workmcp/push/batch')) for path in http.state['paths'])
    for data in http.state['bodies']:
        wire_keys_only(sync.ungzip_json(data))
    check, col.db = Server(tmp_path), store.open_read(col.home)
    try:
        mirrored(col, check, collector)
    finally:
        col.db.close()
        check.close()


def test_a_long_unit_is_applied_one_piece_at_a_time(tmp_path):
    """Review v2 N-2: one line of 3 M characters (one unit) goes one piece per batch; the server stores the
    unit once when it ends, chunks it as it grows (the same chunks as the collector's index), and no single
    application holds the whole unit in Python (tracemalloc over ``receive.apply``)."""
    import tracemalloc
    server = Server(tmp_path)
    collector = server.collector('합성-긴줄')
    col = Collector(tmp_path)
    (col.src/'line.txt').write_text(('합성 긴 한 줄 문장입니다. ' * 200_001)[:3_000_000], encoding='utf-8')
    col.collect()
    count, length = col.db.execute('SELECT COUNT(*), MAX(LENGTH(text)) FROM units').fetchone()
    assert count == 1 and length > 2_999_000
    peaks, real_apply = [], receive.apply

    def measured(db, owner, body, **kwargs):
        tracemalloc.start()
        try:
            return real_apply(db, owner, body, **kwargs)
        finally:
            peaks.append((body['kind'], tracemalloc.get_traced_memory()[1]))
            tracemalloc.stop()
    receive.apply = measured
    try:
        report = col.push(server, collector)
    finally:
        receive.apply = real_apply
    try:
        assert report['exit_code'] == 0 and report['converged'], report
        units_batches = [body for body in server.bodies if body['kind'] == 'units']
        assert len(units_batches) == -(-length // sync.UNIT_PIECE_CHARS)
        assert all(len(body['items']) == 1 and len(body['items'][0]['units']) == 1 for body in units_batches)
        assert max(peak for kind, peak in peaks if kind == 'units') < 16 * 2**20      # the unit is 3 M chars
        mirrored(col, server, collector)
        assert binding.verify(server.db, stored_only=True)['ok']
    finally:
        col.close()
        server.close()

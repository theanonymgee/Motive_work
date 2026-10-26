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

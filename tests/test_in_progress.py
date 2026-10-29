"""Review M5 remote v3 V3-2: a split version the operating server is still applying is no search candidate.

Its chunks are built part by part while its units arrive (review v2 N-2), so between parts, and after a cut
push until the next one, the server holds chunks and FTS rows of a version whose units are not all there.
``store.latest_chunks_sql`` leaves such a version out (``store.IN_PROGRESS_SQL``): no hit points at units
that are not stored yet. Same in-memory server and collector as ``tests/test_push.py``; synthetic text only.
"""
import pytest

from test_push import Collector, Server, big_text, mirrored
from workmcp import binding, chunks, push, receive, store
from workmcp import search as search_module

QUERY = 'synthetic bulk line'                 # in every line of big_text, in no other synthetic document


def version_chunks(db, sql: str, version_id: int) -> int:
    return db.execute(f'SELECT COUNT(*) FROM {sql} AND c.chunk_version = ? AND c.version_id = ?',
                      (chunks.CHUNK_VERSION, version_id)).fetchone()[0]


def eligible_chunks_sql() -> str:
    """The chunks of eligible versions without the in-progress condition (what search used before V3-2)."""
    return ('chunks c JOIN versions v ON v.id = c.version_id JOIN documents d ON d.id = v.document_id '
            f'WHERE {store.ELIGIBLE_VERSIONS}')


def hits(db, mode: str = 'lexical') -> list[dict]:
    return search_module.search(db, QUERY, mode=mode, limit=10)['hits']


@pytest.fixture
def cut_after_two_parts(tmp_path):
    """A push cut after the first two of three parts of one split version (the third never arrives)."""
    server = Server(tmp_path)
    collector = server.collector('합성-진행중')
    col = Collector(tmp_path)
    (col.src/'big.txt').write_text(big_text(600_000), encoding='utf-8')
    col.collect()
    sent = []

    def stop_after_two_parts(method, name, body, call):
        if body is not None and body['kind'] == 'units':
            if len(sent) >= 2:
                raise push.Unreachable('ConnectionResetError')
            sent.append(body['items'][0]['part'])
        return call()
    server.hook = stop_after_two_parts
    report = col.push(server, collector)
    server.hook = None
    yield col, server, collector, report, sent
    col.close()
    server.close()


def test_a_version_in_progress_is_no_search_candidate_until_its_last_part(cut_after_two_parts):
    col, server, collector, report, sent = cut_after_two_parts
    assert report['exit_code'] == push.EXIT_UNREACHABLE and sent == [1, 2]
    (version_id, parts), = server.db.execute(
        'SELECT m.version_id, p.parts FROM push_parts p JOIN collector_versions m ON '
        'm.local_version_id = p.local_version_id WHERE p.part = 0').fetchall()
    assert parts == 3
    in_progress = f'SELECT 1 WHERE ? IN ({store.IN_PROGRESS_SQL})'
    assert server.db.execute(in_progress, (version_id,)).fetchone() is not None
    # the chunks of parts 1-2 are there and eligible as before; only the new condition leaves them out
    assert version_chunks(server.db, eligible_chunks_sql(), version_id) > 0
    assert version_chunks(server.db, store.latest_chunks_sql(), version_id) == 0
    assert server.db.execute('SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?',
                             (f'"{QUERY}"',)).fetchone()[0] > 0
    assert hits(server.db) == []
    # the version in progress is not checked (in_progress), the others verify
    stored = binding.verify(server.db, stored_only=True)
    assert stored['ok'] and stored['versions']['unverifiable']['in_progress'] == 1

    # the next push aborts the cut one, sends the version again from part 1 and finishes it
    final = col.push(server, collector)
    assert final['exit_code'] == 0 and final['converged'], final
    assert server.count('push_parts') == 0
    mirrored(col, server, collector)
    found = hits(server.db)
    assert found and all(hit['doc']['path'].endswith('big.txt') for hit in found)
    assert binding.verify(server.db, stored_only=True)['ok']


def test_a_candidate_of_a_version_in_progress_never_becomes_a_hit(cut_after_two_parts):
    """Review M6 v1 M6-5: the re-check of ``search._hits`` uses the in-progress rule too, so a candidate that came
    from elsewhere (a vector cache mapping left from before, a reused chunk id with the same text) is no hit while
    its version is in progress. Here a finished version is marked in progress again by hand (its units are all
    there, so only the condition can keep it out)."""
    col, server, collector = cut_after_two_parts[:3]
    final = col.push(server, collector)
    assert final['exit_code'] == 0 and final['converged'], final
    db = server.db
    (version_id, local), = db.execute(
        "SELECT m.version_id, m.local_version_id FROM collector_versions m JOIN versions v ON v.id = m.version_id "
        "JOIN documents d ON d.id = v.document_id WHERE d.path LIKE '%big.txt' AND v.superseded_at IS NULL").fetchall()
    ranked = [(row[0], {'lexical': 1.0}) for row in db.execute(
        'SELECT id FROM chunks WHERE version_id = ? AND chunk_version = ? ORDER BY id LIMIT 3',
        (version_id, chunks.CHUNK_VERSION))]
    assert ranked and len(search_module._hits(db, ranked, [QUERY])) == len(ranked)     # eligible and complete
    push_id = db.execute('SELECT MAX(id) FROM pushes').fetchone()[0]
    db.execute('INSERT INTO push_parts(push_id, local_version_id, part, parts, body_json, received_at) '
               "VALUES (?, ?, ?, 3, '{}', '2026-10-29T00:00:00+09:00')", (push_id, local, store.PROGRESS_PART))
    try:
        assert search_module._hits(db, ranked, [QUERY]) == []
    finally:
        db.execute('DELETE FROM push_parts WHERE push_id = ? AND local_version_id = ?', (push_id, local))
    assert len(search_module._hits(db, ranked, [QUERY])) == len(ranked)


def test_verify_stored_finds_no_mismatch_while_a_version_is_in_progress(cut_after_two_parts):
    """Design M6 v1 §1.2: ``verify --stored`` counts a version in progress as unverifiable (``in_progress``),
    not as a mismatch; the CLI line and notice say so."""
    from workmcp import cli
    server = cut_after_two_parts[1]
    assert server.count('push_parts') > 0
    stored = binding.verify(server.db, stored_only=True)
    assert stored['ok'] and stored['failures'] == [], stored['failures']
    assert stored['versions']['unverifiable']['in_progress'] == 1
    assert stored['versions']['checked'] == stored['versions']['total'] - 1
    assert binding.IN_PROGRESS_NOTICE.format(1) in stored['notice']
    lines = cli.verify_lines(stored)
    assert lines[0].endswith('timeout 0, in_progress 1)') and lines[-1].startswith('binding ok')


def test_receive_uses_the_one_in_progress_query(cut_after_two_parts):
    """``receive._in_progress`` is ``store.IN_PROGRESS_SQL`` for one version, and the part that query reads is
    the part ``receive`` writes the progress into (``receive.PROGRESS_PART``)."""
    server = cut_after_two_parts[1]
    assert receive.PROGRESS_PART == store.PROGRESS_PART == 0
    assert store.IN_PROGRESS_SQL.endswith(f'WHERE p.part = {receive.PROGRESS_PART}')
    progress = {row[0] for row in server.db.execute(store.IN_PROGRESS_SQL)}
    assert len(progress) == 1
    versions = [row[0] for row in server.db.execute('SELECT id FROM versions')]
    assert {v for v in versions if receive._in_progress(server.db, v)} == progress


def test_a_collector_store_is_unchanged_by_the_condition(cut_after_two_parts):
    """``push_parts`` is empty on a collector: its target chunks are the eligible ones, as before."""
    col = cut_after_two_parts[0]
    assert col.count('push_parts') == 0
    before = col.db.execute(f'SELECT COUNT(*) FROM {eligible_chunks_sql()}').fetchone()[0]
    assert before > 0 and col.db.execute(f'SELECT COUNT(*) FROM {store.latest_chunks_sql()}').fetchone()[0] == before
    assert col.db.execute(store.IN_PROGRESS_SQL).fetchall() == []
    assert binding.verify(col.db)['versions']['unverifiable']['in_progress'] == 0

"""Empty schema v1, migration ledger and append-only triggers (design §3.2), the M2 schema edits and the
chunk/FTS functions (design M2 §1.4, §4.3), migration v2 (design M3 §3.4): tool call log, delivery rows,
document events and their queries; migrations v3/v4 (design M4) and v5 (design M5 remote §2.2): collectors,
tokens (SHA-256 only, append-only), push records and collector id maps. Synthetic rows only."""
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from workmcp import paths, store, tools

TABLES = {'schema_migrations', 'source_roots', 'documents', 'versions', 'units', 'chunks', 'chunk_vectors',
          'entities', 'assertions', 'evidence', 'links', 'deliveries', 'scans', 'meta', 'index_runs',
          'tool_calls', 'document_events', 'assertion_events', 'collectors', 'tokens', 'pushes', 'push_batches',
          'push_parts', 'collector_documents', 'collector_versions', 'collector_events'}
TRIGGERS = {'assertions_closed_guard', 'assertions_update_guard', 'assertions_status_guard', 'assertions_no_delete',
            'assertions_no_replace', 'evidence_no_update', 'evidence_no_delete', 'evidence_no_replace',
            'versions_update_guard', 'versions_no_delete', 'versions_no_replace', 'versions_no_replace_seq',
            'tool_calls_no_update', 'tool_calls_no_delete', 'tool_calls_no_replace', 'tool_calls_no_replace_call',
            'deliveries_no_update', 'deliveries_no_delete', 'deliveries_no_replace',
            'document_events_no_update', 'document_events_no_delete', 'document_events_no_replace',
            'assertions_v3_update_guard', 'assertions_v3_dates', 'assertions_v3_shape', 'entities_update_guard',
            'entities_no_delete', 'entities_no_replace', 'entities_no_replace_name', 'links_update_guard',
            'links_no_delete', 'links_no_replace', 'assertion_events_no_update', 'assertion_events_no_delete',
            'assertion_events_no_replace', 'tokens_update_guard', 'tokens_no_delete', 'tokens_no_replace',
            'tokens_no_replace_sha'}
INDEXES = {'versions_sha256', 'versions_by_sha256', 'chunks_text', 'tool_calls_at', 'deliveries_call',
           'document_events_at', 'document_events_doc', 'assertions_subject', 'assertions_object',
           'assertions_supersedes', 'links_from', 'links_to', 'assertion_events_assertion', 'units_locator'}
H = '0' * 64
NOW = '2026-10-23T09:00:00+09:00'
LATER = '2026-10-23T10:00:00+09:00'


@pytest.fixture
def db(tmp_path):
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    connection = store.open_home(home)
    yield connection
    connection.close()


def names(db, kind):
    return {r[0] for r in db.execute('SELECT name FROM sqlite_master WHERE type=?', (kind,))}


def test_empty_db_has_schema_v1_v2_and_v3(db):
    assert TABLES <= names(db, 'table')
    assert TRIGGERS == names(db, 'trigger')
    assert INDEXES <= names(db, 'index')
    assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    rows = db.execute('SELECT version, name FROM schema_migrations').fetchall()
    assert rows == [(1, 'initial_schema_v1'), (2, 'tool_calls_events_v2'), (3, 'knowledge_v3'), (4, 'units_locator_v4'),
                    (5, 'remote_sync_v5')]
    assert store.SCHEMA_VERSION == 5
    assert all(db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] == 0 for t in TABLES - {'schema_migrations'})
    if store.fts5_available():
        assert 'chunks_fts' in names(db, 'table')


def test_migrate_is_idempotent_and_detects_edited_migration(db, monkeypatch):
    assert store.migrate(db) == []
    monkeypatch.setattr(store, 'MIGRATIONS', [(1, 'initial_schema_v1', store.SCHEMA_V1 + '\n-- edited\n')])
    with pytest.raises(RuntimeError, match='checksum changed'):
        store.migrate(db)
    assert db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] == 5


def test_statement_splitter_keeps_trigger_bodies():
    parts = store.statements(store.SCHEMA_V1)
    guard = next(p for p in parts if 'assertions_update_guard' in p)
    assert guard.startswith('CREATE TRIGGER') and guard.endswith('END;')


def add_document(db):
    root = db.execute(r"INSERT INTO source_roots(path, added_at) VALUES('C:\synthetic', ?)", (NOW,)).lastrowid
    return db.execute(r"INSERT INTO documents(root_id, path, path_key, ext, status, first_seen_at, last_seen_at) "
                      r"VALUES(?, 'C:\synthetic\a.txt', 'c:\synthetic\a.txt', '.txt', 'active', ?, ?)",
                      (root, NOW, NOW)).lastrowid


def add_version(db, doc, seq, sha256, status='ok'):
    return db.execute("INSERT INTO versions(document_id, seq, sha256, size, mtime_ns, observed_at, parse_status) "
                      "VALUES(?, ?, ?, 1, 1, ?, ?)", (doc, seq, sha256, NOW, status)).lastrowid


def test_content_returning_to_an_earlier_hash_is_a_new_version(db):
    """A -> B -> A: three versions in observation order (review M-5)."""
    doc = add_document(db)
    a, b = 'a' * 64, 'b' * 64
    for seq, sha in enumerate([a, b, a], 1):
        add_version(db, doc, seq, sha)
    assert [r[0] for r in db.execute('SELECT sha256 FROM versions WHERE document_id=? ORDER BY seq', (doc,))] == [a, b, a]
    with pytest.raises(sqlite3.IntegrityError):
        add_version(db, doc, 3, b)  # one row per observation number


def insert_assertion(db, statement='합성 사실 A', supersedes=None, status='active'):
    cur = db.execute(
        "INSERT INTO assertions(kind, statement, recorded_from, status, supersedes_id, source_kind, recorded_by) "
        "VALUES('decision', ?, ?, ?, ?, 'my_note', 'test/record_fact')", (statement, NOW, status, supersedes))
    return cur.lastrowid


def row(db, assertion):
    return db.execute('SELECT status, recorded_to, supersedes_id, change_reason FROM assertions WHERE id=?',
                      (assertion,)).fetchone()


@pytest.mark.parametrize('assignment', ["statement='바뀜'", "data_json='{\"x\":1}'", "kind='todo'",
                                        "valid_from='2026-01-01'", "recorded_by='other'",
                                        "recorded_from='2000-01-01T00:00:00+09:00'", "source_kind='evidence'"])
def test_assertion_content_cannot_change(db, assignment):
    assertion = insert_assertion(db)
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute(f'UPDATE assertions SET {assignment} WHERE id=?', (assertion,))


def test_assertion_status_may_close_but_row_cannot_be_deleted(db):
    assertion = insert_assertion(db)
    db.execute("UPDATE assertions SET status='withdrawn', recorded_to=?, change_reason='합성 철회' WHERE id=?",
               (LATER, assertion))
    assert row(db, assertion) == ('withdrawn', LATER, None, '합성 철회')
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute('DELETE FROM assertions WHERE id=?', (assertion,))


def test_open_assertion_cannot_have_recorded_to(db):
    assertion = insert_assertion(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE assertions SET recorded_to=? WHERE id=?", (LATER, assertion))
    db.execute("UPDATE assertions SET status='conflict' WHERE id=?", (assertion,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE assertions SET recorded_to=? WHERE id=?", (LATER, assertion))


def test_closing_requires_recorded_to(db):
    assertion = insert_assertion(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE assertions SET status='withdrawn' WHERE id=?", (assertion,))


# Review §1.9 reproductions: each was ALLOWED before and must now be refused.
def test_withdrawn_assertion_cannot_be_revived(db):
    assertion = insert_assertion(db)
    db.execute("UPDATE assertions SET status='withdrawn', recorded_to=?, change_reason='합성 철회' WHERE id=?",
               (LATER, assertion))
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute("UPDATE assertions SET status='active', recorded_to=NULL WHERE id=?", (assertion,))
    assert row(db, assertion) == ('withdrawn', LATER, None, '합성 철회')


def test_closed_assertion_history_cannot_be_rewritten(db):
    assertion = insert_assertion(db)
    db.execute("UPDATE assertions SET status='withdrawn', recorded_to=?, change_reason='합성 철회' WHERE id=?",
               (LATER, assertion))
    for assignment in ["status='withdrawn', recorded_to='1999-01-01', change_reason='rewritten'",
                       "recorded_to='2027-01-01T00:00:00+09:00'", "change_reason='rewritten'",
                       "status='superseded'", "status='conflict', recorded_to=NULL"]:
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            db.execute(f'UPDATE assertions SET {assignment} WHERE id=?', (assertion,))
    assert row(db, assertion) == ('withdrawn', LATER, None, '합성 철회')


def test_recorded_to_cannot_precede_recorded_from(db):
    assertion = insert_assertion(db)
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute("UPDATE assertions SET status='withdrawn', recorded_to='1999-01-01', change_reason='rewritten' "
                   "WHERE id=?", (assertion,))
    assert row(db, assertion) == ('active', None, None, None)


def test_supersedes_id_is_set_on_insert_only(db):
    first, second = insert_assertion(db, '합성 A'), insert_assertion(db, '합성 B')
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute('UPDATE assertions SET supersedes_id=? WHERE id=?', (second, first))
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute('UPDATE assertions SET supersedes_id=? WHERE id=?', (first, first))
    third = insert_assertion(db, '합성 C', supersedes=first)
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute('UPDATE assertions SET supersedes_id=NULL WHERE id=?', (third,))


def test_assertion_cannot_supersede_itself(db):
    insert_assertion(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO assertions(id, kind, statement, recorded_from, status, supersedes_id, source_kind, "
                   "recorded_by) VALUES(50, 'decision', '합성', ?, 'active', 50, 'my_note', 't')", (NOW,))
    with pytest.raises(sqlite3.IntegrityError):  # auto id predicted by the writer
        insert_assertion(db, '합성', supersedes=db.execute('SELECT MAX(id) + 1 FROM assertions').fetchone()[0])


def test_change_reason_is_set_once(db):
    assertion = insert_assertion(db)
    db.execute("UPDATE assertions SET status='conflict', change_reason='합성 충돌' WHERE id=?", (assertion,))
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute("UPDATE assertions SET change_reason='rewritten' WHERE id=?", (assertion,))
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute("UPDATE assertions SET change_reason=NULL WHERE id=?", (assertion,))


def test_conflict_marking_and_resolution_pass(db):
    first, second = insert_assertion(db, '합성 A'), insert_assertion(db, '합성 B')
    db.execute("UPDATE assertions SET status='conflict' WHERE id IN (?, ?)", (first, second))
    db.execute("UPDATE assertions SET status='active' WHERE id=?", (first,))
    db.execute("UPDATE assertions SET status='withdrawn', recorded_to=?, change_reason='합성 해소' WHERE id=?",
               (LATER, second))
    assert row(db, first) == ('active', None, None, None)
    assert row(db, second) == ('withdrawn', LATER, None, '합성 해소')


def test_supersede_flow_passes(db):
    old = insert_assertion(db, '합성 값 1')
    new = insert_assertion(db, '합성 값 2', supersedes=old)
    db.execute("UPDATE assertions SET status='superseded', recorded_to=?, change_reason='합성 대체' WHERE id=?",
               (LATER, old))
    assert row(db, old) == ('superseded', LATER, None, '합성 대체')
    assert row(db, new) == ('active', None, old, None)
    conflicted = insert_assertion(db, '합성 값 3')
    db.execute("UPDATE assertions SET status='conflict' WHERE id=?", (conflicted,))
    db.execute("UPDATE assertions SET status='superseded', recorded_to=?, change_reason='합성 대체' WHERE id=?",
               (NOW, conflicted))  # recorded_to may equal recorded_from
    assert row(db, conflicted)[0] == 'superseded'


def test_evidence_rows_cannot_change_or_be_deleted(db):
    version = add_version(db, add_document(db), 1, H)
    assertion = insert_assertion(db)
    evidence = db.execute("INSERT INTO evidence(assertion_id, version_id, locator_json, quote, quote_sha256) "
                          "VALUES(?, ?, '{}', '합성 인용', ?)", (assertion, version, H)).lastrowid
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute("UPDATE evidence SET quote='바뀜' WHERE id=?", (evidence,))
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute('DELETE FROM evidence WHERE id=?', (evidence,))


# Review v2 N-1: REPLACE conflict resolution deletes the old row without DELETE triggers.
ASSERTION_COLUMNS = ('id, kind, statement, recorded_from, recorded_to, status, change_reason, source_kind, '
                     'recorded_by')
EVIDENCE_COLUMNS = 'id, assertion_id, version_id, locator_json, quote, quote_sha256'


def full_row(db, table, key):
    return db.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()


def add_evidence(db, assertion, version, quote='합성 인용'):
    return db.execute("INSERT INTO evidence(assertion_id, version_id, locator_json, quote, quote_sha256) "
                      "VALUES(?, ?, '{}', ?, ?)", (assertion, version, quote, H)).lastrowid


@pytest.mark.parametrize('verb', ['INSERT OR REPLACE INTO', 'REPLACE INTO'])
@pytest.mark.parametrize('key_column', ['id', 'rowid'])
@pytest.mark.parametrize('closed', [True, False])
def test_assertion_cannot_be_replaced(db, verb, key_column, closed):
    assertion = insert_assertion(db)
    if closed:
        db.execute("UPDATE assertions SET status='withdrawn', recorded_to=?, change_reason='합성 철회' WHERE id=?",
                   (LATER, assertion))
    before = full_row(db, 'assertions', assertion)
    columns = ASSERTION_COLUMNS.replace('id', key_column, 1)
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute(f"{verb} assertions({columns}) VALUES(?, 'decision', '다시 쓴 내용', ?, NULL, 'active', NULL, "
                   "'my_note', 'test/replace')", (assertion, NOW))
    assert full_row(db, 'assertions', assertion) == before
    assert db.execute('SELECT COUNT(*) FROM assertions').fetchone()[0] == 1


@pytest.mark.parametrize('verb', ['INSERT OR REPLACE INTO', 'REPLACE INTO'])
def test_evidence_cannot_be_replaced(db, verb):
    version = add_version(db, add_document(db), 1, H)
    assertion = insert_assertion(db)
    db.execute("UPDATE assertions SET status='withdrawn', recorded_to=?, change_reason='합성 철회' WHERE id=?",
               (LATER, assertion))
    evidence = add_evidence(db, assertion, version)
    before = full_row(db, 'evidence', evidence)
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute(f"{verb} evidence({EVIDENCE_COLUMNS}) VALUES(?, ?, ?, '{{}}', '바꾼 인용', ?)",
                   (evidence, assertion, version, H))
    assert full_row(db, 'evidence', evidence) == before
    assert db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] == 1


@pytest.mark.parametrize('sql', [
    "UPDATE OR REPLACE assertions SET id=? WHERE id=?",
    "INSERT INTO assertions(id, kind, statement, recorded_from, status, source_kind, recorded_by) "
    "SELECT ?, 'decision', '다시', recorded_from, 'active', 'my_note', 't' FROM assertions WHERE id=? "
    "ON CONFLICT(id) DO UPDATE SET status='active', recorded_to=NULL"])
def test_other_conflict_forms_cannot_rewrite_assertions(db, sql):
    closed, other = insert_assertion(db, '합성 A'), insert_assertion(db, '합성 B')
    db.execute("UPDATE assertions SET status='withdrawn', recorded_to=?, change_reason='합성 철회' WHERE id=?",
               (LATER, closed))
    before = db.execute('SELECT * FROM assertions ORDER BY id').fetchall()
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute(sql, (closed, other))
    assert db.execute('SELECT * FROM assertions ORDER BY id').fetchall() == before


def test_id_is_the_only_unique_key_of_append_only_tables(db):
    """A second UNIQUE key would let REPLACE delete a row through that key; guard it before adding one."""
    for table in ('assertions', 'evidence'):
        assert [r for r in db.execute(f'PRAGMA index_list({table})') if r[2]] == []


def test_inserts_without_an_existing_id_still_pass(db):
    version = add_version(db, add_document(db), 1, H)
    first = insert_assertion(db, '합성 A')
    second = db.execute("INSERT OR REPLACE INTO assertions(kind, statement, recorded_from, status, source_kind, "
                        "recorded_by) VALUES('decision', '합성 B', ?, 'active', 'my_note', 't')", (NOW,)).lastrowid
    third = db.execute("INSERT INTO assertions(id, kind, statement, recorded_from, status, source_kind, recorded_by) "
                       "VALUES(100, 'decision', '합성 C', ?, 'active', 'my_note', 't')", (NOW,)).lastrowid
    assert [first, third] == [1, 100] and second == 2
    add_evidence(db, first, version)
    db.execute(f"REPLACE INTO evidence({EVIDENCE_COLUMNS}) VALUES(NULL, ?, ?, '{{}}', '합성 인용 2', ?)",
               (first, version, H))
    assert db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] == 2


def test_foreign_keys_are_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        add_version(db, 999, 1, H)


def test_status_tool_reads_without_migrating(tmp_path):
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    assert tools.status(home)['error']['code'] == 'store_missing'  # tools never create or migrate the store
    assert not paths.db_path(home).exists()
    store.open_home(home).close()  # serve migrates once at start
    response = tools.status(home)
    assert response['ok'] and response['delivered_sources'] == []
    result = response['result']
    assert result['schema_version'] == 5
    assert result['db'] == {'documents': 0, 'versions': 0, 'chunks': 0, 'vectors': 0}
    assert result['model']['name'] == 'bge-m3' and result['model']['available'] is False


def test_status_tool_does_not_take_a_write_lock(tmp_path):
    """L-9: a long write transaction elsewhere does not block ``status``."""
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    writer = store.open_home(home)
    try:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute(r"INSERT INTO source_roots(path, added_at) VALUES('C:\synthetic', ?)", (NOW,))
        assert tools.status(home)['ok']
        writer.execute('ROLLBACK')
    finally:
        writer.close()


def test_read_connection_cannot_write(tmp_path):
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    store.open_home(home).close()
    reader = store.open_read(home)
    try:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            reader.execute(r"INSERT INTO source_roots(path, added_at) VALUES('C:\synthetic', ?)", (NOW,))
    finally:
        reader.close()


# M1 schema additions (design M1 §10.2)
def columns(db, table):
    return [r[1] for r in db.execute(f'PRAGMA table_info({table})')]


def test_m1_columns_tables_and_indexes(db):
    assert 'skip_reason' in columns(db, 'documents')
    assert columns(db, 'scans') == ['id', 'started_at', 'finished_at', 'status', 'partial', 'roots_json',
                                    'counts_json', 'note']
    index = {r[1]: r for r in db.execute('PRAGMA index_list(versions)')}
    assert [r[2] for r in db.execute('PRAGMA index_info(versions_by_sha256)')] == ['sha256', 'parser_version']
    assert index['versions_by_sha256'][2] == 0  # not unique: the same content may be in many documents


def test_document_can_be_skipped_with_a_reason(db):
    root = db.execute(r"INSERT INTO source_roots(path, added_at) VALUES('C:\synthetic', ?)", (NOW,)).lastrowid
    doc = db.execute(r"INSERT INTO documents(root_id, path, path_key, ext, status, skip_reason, first_seen_at, "
                     r"last_seen_at) VALUES(?, 'C:\synthetic\p.docx', 'c:\synthetic\p.docx', '.docx', 'skipped', "
                     r"'cloud_placeholder', ?, ?)", (root, NOW, NOW)).lastrowid
    assert db.execute('SELECT status, skip_reason FROM documents WHERE id=?', (doc,)).fetchone() == (
        'skipped', 'cloud_placeholder')


def test_scans_table_records_status_and_counts(db):
    scan = db.execute("INSERT INTO scans(started_at, status, roots_json) VALUES(?, 'running', '[\"C:\\synthetic\"]')",
                      (NOW,)).lastrowid
    db.execute("UPDATE scans SET status='done', finished_at=?, counts_json='{\"added\": 1}' WHERE id=?", (LATER, scan))
    assert db.execute('SELECT status, partial, counts_json FROM scans WHERE id=?', (scan,)).fetchone() == (
        'done', 0, '{"added": 1}')
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO scans(started_at, status, roots_json) VALUES(?, 'finished', '[]')", (NOW,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO scans(started_at, roots_json) VALUES(?, '[]')", (NOW,))  # status is required


def version_row(db, version):
    return db.execute('SELECT * FROM versions WHERE id=?', (version,)).fetchone()


@pytest.mark.parametrize('assignment', [
    "sha256='b'||substr(sha256, 2)", 'size=size+1', 'seq=seq+1', 'document_id=document_id+1',
    "observed_at='2000-01-01T00:00:00+09:00'", 'id=id+100', "parse_status='locked'"])
def test_version_identity_cannot_change(db, assignment):
    doc = add_document(db)
    db.execute(r"INSERT INTO documents(root_id, path, path_key, ext, status, first_seen_at, last_seen_at) "
               r"SELECT root_id, 'C:\synthetic\b.txt', 'c:\synthetic\b.txt', '.txt', 'active', ?, ? FROM documents",
               (NOW, NOW))  # a second document, so a changed document_id would satisfy the foreign key
    version = add_version(db, doc, 1, 'a' * 64)
    before = version_row(db, version)
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute(f'UPDATE versions SET {assignment} WHERE id=?', (version,))
    assert version_row(db, version) == before


def test_superseded_at_is_set_once(db):
    version = add_version(db, add_document(db), 1, 'a' * 64)
    db.execute('UPDATE versions SET superseded_at=? WHERE id=?', (NOW, version))
    for value in (LATER, None):
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            db.execute('UPDATE versions SET superseded_at=? WHERE id=?', (value, version))
    assert db.execute('SELECT superseded_at FROM versions WHERE id=?', (version,)).fetchone() == (NOW,)


def test_version_observation_updates_that_are_allowed(db):
    """touched (mtime_ns), supersede (NULL -> value), retry/--reparse (parser columns)."""
    version = add_version(db, add_document(db), 1, 'a' * 64)
    db.execute('UPDATE versions SET mtime_ns=99 WHERE id=?', (version,))
    db.execute('UPDATE versions SET superseded_at=? WHERE id=?', (LATER, version))
    db.execute("UPDATE versions SET parser='text', parser_version='text-2', encoding='utf-8', parse_status='failed', "
               "parse_error='encoding_unknown', unit_count=0, text_chars=0 WHERE id=?", (version,))
    db.execute("UPDATE versions SET parse_status='ok', parse_error=NULL, unit_count=3, text_chars=30 WHERE id=?",
               (version,))
    assert db.execute('SELECT mtime_ns, superseded_at, parser_version, parse_status, unit_count FROM versions '
                      'WHERE id=?', (version,)).fetchone() == (99, LATER, 'text-2', 'ok', 3)


def test_locked_placeholder_gets_its_hash_once(db):
    version = add_version(db, add_document(db), 1, H, status='locked')
    db.execute("UPDATE versions SET sha256=?, size=42, mtime_ns=7, parse_status='ok', unit_count=1 WHERE id=?",
               ('c' * 64, version))
    assert db.execute('SELECT sha256, size, parse_status FROM versions WHERE id=?', (version,)).fetchone() == (
        'c' * 64, 42, 'ok')
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):  # no longer locked: hash is fixed
        db.execute("UPDATE versions SET sha256=? WHERE id=?", ('d' * 64, version))


def test_versions_cannot_be_deleted(db):
    version = add_version(db, add_document(db), 1, 'a' * 64)
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute('DELETE FROM versions WHERE id=?', (version,))
    assert db.execute('SELECT COUNT(*) FROM versions').fetchone()[0] == 1


VERSION_COLUMNS = 'id, document_id, seq, sha256, size, mtime_ns, observed_at, parse_status'


@pytest.mark.parametrize('verb', ['INSERT OR REPLACE INTO', 'REPLACE INTO'])
@pytest.mark.parametrize('key', ['id', 'document_seq'])
def test_versions_cannot_be_replaced_through_either_key(db, verb, key):
    doc = add_document(db)
    first, second = add_version(db, doc, 1, 'a' * 64), add_version(db, doc, 2, 'b' * 64)
    before = db.execute('SELECT * FROM versions ORDER BY id').fetchall()
    values = (first, doc, 9) if key == 'id' else (None, doc, 2)  # same id / same (document_id, seq)
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute(f"{verb} versions({VERSION_COLUMNS}) VALUES(?, ?, ?, ?, 5, 5, ?, 'ok')", (*values, 'e' * 64, NOW))
    assert db.execute('SELECT * FROM versions ORDER BY id').fetchall() == before
    assert second == first + 1


def test_version_upsert_on_document_seq_is_refused(db):
    doc = add_document(db)
    version = add_version(db, doc, 1, 'a' * 64)
    before = version_row(db, version)
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute("INSERT INTO versions(document_id, seq, sha256, size, mtime_ns, observed_at, parse_status) "
                   "VALUES(?, 1, ?, 5, 5, ?, 'ok') ON CONFLICT(document_id, seq) DO UPDATE SET mtime_ns=5",
                   (doc, 'e' * 64, NOW))
    assert version_row(db, version) == before


def test_versions_unique_keys_are_id_and_document_seq_only(db):
    """Both keys are guarded above; a third UNIQUE key would need its own guard."""
    unique = [r[1] for r in db.execute('PRAGMA index_list(versions)') if r[2]]
    assert len(unique) == 1
    assert [r[2] for r in db.execute(f'PRAGMA index_info({unique[0]})')] == ['document_id', 'seq']


# M2 schema edits and the chunk/FTS functions (design M2 §1.4, §4.3)
def chunk_spec(ordinal, text, unit_first=1, unit_last=1):
    from workmcp import chunks
    return chunks.ChunkSpec(ordinal, unit_first, unit_last, 0, len(text), text)


def eligible_version(db, doc, seq, sha256, *, status='ok', units=1):
    return db.execute("INSERT INTO versions(document_id, seq, sha256, size, mtime_ns, observed_at, parse_status, "
                      "unit_count) VALUES(?, ?, ?, 1, 1, ?, ?, ?)", (doc, seq, sha256, NOW, status, units)).lastrowid


def fts_match(db, phrase):
    return [r[0] for r in db.execute('SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rowid',
                                     (f'"{phrase}"',))]


def test_schema_v1_has_content_addressed_vectors_meta_and_index_runs(db):
    assert columns(db, 'chunk_vectors') == ['model_key', 'text_sha256', 'dims', 'vector', 'embedded_at']
    primary = sorted((r[5], r[1]) for r in db.execute('PRAGMA table_info(chunk_vectors)') if r[5])
    assert [name for _, name in primary] == ['model_key', 'text_sha256']
    assert db.execute('PRAGMA foreign_key_list(chunk_vectors)').fetchall() == []
    assert [r[2] for r in db.execute('PRAGMA index_info(chunks_text)')] == ['text_sha256']
    assert columns(db, 'meta') == ['key', 'value', 'updated_at']
    assert columns(db, 'index_runs') == ['id', 'started_at', 'finished_at', 'status', 'model_key', 'chunk_version',
                                         'counts_json', 'note']
    run = db.execute("INSERT INTO index_runs(started_at, status, chunk_version) VALUES(?, 'running', 'v')",
                     (NOW,)).lastrowid
    db.execute("UPDATE index_runs SET status='done', finished_at=? WHERE id=?", (LATER, run))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO index_runs(started_at, status, chunk_version) VALUES(?, 'finished', 'v')", (NOW,))
    vector = (b'\0' * 16, NOW)
    db.execute("INSERT INTO chunk_vectors VALUES('m', ?, 4, ?, ?)", (H, *vector))
    with pytest.raises(sqlite3.IntegrityError):  # one row per (model_key, text_sha256)
        db.execute("INSERT INTO chunk_vectors VALUES('m', ?, 4, ?, ?)", (H, *vector))
    db.execute("INSERT INTO chunk_vectors VALUES('other', ?, 4, ?, ?)", (H, *vector))


def test_evidence_chunk_id_has_no_foreign_key(db):
    assert {r[2] for r in db.execute('PRAGMA foreign_key_list(evidence)')} == {'assertions', 'versions'}
    version = add_version(db, add_document(db), 1, 'a' * 64)
    assertion = insert_assertion(db)
    db.execute("INSERT INTO evidence(assertion_id, version_id, chunk_id, locator_json, quote, quote_sha256) "
               "VALUES(?, ?, 999, '{}', '합성 인용', ?)", (assertion, version, H))  # no such chunk: still stored
    assert db.execute('SELECT chunk_id FROM evidence').fetchone() == (999,)


@pytest.mark.skipif(not store.fts5_available(), reason='SQLite without FTS5 trigram')
def test_insert_and_drop_chunks_keep_fts_in_sync(db):
    doc = add_document(db)
    first, second = eligible_version(db, doc, 1, 'a' * 64), eligible_version(db, doc, 2, 'b' * 64)
    assert store.insert_chunks(db, first, 'cv1', [chunk_spec(1, '합성자료 회의록 첫째'), chunk_spec(2, '합성자료 견적서 둘째')]) == 2
    assert store.insert_chunks(db, second, 'cv1', copy_from=first) == 2
    assert store.insert_chunks(db, first, 'cv0', [chunk_spec(1, '옛 규칙 회의록')]) == 1
    assert len(fts_match(db, '회의록')) == 3 and store.fts_status(db)['consistent']
    row = db.execute("SELECT text, text_sha256 FROM chunks WHERE version_id=? AND ordinal=2", (second,)).fetchone()
    assert row == ('합성자료 견적서 둘째', chunk_spec(2, '합성자료 견적서 둘째').text_sha256())
    with pytest.raises(ValueError):
        store.insert_chunks(db, first, 'cv1', [chunk_spec(1, '두 번')])
    with pytest.raises(ValueError):
        store.insert_chunks(db, first, 'cv2', [chunk_spec(1, 'x')], copy_from=second)
    db.execute("INSERT INTO chunk_vectors VALUES('m', ?, 4, ?, ?)", (row[1], b'\0' * 16, NOW))
    assert store.drop_chunks(db, first, keep='cv1') == 1  # only the other chunk version
    assert len(fts_match(db, '회의록')) == 2 and fts_match(db, '옛 규칙') == []
    assert store.drop_chunks(db, first) == 2
    assert store.drop_chunks(db, first) == 0
    remaining = [r[0] for r in db.execute('SELECT id FROM chunks ORDER BY id')]
    assert fts_match(db, '합성자료') == remaining and len(remaining) == 2
    status = store.fts_status(db)
    assert (status['rows'], status['chunks'], status['consistent']) == (2, 2, True)
    assert db.execute('SELECT COUNT(*) FROM chunk_vectors').fetchone()[0] == 1  # vectors are not touched
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")  # raises if FTS and chunks disagree


@pytest.mark.skipif(not store.fts5_available(), reason='SQLite without FTS5 trigram')
def test_fts_status_reports_mismatch_and_rebuild_fixes_it(db, monkeypatch):
    version = eligible_version(db, add_document(db), 1, 'a' * 64)
    with monkeypatch.context() as patch:
        patch.setattr(store, 'fts5_available', lambda: False)  # written by a Python without FTS5
        store.insert_chunks(db, version, 'cv1', [chunk_spec(1, '합성자료 회의록'), chunk_spec(2, '합성자료 견적서')])
    status = store.fts_status(db)
    assert (status['available'], status['rows'], status['chunks'], status['consistent']) == (True, 0, 2, False)
    assert store.meta_get(db, 'fts_rows') is None
    assert store.rebuild_fts(db) == 2
    status = store.fts_status(db)
    assert status['consistent'] and status['synced_at'] and store.meta_get(db, 'fts_rows') == '2'
    assert len(fts_match(db, '합성자료')) == 2


def test_fts_absent_python_still_opens_and_indexes(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'fts5_available', lambda: False)
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    db = store.open_home(home)
    try:
        assert 'chunks_fts' not in names(db, 'table') and not store.fts_available(db)
        version = eligible_version(db, add_document(db), 1, 'a' * 64)
        assert store.insert_chunks(db, version, 'cv1', [chunk_spec(1, '합성 회의록')]) == 1
        assert store.fts_status(db) == {'available': False, 'rows': None, 'chunks': 1, 'missing': None,
                                        'extra': None, 'synced_at': None, 'dirty': True, 'consistent': False}
        assert store.rebuild_fts(db) == -1
        assert store.drop_chunks(db, version) == 1
    finally:
        db.close()


def test_meta_values_are_set_and_replaced(db):
    assert store.meta_get(db, 'index_generation') is None
    assert store.meta_get(db, 'index_generation', '0') == '0'
    store.meta_set(db, 'index_generation', 1)
    store.meta_set(db, 'index_generation', '2')
    assert store.meta_get(db, 'index_generation') == '2'
    assert db.execute('SELECT COUNT(*) FROM meta').fetchone()[0] == 1


def test_latest_chunks_sql_selects_latest_active_ok_versions_only(db):
    doc = add_document(db)
    old = eligible_version(db, doc, 1, 'a' * 64)
    latest = eligible_version(db, doc, 2, 'b' * 64)
    db.execute('UPDATE versions SET superseded_at=? WHERE id=?', (LATER, old))
    other = db.execute(r"INSERT INTO documents(root_id, path, path_key, ext, status, first_seen_at, last_seen_at) "
                       r"SELECT root_id, 'C:\synthetic\b.txt', 'c:\synthetic\b.txt', '.txt', 'deleted', ?, ? "
                       r"FROM documents", (NOW, NOW)).lastrowid
    gone = eligible_version(db, other, 1, 'c' * 64)
    third = db.execute(r"INSERT INTO documents(root_id, path, path_key, ext, status, first_seen_at, last_seen_at) "
                       r"SELECT root_id, 'C:\synthetic\c.txt', 'c:\synthetic\c.txt', '.txt', 'active', ? , ? "
                       r"FROM documents LIMIT 1", (NOW, NOW)).lastrowid
    failed = eligible_version(db, third, 1, 'd' * 64, status='failed')
    empty_doc = db.execute(r"INSERT INTO documents(root_id, path, path_key, ext, status, first_seen_at, last_seen_at) "
                           r"SELECT root_id, 'C:\synthetic\d.txt', 'c:\synthetic\d.txt', '.txt', 'active', ? , ? "
                           r"FROM documents LIMIT 1", (NOW, NOW)).lastrowid
    empty = eligible_version(db, empty_doc, 1, 'e' * 64, units=0)
    for version in (old, latest, gone, failed, empty):
        store.insert_chunks(db, version, 'cv1', [chunk_spec(1, f'합성 청크 {version}')])
    assert [r[0] for r in db.execute(f'SELECT v.id FROM {store.eligible_versions_sql()} ORDER BY v.id')] == [latest]
    rows = db.execute(f'SELECT c.version_id FROM {store.latest_chunks_sql()} AND c.chunk_version = ?', ('cv1',))
    assert [r[0] for r in rows] == [latest]


def test_index_lock_is_separate_from_the_scan_lock(tmp_path):
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    scan_lock, _ = store.acquire_scan_lock(home)
    index_lock, warning = store.acquire_lock(home, store.INDEX_LOCK_NAME)
    assert warning is None and index_lock == store.lock_path(home, 'index.lock') != scan_lock
    with pytest.raises(store.Locked, match='another index is running') as refused:
        store.acquire_lock(home, store.INDEX_LOCK_NAME)
    assert not isinstance(refused.value, store.ScanLocked)
    with pytest.raises(store.ScanLocked, match='another scan is running'):
        store.acquire_scan_lock(home)
    store.release_lock(index_lock)
    store.release_scan_lock(scan_lock)
    assert not index_lock.exists() and not scan_lock.exists()


@pytest.mark.skipif(not store.fts5_available(), reason='SQLite without FTS5 trigram')
def test_drop_chunks_removes_only_the_rows_the_fts_index_holds(db, monkeypatch):
    doc = add_document(db)
    unindexed, indexed = eligible_version(db, doc, 1, 'a' * 64), eligible_version(db, doc, 2, 'b' * 64)
    with monkeypatch.context() as patch:
        patch.setattr(store, 'fts5_available', lambda: False)
        store.insert_chunks(db, unindexed, 'cv1', [chunk_spec(1, '합성자료 색인 밖')])
    # once a write skipped the index, later writes leave it alone too until a rebuild (review M2-2)
    store.insert_chunks(db, indexed, 'cv1', [chunk_spec(1, '합성자료 색인 안')])
    assert store.drop_chunks(db, unindexed) == 1
    status = store.fts_status(db)
    assert status['dirty'] and not status['consistent'] and (status['missing'], status['extra']) == (1, 0)
    assert store.rebuild_fts(db) == 1
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")
    assert store.fts_status(db)['consistent'] and len(fts_match(db, '합성자료')) == 1


@pytest.mark.skipif(not store.fts5_available(), reason='SQLite without FTS5 trigram')
def test_fts_write_without_fts5_is_never_taken_as_consistent(db, monkeypatch):
    """Review M2-2: equal row counts (even reused ids) do not hide a write that skipped the index."""
    version = eligible_version(db, add_document(db), 1, 'a' * 64)
    store.insert_chunks(db, version, 'cv1', [chunk_spec(1, '합성자료 첫째'), chunk_spec(2, '합성자료 둘째')])
    assert store.fts_status(db)['consistent']
    with monkeypatch.context() as patch:
        patch.setattr(store, 'fts5_available', lambda: False)
        store.drop_chunks(db, version, keep='none')       # both rows gone from chunks, still in the index
        store.insert_chunks(db, version, 'cv2', [chunk_spec(1, '새 합성자료 하나'), chunk_spec(2, '새 합성자료 둘')])
    status = store.fts_status(db)
    assert (status['rows'], status['chunks']) == (2, 2)     # the counts agree, and the ids are reused ...
    assert status['dirty'] and not status['consistent']     # ... the skipped write still shows
    assert fts_match(db, '새 합성자료') == []                # the index holds the old texts
    assert store.rebuild_fts(db) == 2 and store.fts_status(db)['consistent']
    assert len(fts_match(db, '새 합성자료')) == 2


@pytest.mark.skipif(not store.fts5_available(), reason='SQLite without FTS5 trigram')
def test_fts_differences_count_missing_and_stale_ids(db):
    version = eligible_version(db, add_document(db), 1, 'a' * 64)
    store.insert_chunks(db, version, 'cv1', [chunk_spec(1, '합성자료 첫째'), chunk_spec(2, '합성자료 둘째')])
    assert store.fts_differences(db) == (0, 0)
    first = db.execute('SELECT id, text FROM chunks ORDER BY id').fetchone()
    db.execute("INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', ?, ?)", first)  # outside store
    assert store.fts_differences(db) == (1, 0) and not store.fts_status(db)['consistent']
    db.execute('INSERT INTO chunks_fts(rowid, text) VALUES(?, ?)', first)
    db.execute("INSERT INTO chunks_fts(rowid, text) VALUES(9999, '유령 항목')")
    assert store.fts_differences(db) == (0, 1) and not store.fts_status(db)['consistent']


# Migration v2 (design M3 §3.4)
ROOT = Path(__file__).resolve().parents[1]
CALL = {'at': NOW, 'client': 'test-client', 'client_version': '1.0', 'tool': 'search', 'call_id': 'c' * 16,
        'args_json': {'mode': 'hybrid', 'query_chars': 5}, 'pieces': 2, 'chars': 30, 'ok': True, 'elapsed_ms': 1.5,
        'pid': 1}


def test_migration_v2_adds_tool_calls_events_and_call_id(db):
    assert columns(db, 'tool_calls') == ['id', 'at', 'client', 'client_version', 'tool', 'call_id', 'args_json',
                                         'pieces', 'chars', 'ok', 'error_code', 'elapsed_ms', 'pid']
    assert columns(db, 'deliveries') == ['id', 'at', 'client', 'tool', 'locator_json', 'chars', 'call_id']
    assert columns(db, 'document_events') == ['id', 'at', 'scan_id', 'document_id', 'version_id', 'kind']
    unique = {r[1] for r in db.execute('PRAGMA index_list(tool_calls)') if r[2]}
    assert [[c[2] for c in db.execute(f'PRAGMA index_info({name})')] for name in unique] == [['call_id']]


def test_migration_v2_upgrades_a_v1_store_in_place(tmp_path, monkeypatch):
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    monkeypatch.setattr(store, 'MIGRATIONS', store.MIGRATIONS[:1])
    old = store.open_home(home)
    old.execute("INSERT INTO deliveries(at, client, tool, locator_json, chars) VALUES(?, 'c', 't', '{}', 3)", (NOW,))
    old.close()
    monkeypatch.undo()
    upgraded = store.open_home(home)
    try:
        assert [r[0] for r in upgraded.execute('SELECT version FROM schema_migrations ORDER BY version')] == [1, 2, 3, 4, 5]
        assert upgraded.execute('SELECT chars, call_id FROM deliveries').fetchall() == [(3, '')]
        assert store.migrate(upgraded) == []
    finally:
        upgraded.close()


def test_migrations_apply_once_under_two_processes(tmp_path):
    """Two serve processes may start together; BEGIN IMMEDIATE lets one apply v2 and v3 (design M3 §7,
    M4 §2.3)."""
    home = tmp_path/'home'
    code = ('import sys\nfrom workmcp import paths, store\n'
            'home = paths.ensure_home(paths.validate_home(sys.argv[1], {}, None))\n'
            'db = store.open_home(home)\n'
            'print(db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])\ndb.close()\n')
    env = {**os.environ, 'PYTHONPATH': str(ROOT/'src')}
    runs = [subprocess.Popen([sys.executable, '-c', code, str(home)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=env) for _ in range(2)]
    outputs = [run.communicate(timeout=120) for run in runs]
    assert [run.returncode for run in runs] == [0, 0], outputs
    assert [out.strip() for out, _ in outputs] == ['5', '5']
    check = sqlite3.connect(paths.db_path(home))
    try:
        assert check.execute('SELECT version FROM schema_migrations ORDER BY version').fetchall() == [
            (1,), (2,), (3,), (4,), (5,)]
    finally:
        check.close()


def add_call(db, **changes):
    return store.record_call(db, {**CALL, **changes})


@pytest.mark.parametrize('table', ['tool_calls', 'deliveries', 'document_events'])
def test_v2_tables_are_append_only(db, table):
    doc = add_document(db)
    if table == 'tool_calls':
        key = add_call(db)
    elif table == 'deliveries':
        store.record_deliveries(db, 'c' * 16, [{'at': NOW, 'client': 'c', 'tool': 't', 'locator_json': {}, 'chars': 1}])
        key = db.execute('SELECT id FROM deliveries').fetchone()[0]
    else:
        key = store.add_document_event(db, None, doc, None, 'deleted', NOW)
    before = full_row(db, table, key)
    column = {'tool_calls': 'client', 'deliveries': 'client', 'document_events': 'kind'}[table]
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute(f"UPDATE {table} SET {column}='added' WHERE id=?", (key,))
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute(f'DELETE FROM {table} WHERE id=?', (key,))
    names = [c for c in columns(db, table) if c != 'id']
    values = [v for c, v in zip(columns(db, table), before) if c != 'id']
    for verb in ('INSERT OR REPLACE INTO', 'REPLACE INTO'):
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            db.execute(f"{verb} {table}(id, {', '.join(names)}) VALUES(?{', ?' * len(names)})", (key, *values))
    assert full_row(db, table, key) == before
    assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 1


def test_tool_call_cannot_be_replaced_through_its_call_id(db):
    add_call(db)
    before = db.execute('SELECT * FROM tool_calls').fetchall()
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute("INSERT OR REPLACE INTO tool_calls(at, client, tool, call_id, ok) VALUES(?, 'other', 'status', ?, 1)",
                   (LATER, CALL['call_id']))
    assert db.execute('SELECT * FROM tool_calls').fetchall() == before


def test_record_call_and_deliveries_keep_summaries_under_one_call_id(db):
    call = add_call(db)
    obj = {'v': 1, 'doc': 1, 'path': 'C:\\synthetic\\a.txt', 'sha256': H, 'unit': {'kind': 'line', 'line': 1},
           'chars': [0, 5], 'text_sha256': H}
    assert store.record_deliveries(db, CALL['call_id'], [
        {'at': NOW, 'client': 'test-client', 'tool': 'search', 'locator_json': obj, 'chars': 5},
        {'at': NOW, 'client': 'test-client', 'tool': 'search', 'locator_json': json.dumps(obj), 'chars': 25}]) == 2
    assert store.record_deliveries(db, 'none', []) == 0
    row = db.execute('SELECT client, client_version, tool, args_json, pieces, chars, ok, error_code FROM tool_calls '
                     'WHERE id=?', (call,)).fetchone()
    assert row == ('test-client', '1.0', 'search', '{"mode":"hybrid","query_chars":5}', 2, 30, 1, None)
    rows = db.execute('SELECT locator_json, chars FROM deliveries WHERE call_id=? ORDER BY id',
                      (CALL['call_id'],)).fetchall()
    assert [json.loads(text) for text, _ in rows] == [obj, obj] and [n for _, n in rows] == [5, 25]
    add_call(db, call_id='d' * 16, client='other', ok=False, error_code='empty_query', pieces=0, chars=0)
    assert store.call_counts(db) == {'total': 2, 'by_client': {'other': 1, 'test-client': 1}}


def test_record_functions_refuse_unknown_columns(db):
    with pytest.raises(ValueError, match='unknown tool_calls columns'):
        add_call(db, query='합성 질의')
    with pytest.raises(ValueError, match='unknown deliveries columns'):
        store.record_deliveries(db, 'x', [{'at': NOW, 'client': 'c', 'tool': 't', 'locator_json': {}, 'chars': 1,
                                           'text': '합성 본문'}])
    with pytest.raises(ValueError, match='unknown document event kind'):
        store.add_document_event(db, None, add_document(db), None, 'locked')


def add_named_document(db, root, name, ext='.txt', status='active', deleted_at=None):
    path = f'C:\\synthetic\\{name}'
    return db.execute('INSERT INTO documents(root_id, path, path_key, ext, status, first_seen_at, last_seen_at, '
                      'deleted_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)',
                      (root, path, os.path.normcase(path), ext, status, NOW, NOW, deleted_at)).lastrowid


def test_documents_query_filters_pages_latest_and_indexed(db):
    root = db.execute(r"INSERT INTO source_roots(path, added_at) VALUES('C:\synthetic', ?)", (NOW,)).lastrowid
    a = add_named_document(db, root, 'a.txt')
    b = add_named_document(db, root, 'b.md', '.md')
    c = add_named_document(db, root, 'sub\\c.txt', status='deleted', deleted_at=LATER)
    add_named_document(db, root, 'd.pdf', '.pdf', status='skipped')
    eligible_version(db, a, 1, 'a' * 64)
    db.execute('UPDATE versions SET superseded_at=? WHERE document_id=? AND seq=1', (LATER, a))
    newest = eligible_version(db, a, 2, 'b' * 64)
    eligible_version(db, b, 1, 'c' * 64)
    eligible_version(db, c, 1, 'd' * 64)
    store.insert_chunks(db, newest, 'cv1', [chunk_spec(1, '합성자료 하나')])
    rows, total = store.documents_query(db, {}, 10, 0, chunk_version='cv1')
    assert total == 2 and [r['id'] for r in rows] == [a, b]
    assert set(rows[0]) == {'id', 'path', 'ext', 'status', 'skip_reason', 'root_id', 'first_seen_at', 'last_seen_at',
                            'deleted_at', 'latest', 'indexed'}
    assert rows[0]['latest']['version_id'] == newest and rows[0]['latest']['seq'] == 2
    assert [r['indexed'] for r in rows] == [True, False]
    assert store.documents_query(db, {}, 10, 0, chunk_version='other')[0][0]['indexed'] is False
    everything, total = store.documents_query(db, {'status': 'all'}, 2, 1, chunk_version='cv1')
    assert total == 4 and [r['path'] for r in everything] == ['C:\\synthetic\\b.md', 'C:\\synthetic\\d.pdf']
    assert everything[1]['latest'] is None
    assert [r['id'] for r in store.documents_query(db, {'status': 'deleted'}, 10, 0, chunk_version='cv1')[0]] == [c]
    assert store.documents_query(db, {'status': 'all', 'ext': ['.md', '.pdf']}, 10, 0, chunk_version='cv1')[1] == 2
    prefix = os.path.normcase('C:\\synthetic\\sub')
    assert store.documents_query(db, {'status': 'all', 'path_prefix': prefix}, 10, 0, chunk_version='cv1')[1] == 1
    changed = store.documents_query(db, {'status': 'all', 'changed_since': LATER}, 10, 0, chunk_version='cv1')
    assert [r['id'] for r in changed[0]] == [c]  # deleted at LATER; every version was observed at NOW


def test_document_events_query_filters_orders_and_pages(db):
    root = db.execute(r"INSERT INTO source_roots(path, added_at) VALUES('C:\synthetic', ?)", (NOW,)).lastrowid
    a = add_named_document(db, root, 'a.txt')
    b = add_named_document(db, root, 'b.md', '.md')
    va = eligible_version(db, a, 1, 'a' * 64)
    vb = eligible_version(db, b, 1, 'b' * 64)
    scan = db.execute("INSERT INTO scans(started_at, status, roots_json) VALUES(?, 'done', '[]')", (NOW,)).lastrowid
    first = store.add_document_event(db, scan, a, va, 'added', '2026-10-23T09:00:00+09:00')
    second = store.add_document_event(db, scan, b, vb, 'added', '2026-10-23T09:00:00+09:00')
    third = store.add_document_event(db, scan, a, None, 'deleted', '2026-10-23T01:30:00+00:00')  # 10:30 +09:00
    rows, total = store.document_events_query(db, {}, 10, 0)
    assert total == 3 and [r['id'] for r in rows] == [third, second, first]  # newest first, by instant
    assert set(rows[0]) == {'id', 'at', 'kind', 'doc', 'version_id', 'seq', 'sha256', 'parse_status', 'parse_error',
                            'scan_id'}
    assert rows[0]['doc'] == {'id': a, 'path': 'C:\\synthetic\\a.txt', 'ext': '.txt', 'status': 'active'}
    assert rows[0]['version_id'] is None and rows[0]['seq'] is None and rows[0]['scan_id'] == scan
    assert rows[1]['seq'] == 1 and rows[1]['sha256'] == 'b' * 64 and rows[1]['parse_status'] == 'ok'
    assert [r['id'] for r in store.document_events_query(db, {'kinds': ['added']}, 10, 0)[0]] == [second, first]
    assert [r['id'] for r in store.document_events_query(db, {'ext': ['.md']}, 10, 0)[0]] == [second]
    since = store.document_events_query(db, {'since': '2026-10-23T10:00:00+09:00'}, 10, 0)[0]
    assert [r['id'] for r in since] == [third]
    until = store.document_events_query(db, {'until': '2026-10-23T09:30:00+09:00'}, 10, 0)[0]
    assert [r['id'] for r in until] == [second, first]
    page, total = store.document_events_query(db, {}, 1, 1)
    assert total == 3 and [r['id'] for r in page] == [second]


def test_an_up_to_date_store_opens_while_another_connection_holds_the_write_lock(tmp_path, monkeypatch):
    """Review M3 v2 N1: ``migrate`` sees by reading alone that nothing is pending, so opening the store
    (``serve`` start, any command) does not wait for a running scan's write transaction."""
    import time
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    store.open_home(home).close()
    other = store.open_home(home)
    other.execute('BEGIN IMMEDIATE')
    try:
        began = time.perf_counter()
        opened = store.open_home(home)
        assert time.perf_counter() - began < 1.0 and store.migrate(opened) == []
        opened.close()
        # the read-only check is what makes it work: forced into the write path it waits and fails
        monkeypatch.setattr(store, '_up_to_date', lambda db: False)
        late = sqlite3.connect(paths.db_path(home), isolation_level=None)
        late.execute('PRAGMA busy_timeout=100')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            store.migrate(late)
        late.close()
    finally:
        other.execute('ROLLBACK')
        other.close()


def test_the_read_only_check_still_refuses_a_changed_checksum(tmp_path):
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    db = store.open_home(home)
    try:
        db.execute("UPDATE schema_migrations SET checksum='0' WHERE version=2")
        with pytest.raises(RuntimeError, match='checksum changed'):
            store.migrate(db)
    finally:
        db.close()


# ---- migration v3: knowledge columns, assertion events, entity/link guards (design M4 §2.3) --------

def add_entity(db, name='합성 과제', kind='project'):
    return db.execute("INSERT INTO entities(kind, name, created_at, created_by) VALUES(?, ?, ?, 'test/record_entity')",
                      (kind, name, NOW)).lastrowid


def add_fact(db, entity, predicate='담당', value='합성 값', valid_from=None, valid_to=None, object_entity=None):
    data = json.dumps({'unit': None, 'value': value}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return db.execute(
        "INSERT INTO assertions(kind, entity_id, predicate, object_entity_id, statement, data_json, valid_from, "
        "valid_to, recorded_from, status, source_kind, recorded_by) "
        "VALUES('fact', ?, ?, ?, '합성 사실', ?, ?, ?, ?, 'active', 'my_note', 'test/record_fact')",
        (entity, predicate, object_entity, data, valid_from, valid_to, NOW)).lastrowid


def add_link(db, source, target, relation='conflicts'):
    return db.execute("INSERT INTO links(from_id, to_id, relation, recorded_at, recorded_by) "
                      "VALUES(?, ?, ?, ?, 'test/record_fact')", (source, target, relation, NOW)).lastrowid


def add_assertion_event(db, assertion, kind='recorded'):
    return db.execute("INSERT INTO assertion_events(assertion_id, at, kind, recorded_by, call_id) "
                      "VALUES(?, ?, ?, 'test/record_fact', 'c0')", (assertion, NOW, kind)).lastrowid


@pytest.fixture
def known(db):
    """Two entities, two facts about the first, a conflict link between them and one event."""
    first, second = add_entity(db, '합성 과제 A'), add_entity(db, '합성 사람 B', 'person')
    fact = add_fact(db, first, valid_from='2026-10-01')
    other = add_fact(db, first, value='다른 합성 값', object_entity=second)
    return {'entity': first, 'person': second, 'fact': fact, 'other': other, 'link': add_link(db, fact, other),
            'event': add_assertion_event(db, fact)}


def knowledge_rows(db):
    return {table: db.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
            for table in ('entities', 'assertions', 'links', 'assertion_events')}


def test_migration_v3_adds_knowledge_columns_tables_triggers_indexes(db):
    assert store.SCHEMA_VERSION == 5 and store.MIGRATIONS[2][:2] == (3, 'knowledge_v3')
    assert columns(db, 'assertions')[-2:] == ['predicate', 'object_entity_id']
    assert columns(db, 'assertion_events') == ['id', 'assertion_id', 'at', 'kind', 'reason', 'related_id',
                                               'recorded_by', 'call_id']
    keys = {(r[3], r[2], r[4]) for r in db.execute('PRAGMA foreign_key_list(assertions)')}
    assert ('object_entity_id', 'entities', 'id') in keys
    assert {(r[3], r[2]) for r in db.execute('PRAGMA foreign_key_list(assertion_events)')} == {
        ('assertion_id', 'assertions'), ('related_id', 'assertions')}
    unique = {table: [[c[2] for c in db.execute(f'PRAGMA index_info({r[1]})')]
                      for r in db.execute(f'PRAGMA index_list({table})') if r[2]]
              for table in ('entities', 'links', 'assertion_events')}
    # every unique key is guarded against REPLACE: entities by id and (kind, name), the others by id only
    assert unique == {'entities': [['kind', 'name']], 'links': [], 'assertion_events': []}
    assert [[c[2] for c in db.execute('PRAGMA index_info(assertions_subject)')]] == [['entity_id', 'kind', 'predicate']]


def test_migration_v3_upgrades_a_v2_store_in_place(tmp_path, monkeypatch):
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    monkeypatch.setattr(store, 'MIGRATIONS', store.MIGRATIONS[:2])
    old = store.open_home(home)
    before = old.execute("INSERT INTO assertions(kind, statement, recorded_from, status, source_kind, recorded_by) "
                         "VALUES('fact', '합성 v2 사실', ?, 'active', 'my_note', 't')", (NOW,)).lastrowid
    old.close()
    monkeypatch.undo()
    upgraded = store.open_home(home)
    try:
        assert [r[0] for r in upgraded.execute('SELECT version FROM schema_migrations ORDER BY version')] == [1, 2, 3, 4, 5]
        assert upgraded.execute('SELECT predicate, object_entity_id FROM assertions WHERE id=?', (before,)).fetchone() \
            == (None, None)
        # a row from before v3 can still be closed (the shape trigger checks inserts only)
        upgraded.execute("UPDATE assertions SET status='withdrawn', recorded_to=?, change_reason='합성' WHERE id=?",
                         (LATER, before))
        assert store.migrate(upgraded) == []
    finally:
        upgraded.close()


FACT_INSERT = ("INSERT INTO assertions(kind, entity_id, predicate, object_entity_id, statement, valid_from, valid_to, "
               "recorded_from, status, source_kind, recorded_by) VALUES(?, ?, ?, ?, '합성', ?, ?, ?, 'active', "
               "'my_note', 't')")


def insert_shaped(db, kind, entity, predicate, object_entity=None, valid_from=None, valid_to=None):
    return db.execute(FACT_INSERT, (kind, entity, predicate, object_entity, valid_from, valid_to, NOW)).lastrowid


SHAPE = 'assertion shape'
DATES = 'valid_from/valid_to'


@pytest.mark.parametrize('case, match', [
    (lambda db, k: insert_shaped(db, 'fact', k['entity'], None), SHAPE),                       # fact without predicate
    (lambda db, k: insert_shaped(db, 'fact', None, '담당'), SHAPE),                             # fact without subject
    (lambda db, k: insert_shaped(db, 'todo', None, '담당'), SHAPE),                             # task with predicate
    (lambda db, k: insert_shaped(db, 'decision', None, None, k['person']), SHAPE),              # decision with object
    (lambda db, k: insert_shaped(db, 'fact', k['entity'], '담당', valid_from='2026-9-1'), DATES),
    (lambda db, k: insert_shaped(db, 'fact', k['entity'], '담당', valid_to='2026-10-01T00:00:00'), DATES),
    (lambda db, k: insert_shaped(db, 'fact', k['entity'], '담당', valid_from='2026-10-01', valid_to='2026-10-01'), DATES),
    (lambda db, k: insert_shaped(db, 'fact', k['entity'], '담당', valid_from='2026-10-02', valid_to='2026-10-01'), DATES),
    (lambda db, k: insert_shaped(db, 'decision', None, None, valid_from='어제'), DATES),
    (lambda db, k: insert_shaped(db, 'fact', k['entity'], '담당', 999), 'FOREIGN KEY'),         # no such object entity
    (lambda db, k: db.execute("UPDATE assertions SET predicate='담당자' WHERE id=?", (k['fact'],)), 'append-only'),
    (lambda db, k: db.execute('UPDATE assertions SET predicate=NULL WHERE id=?', (k['fact'],)), 'append-only'),
    (lambda db, k: db.execute('UPDATE assertions SET object_entity_id=? WHERE id=?', (k['entity'], k['other'])),
     'append-only'),
    (lambda db, k: db.execute("UPDATE entities SET name='합성 새 이름' WHERE id=?", (k['entity'],)), 'append-only'),
    (lambda db, k: db.execute("UPDATE entities SET kind='org' WHERE id=?", (k['entity'],)), 'append-only'),
    (lambda db, k: db.execute("UPDATE entities SET created_by='x', created_at=? WHERE id=?", (LATER, k['entity'])),
     'append-only'),
    (lambda db, k: db.execute('UPDATE entities SET id=50 WHERE id=?', (k['entity'],)), 'append-only'),
    (lambda db, k: db.execute('DELETE FROM entities WHERE id=?', (k['person'],)), 'append-only'),
    (lambda db, k: db.execute("REPLACE INTO entities(id, kind, name, created_at, created_by) "
                              "VALUES(?, 'project', '합성 덮어쓰기', ?, 't')", (k['entity'], NOW)), 'append-only'),
    (lambda db, k: db.execute("INSERT OR REPLACE INTO entities(kind, name, created_at, created_by) "
                              "VALUES('project', '합성 과제 A', ?, 't')", (NOW,)), 'append-only'),
    (lambda db, k: db.execute("INSERT INTO entities(kind, name, created_at, created_by) "
                              "VALUES('project', '합성 과제 A', ?, 't')", (NOW,)), 'append-only'),
    (lambda db, k: [db.execute('UPDATE entities SET merged_into=? WHERE id=?', (k['entity'], k['person'])),
                    db.execute('UPDATE entities SET merged_into=NULL WHERE id=?', (k['person'],))], 'append-only'),
    (lambda db, k: db.execute("UPDATE links SET relation='supersedes' WHERE id=?", (k['link'],)), 'append-only'),
    (lambda db, k: db.execute('UPDATE links SET to_id=from_id WHERE id=?', (k['link'],)), 'append-only'),
    (lambda db, k: db.execute("UPDATE links SET note='합성' WHERE id=?", (k['link'],)), 'append-only'),
    (lambda db, k: db.execute("UPDATE links SET recorded_by='x' WHERE id=?", (k['link'],)), 'append-only'),
    (lambda db, k: db.execute('UPDATE links SET resolved_by_id=? WHERE id=?', (k['fact'], k['link'])), 'append-only'),
    (lambda db, k: [db.execute('UPDATE links SET resolved_at=?, resolved_by_id=? WHERE id=?', (LATER, k['fact'], k['link'])),
                    db.execute('UPDATE links SET resolved_at=NULL, resolved_by_id=NULL WHERE id=?', (k['link'],))],
     'append-only'),
    (lambda db, k: [db.execute('UPDATE links SET resolved_at=? WHERE id=?', (LATER, k['link'])),
                    db.execute('UPDATE links SET resolved_by_id=? WHERE id=?', (k['other'], k['link']))], 'append-only'),
    (lambda db, k: db.execute('DELETE FROM links WHERE id=?', (k['link'],)), 'append-only'),
    (lambda db, k: db.execute("REPLACE INTO links(id, from_id, to_id, relation, recorded_at, recorded_by) "
                              "VALUES(?, ?, ?, 'supersedes', ?, 't')", (k['link'], k['other'], k['fact'], NOW)),
     'append-only'),
    (lambda db, k: db.execute("UPDATE assertion_events SET reason='합성' WHERE id=?", (k['event'],)), 'append-only'),
    (lambda db, k: db.execute('DELETE FROM assertion_events WHERE id=?', (k['event'],)), 'append-only'),
    (lambda db, k: db.execute("INSERT OR REPLACE INTO assertion_events(id, assertion_id, at, kind, recorded_by) "
                              "VALUES(?, ?, ?, 'retracted', 't')", (k['event'], k['fact'], NOW)), 'append-only'),
    (lambda db, k: db.execute("INSERT INTO assertion_events(assertion_id, at, kind, recorded_by) "
                              "VALUES(?, ?, 'edited', 't')", (k['fact'], NOW)), 'CHECK'),
])
def test_v3_guards_refuse_and_leave_every_row_as_it_was(db, known, case, match):
    """The refusals of design M4 §2.3 (and the design probe of 2026-10-25); a refused statement inside a
    transaction keeps every knowledge row as it was."""
    before = knowledge_rows(db)
    db.execute('BEGIN')
    with pytest.raises(sqlite3.IntegrityError, match=match):
        case(db, known)
    db.execute('ROLLBACK')
    assert knowledge_rows(db) == before


def test_v3_allows_what_the_knowledge_writer_needs(db, known):
    decision = insert_shaped(db, 'decision', None, None, valid_from='2026-10-25')           # no subject
    task = insert_shaped(db, 'todo', known['entity'], None, valid_from='2026-10-25', valid_to='2026-11-01')
    fact = insert_shaped(db, 'fact', known['entity'], '예산', valid_from='2026-01-01', valid_to='2026-12-31')
    assert decision and task and fact
    db.execute("UPDATE assertions SET status='conflict' WHERE id IN (?, ?)", (known['fact'], known['other']))
    db.execute("UPDATE entities SET aliases_json='[\"A\"]', note='합성 메모' WHERE id=?", (known['entity'],))
    db.execute("UPDATE entities SET aliases_json='[\"A\",\"에이\"]', note='합성 메모 2' WHERE id=?", (known['entity'],))
    db.execute('UPDATE entities SET merged_into=? WHERE id=?', (known['entity'], known['person']))  # once (M5)
    db.execute('UPDATE links SET resolved_at=?, resolved_by_id=? WHERE id=?', (LATER, known['other'], known['link']))
    db.execute("UPDATE assertions SET status='withdrawn', recorded_to=?, change_reason='합성 철회' WHERE id=?",
               (LATER, known['other']))
    db.execute("UPDATE assertions SET status='active' WHERE id=?", (known['fact'],))
    add_assertion_event(db, known['other'], 'retracted')
    add_assertion_event(db, known['fact'], 'conflict_cleared')
    add_link(db, fact, known['fact'], 'supersedes')
    assert db.execute('SELECT aliases_json, note, merged_into FROM entities ORDER BY id').fetchall() == [
        ('["A","에이"]', '합성 메모 2', None), ('[]', None, known['entity'])]
    assert db.execute('SELECT resolved_at, resolved_by_id FROM links WHERE id=?', (known['link'],)).fetchone() == \
        (LATER, known['other'])


def test_knowledge_counts_for_status(db, known):
    assert store.knowledge_counts(db) == {'entities': 2, 'assertions': {'active': 2, 'conflict': 0, 'withdrawn': 0,
                                                                        'superseded': 0},
                                          'tasks_open': 0, 'evidence': 0, 'events': 1}
    for state in ('open', 'open', 'done'):
        db.execute("INSERT INTO assertions(kind, statement, data_json, recorded_from, status, source_kind, recorded_by) "
                   "VALUES('todo', '합성 할 일', ?, ?, 'active', 'my_note', 't')", (json.dumps({'state': state}), NOW))
    db.execute("UPDATE assertions SET status='withdrawn', recorded_to=?, change_reason='합성' WHERE id=?",
               (LATER, known['other']))
    counts = store.knowledge_counts(db)
    assert counts['tasks_open'] == 2 and counts['assertions'] == {'active': 4, 'conflict': 0, 'withdrawn': 1,
                                                                  'superseded': 0}
    assert set(counts) == tools.KNOWLEDGE_STATUS_KEYS


# ---- migration v4: the unit lookup index (review M4 v2 N1) -----------------------------------------------

# The unit lookups of the package, exactly as written (knowledge_query.still_matches/requote, knowledge._targets/
# _copy_evidence, binding.validate_locator, snippet.by_locator).
UNIT_LOOKUPS = ('SELECT text FROM units WHERE version_id = ? AND locator_json = ?',
                'SELECT text FROM units WHERE version_id=? AND locator_json=?',
                'SELECT id, text FROM units WHERE version_id=? AND locator_json=?',
                'SELECT ordinal, text FROM units WHERE version_id = ? AND locator_json = ?')


def test_unit_lookups_in_the_package_are_the_indexed_forms():
    root = Path(__file__).resolve().parents[1]/'src'/'workmcp'
    found = set()
    for path in root.rglob('*.py'):
        text = ' '.join(path.read_text(encoding='utf-8').split())
        for match in re.finditer(r"'(SELECT [^']*FROM units WHERE version_id ?= ?\? AND locator_json ?= ?\?)'", text):
            found.add(match.group(1))
    assert found and found <= set(UNIT_LOOKUPS), found


@pytest.mark.parametrize('sql', UNIT_LOOKUPS)
def test_migration_v4_unit_lookups_use_the_locator_index(db, sql):
    version = add_version(db, add_document(db), 1, H)
    db.executemany('INSERT INTO units(version_id, ordinal, kind, locator_json, text, text_sha256) VALUES(?,?,?,?,?,?)',
                   [(version, n, 'line', json.dumps({'kind': 'line', 'line': n}), f'합성 {n}', H) for n in range(1, 500)])
    db.execute('ANALYZE')
    plan = ' '.join(row[3] for row in db.execute(f'EXPLAIN QUERY PLAN {sql}', (version, '{}')))
    assert 'USING INDEX units_locator (version_id=? AND locator_json=?)' in plan, plan
    assert [[c[2] for c in db.execute('PRAGMA index_info(units_locator)')]] == [['version_id', 'locator_json']]


def test_migration_v4_upgrades_a_v3_store_in_place(tmp_path, monkeypatch):
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    monkeypatch.setattr(store, 'MIGRATIONS', store.MIGRATIONS[:3])
    old = store.open_home(home)
    assert 'units_locator' not in names(old, 'index')
    old.close()
    monkeypatch.undo()
    upgraded = store.open_home(home)
    try:
        assert [r[0] for r in upgraded.execute('SELECT version FROM schema_migrations ORDER BY version')] == [1, 2, 3, 4, 5]
        assert 'units_locator' in names(upgraded, 'index') and store.migrate(upgraded) == []
    finally:
        upgraded.close()


# ---- migration v5: the operating server's collectors, tokens and push records (design M5 remote §2.2) -------

# Applied migrations never change (a real home holds these checksums): v1-v4 as they were before M5, v5 as added.
CHECKSUMS = {1: '8af56d4806389a3c203106e716761371491aec393673bb8d5b52164c5858feec',
             2: '4f4a3f6ca60a895ec9f4080123af85936df83caf63ffb07123b437dc158d1c0b',
             3: '8a5fb855fa097a080861523499f83f6a8ba0ae862a1fa08f8217e971d15c6592',
             4: '548f45f16d520dbfea0b3ccf4f8f0ffbbd1e55e78322c823865c0311bcd99bc0',
             5: 'd5abc536868a67923c34e260ea37c9a6c7e972390f03a5774fd49983537c4f9f'}  # v5 edited before any server DB (review M5 remote v1 M-1)


def test_migration_checksums_are_pinned():
    assert {version: store.migration_checksum(version) for version, _, _ in store.MIGRATIONS} == CHECKSUMS


def test_migration_v5_tables_columns_and_keys(db):
    assert store.MIGRATIONS[4][:2] == (5, 'remote_sync_v5')
    assert columns(db, 'collectors') == ['id', 'name', 'created_at', 'last_push_at', 'retired_at', 'note']
    assert columns(db, 'tokens') == ['id', 'kind', 'name', 'token_sha256', 'prefix', 'collector_id', 'created_at',
                                     'revoked_at', 'last_used_at']
    assert columns(db, 'source_roots')[-1] == 'collector_id'
    assert columns(db, 'pushes') == ['id', 'collector_id', 'scan_id', 'started_at', 'finished_at', 'status', 'batches',
                                     'bytes_in', 'counts_json', 'client_version', 'note']
    assert columns(db, 'push_batches') == ['batch_id', 'push_id', 'kind', 'applied_at', 'bytes_in', 'response_json']
    assert columns(db, 'push_parts') == ['push_id', 'local_version_id', 'part', 'parts', 'body_json', 'received_at']
    assert columns(db, 'collector_documents') == ['collector_id', 'local_document_id', 'document_id']
    assert columns(db, 'collector_versions') == ['collector_id', 'local_version_id', 'version_id', 'units_sha256']
    unique = {table: sorted([c[2] for c in db.execute(f'PRAGMA index_info({r[1]})')]
                            for r in db.execute(f'PRAGMA index_list({table})') if r[2])
              for table in ('collectors', 'tokens', 'collector_documents', 'collector_versions', 'push_batches')}
    assert unique == {'collectors': [['name']], 'tokens': [['token_sha256']],
                      'collector_documents': [['collector_id', 'local_document_id'], ['document_id']],
                      'collector_versions': [['collector_id', 'local_version_id'], ['version_id']],
                      'push_batches': [['batch_id']]}
    keys = {(r[3], r[2]) for r in db.execute('PRAGMA foreign_key_list(source_roots)')}
    assert keys == {('collector_id', 'collectors')}


def add_collector(db, name='합성-수집기'):
    return db.execute('INSERT INTO collectors(name, created_at) VALUES(?, ?)', (name, NOW)).lastrowid


def add_token(db, kind='mcp', sha='1' * 64, collector=None, name='합성-토큰'):
    return db.execute('INSERT INTO tokens(kind, name, token_sha256, prefix, collector_id, created_at) '
                      'VALUES(?, ?, ?, ?, ?, ?)', (kind, name, sha, sha[:6], collector, NOW)).lastrowid


def test_tokens_store_only_a_hash_and_push_tokens_name_their_collector(db):
    collector = add_collector(db)
    assert add_token(db, 'mcp') and add_token(db, 'push', '2' * 64, collector)
    for kind, owner in (('push', None), ('mcp', collector), ('other', None)):
        with pytest.raises(sqlite3.IntegrityError):
            add_token(db, kind, '3' * 64, owner)
    with pytest.raises(sqlite3.IntegrityError):
        add_token(db, 'push', '4' * 64, 999)  # foreign key


@pytest.mark.parametrize('assignment', ["kind='push'", "name='other'", "token_sha256='" + '9' * 64 + "'",
                                        "prefix='zzzzzz'", 'collector_id=1', "created_at='2000-01-01'", 'id=77'])
def test_token_identity_cannot_change(db, assignment):
    add_collector(db)
    token = add_token(db)
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute(f'UPDATE tokens SET {assignment} WHERE id=?', (token,))


def test_token_is_revoked_once_and_may_record_use(db):
    token = add_token(db)
    db.execute('UPDATE tokens SET last_used_at=? WHERE id=?', (NOW, token))
    db.execute('UPDATE tokens SET revoked_at=? WHERE id=?', (LATER, token))
    db.execute('UPDATE tokens SET last_used_at=? WHERE id=?', (LATER, token))
    for value in (None, NOW):
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            db.execute('UPDATE tokens SET revoked_at=? WHERE id=?', (value, token))
    assert db.execute('SELECT revoked_at, last_used_at FROM tokens').fetchone() == (LATER, LATER)


@pytest.mark.parametrize('sql', [
    'DELETE FROM tokens',
    "INSERT OR REPLACE INTO tokens(id, kind, name, token_sha256, prefix, created_at) "
    "VALUES(1, 'mcp', 'x', '" + '8' * 64 + "', '888888', '2026-10-26')",
    "INSERT OR REPLACE INTO tokens(kind, name, token_sha256, prefix, created_at) "
    "VALUES('mcp', 'x', '" + '1' * 64 + "', '111111', '2026-10-26')",
    "REPLACE INTO tokens(kind, name, token_sha256, prefix, created_at) "
    "VALUES('mcp', 'x', '" + '1' * 64 + "', '111111', '2026-10-26')",
])
def test_tokens_cannot_be_deleted_or_replaced(db, sql):
    add_token(db)
    before = db.execute('SELECT * FROM tokens').fetchall()
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute(sql)
    assert db.execute('SELECT * FROM tokens').fetchall() == before


def test_collector_maps_and_push_records(db):
    collector = add_collector(db)
    doc = add_document(db)
    version = add_version(db, doc, 1, H)
    db.execute('INSERT INTO collector_documents VALUES(?, 7, ?)', (collector, doc))
    db.execute('INSERT INTO collector_versions(collector_id, local_version_id, version_id) VALUES(?, 9, ?)',
               (collector, version))
    with pytest.raises(sqlite3.IntegrityError):  # one server document belongs to one collector id
        db.execute('INSERT INTO collector_documents VALUES(?, 8, ?)', (collector, doc))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute('INSERT INTO collector_versions(collector_id, local_version_id, version_id) VALUES(?, 9, ?)',
                   (collector, version))
    push = db.execute("INSERT INTO pushes(collector_id, started_at, status) VALUES(?, ?, 'running')",
                      (collector, NOW)).lastrowid
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO pushes(collector_id, started_at, status) VALUES(?, ?, 'other')", (collector, NOW))
    db.execute("INSERT INTO push_batches VALUES(?, ?, 'documents', ?, 10, '{}')", (H, push, NOW))
    with pytest.raises(sqlite3.IntegrityError):  # a batch id is applied once
        db.execute("INSERT INTO push_batches VALUES(?, ?, 'documents', ?, 10, '{}')", (H, push, NOW))
    db.execute("INSERT INTO push_parts VALUES(?, 9, 1, 2, '{}', ?)", (push, NOW))
    db.execute("UPDATE pushes SET status='done', finished_at=?, counts_json=? WHERE id=?",
               (LATER, json.dumps({'documents': 1, 'units': 3, 'vectors': 2}), push))
    db.execute('UPDATE source_roots SET collector_id=?', (collector,))
    assert store.collector_status(db) == [{'name': '합성-수집기', 'roots': 1, 'documents': 1, 'last_push_at': None}]
    assert store.last_push(db) == {'collector': '합성-수집기', 'at': LATER, 'documents': 1, 'units': 3, 'vectors': 2}


def test_status_reports_role_collectors_and_last_push(tmp_path):
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    store.open_home(home).close()
    local = tools.status(home)['result']
    assert (local['role'], local['collectors'], local['last_push']) == ('local', [], None)
    server = tools.status(home, role='server')['result']
    assert (server['role'], server['collectors'], server['last_push']) == ('server', [], None)
    with pytest.raises(ValueError):
        tools.status(home, role='other')


def test_migration_v5_upgrades_a_v4_store_in_place(tmp_path, monkeypatch):
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    monkeypatch.setattr(store, 'MIGRATIONS', store.MIGRATIONS[:4])
    old = store.open_home(home)
    root = old.execute(r"INSERT INTO source_roots(path, added_at) VALUES('C:\synthetic', ?)", (NOW,)).lastrowid
    assert 'tokens' not in names(old, 'table')
    old.close()
    monkeypatch.undo()
    upgraded = store.open_home(home)
    try:
        assert [r[0] for r in upgraded.execute('SELECT version FROM schema_migrations ORDER BY version')] == [1, 2, 3, 4, 5]
        assert upgraded.execute('SELECT id, collector_id FROM source_roots').fetchall() == [(root, None)]
        assert {'tokens', 'collectors', 'pushes'} <= names(upgraded, 'table') and store.migrate(upgraded) == []
    finally:
        upgraded.close()

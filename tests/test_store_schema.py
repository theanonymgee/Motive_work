"""Empty schema v1, migration ledger and append-only triggers (design §3.2), the M2 schema edits and the
chunk/FTS functions (design M2 §1.4, §4.3). Synthetic rows only."""
import sqlite3

import pytest

from workmcp import paths, store, tools

TABLES = {'schema_migrations', 'source_roots', 'documents', 'versions', 'units', 'chunks', 'chunk_vectors',
          'entities', 'assertions', 'evidence', 'links', 'deliveries', 'scans', 'meta', 'index_runs'}
TRIGGERS = {'assertions_closed_guard', 'assertions_update_guard', 'assertions_status_guard', 'assertions_no_delete',
            'assertions_no_replace', 'evidence_no_update', 'evidence_no_delete', 'evidence_no_replace',
            'versions_update_guard', 'versions_no_delete', 'versions_no_replace', 'versions_no_replace_seq'}
INDEXES = {'versions_sha256', 'versions_by_sha256', 'chunks_text'}
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


def test_empty_db_has_schema_v1(db):
    assert TABLES <= names(db, 'table')
    assert TRIGGERS <= names(db, 'trigger')
    assert INDEXES <= names(db, 'index')
    assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    rows = db.execute('SELECT version, name FROM schema_migrations').fetchall()
    assert rows == [(1, 'initial_schema_v1')]
    assert all(db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] == 0 for t in TABLES - {'schema_migrations'})
    if store.fts5_available():
        assert 'chunks_fts' in names(db, 'table')


def test_migrate_is_idempotent_and_detects_edited_migration(db, monkeypatch):
    assert store.migrate(db) == []
    monkeypatch.setattr(store, 'MIGRATIONS', [(1, 'initial_schema_v1', store.SCHEMA_V1 + '\n-- edited\n')])
    with pytest.raises(RuntimeError, match='checksum changed'):
        store.migrate(db)
    assert db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] == 1


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
        "VALUES('fact', ?, ?, ?, ?, 'my_note', 'test/record_fact')", (statement, NOW, status, supersedes))
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
                   "recorded_by) VALUES(50, 'fact', '합성', ?, 'active', 50, 'my_note', 't')", (NOW,))
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
        db.execute(f"{verb} assertions({columns}) VALUES(?, 'fact', '다시 쓴 내용', ?, NULL, 'active', NULL, "
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
    "SELECT ?, 'fact', '다시', recorded_from, 'active', 'my_note', 't' FROM assertions WHERE id=? "
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
                        "recorded_by) VALUES('fact', '합성 B', ?, 'active', 'my_note', 't')", (NOW,)).lastrowid
    third = db.execute("INSERT INTO assertions(id, kind, statement, recorded_from, status, source_kind, recorded_by) "
                       "VALUES(100, 'fact', '합성 C', ?, 'active', 'my_note', 't')", (NOW,)).lastrowid
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
    assert result['schema_version'] == 1
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

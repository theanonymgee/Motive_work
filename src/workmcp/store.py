"""SQLite store: connection, schema v1, migration ledger, append-only triggers (design §3).

Source records (documents/versions/units/chunks) and knowledge records
(entities/assertions/evidence/links) live in one WAL database. ``chunk_vectors`` and the
FTS5 index ``chunks_fts`` are derived and may be rebuilt. ``chunks_fts`` is created outside
the migration ledger only when this SQLite has FTS5 with the trigram tokenizer, so a
Python without FTS5 still opens the store (lexical search then falls back to LIKE).

Schema v1 was edited in place through M2 (design M1 §10.1); a development store made from an
earlier v1 is refused by the checksum check (delete it and scan again). From M3 on v1 is frozen
and every change is a new migration: v2 adds the tool call log, ``deliveries.call_id`` and
``document_events`` (design M3 §3.4); v3 adds the knowledge columns, ``assertion_events`` and the
entity/link guards (design M4 §2.3); v4 the unit lookup index; v5 the operating server's collectors, tokens
(SHA-256 only), push records and collector id maps (design M5 remote §2.2).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from .paths import db_path

SCHEMA_V1 = """
CREATE TABLE source_roots(id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1, added_at TEXT NOT NULL);

CREATE TABLE documents(id INTEGER PRIMARY KEY, root_id INTEGER NOT NULL REFERENCES source_roots(id),
  path TEXT NOT NULL,
  path_key TEXT NOT NULL UNIQUE,
  ext TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active','deleted','skipped')),
  skip_reason TEXT,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, deleted_at TEXT);

-- One row per observed content change, in observation order (seq 1, 2, ...). The same sha256
-- may come back (A -> B -> A) as a new row; M1 reuses units/chunks/vectors by sha256.
CREATE TABLE versions(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
  seq INTEGER NOT NULL CHECK(seq >= 1),
  sha256 TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  observed_at TEXT NOT NULL, superseded_at TEXT,
  parser TEXT, parser_version TEXT, encoding TEXT,
  parse_status TEXT NOT NULL CHECK(parse_status IN ('ok','failed','locked','unsupported','too_large','encrypted','pending')),
  parse_error TEXT, unit_count INTEGER, text_chars INTEGER,
  UNIQUE(document_id, seq));
CREATE INDEX versions_sha256 ON versions(document_id, sha256);
-- unit reuse across documents: same content, same parser version (design M1 §3.3)
CREATE INDEX versions_by_sha256 ON versions(sha256, parser_version);
-- What was observed (document, seq, sha256, size, observed_at) never changes; superseded_at is set
-- once (NULL -> value); mtime_ns and the parser columns may change (touched file, retry, --reparse).
-- A 'locked' row holds the placeholder sha256 '0'*64 and gets the real sha256/size when read; no
-- row becomes 'locked' later (that would reopen its sha256).
CREATE TRIGGER versions_update_guard BEFORE UPDATE ON versions
  WHEN NEW.id <> OLD.id OR NEW.document_id <> OLD.document_id OR NEW.seq <> OLD.seq
    OR NEW.observed_at <> OLD.observed_at
    OR ((NEW.sha256 <> OLD.sha256 OR NEW.size <> OLD.size) AND OLD.parse_status <> 'locked')
    OR (NEW.parse_status = 'locked' AND OLD.parse_status <> 'locked')
    OR (OLD.superseded_at IS NOT NULL AND NEW.superseded_at IS NOT OLD.superseded_at)
  BEGIN SELECT RAISE(ABORT, 'version identity is append-only'); END;
CREATE TRIGGER versions_no_delete BEFORE DELETE ON versions BEGIN SELECT RAISE(ABORT, 'append-only'); END;
-- REPLACE deletes the old row without DELETE triggers; refuse an INSERT that reuses either
-- uniqueness key (id, or (document_id, seq)) before any conflict handling.
CREATE TRIGGER versions_no_replace BEFORE INSERT ON versions
  WHEN EXISTS(SELECT 1 FROM versions WHERE id = NEW.id)
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER versions_no_replace_seq BEFORE INSERT ON versions
  WHEN EXISTS(SELECT 1 FROM versions WHERE document_id = NEW.document_id AND seq = NEW.seq)
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TABLE units(id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES versions(id),
  ordinal INTEGER NOT NULL, kind TEXT NOT NULL,
  locator_json TEXT NOT NULL,
  text TEXT NOT NULL, text_sha256 TEXT NOT NULL,
  UNIQUE(version_id, ordinal));

CREATE TABLE chunks(id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES versions(id),
  chunk_version TEXT NOT NULL,
  ordinal INTEGER NOT NULL, unit_first INTEGER NOT NULL, unit_last INTEGER NOT NULL,
  char_start INTEGER NOT NULL, char_end INTEGER NOT NULL,
  text TEXT NOT NULL, text_sha256 TEXT NOT NULL,
  UNIQUE(version_id, chunk_version, ordinal));
-- Rows are written and removed only by insert_chunks/drop_chunks below, which keep chunks_fts in step.
CREATE INDEX chunks_text ON chunks(text_sha256);

-- Derived: a vector is a function of the chunk text (design M2 §4.2), so it is keyed by content and
-- has no chunk foreign key; the same text in any document or version shares one row. float32
-- little-endian, unit norm. No triggers: rebuildable, ``index --gc`` drops rows no chunk uses.
CREATE TABLE chunk_vectors(model_key TEXT NOT NULL,
  text_sha256 TEXT NOT NULL, dims INTEGER NOT NULL, vector BLOB NOT NULL, embedded_at TEXT NOT NULL,
  PRIMARY KEY(model_key, text_sha256));

CREATE TABLE entities(id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('person','project','customer','org','term','other')),
  name TEXT NOT NULL, aliases_json TEXT NOT NULL DEFAULT '[]', note TEXT,
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  merged_into INTEGER REFERENCES entities(id),
  UNIQUE(kind, name));

CREATE TABLE assertions(id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('fact','decision','todo','term')),
  entity_id INTEGER REFERENCES entities(id),
  statement TEXT NOT NULL,
  data_json TEXT NOT NULL DEFAULT '{}',
  valid_from TEXT, valid_to TEXT,
  recorded_from TEXT NOT NULL,
  recorded_to TEXT,
  status TEXT NOT NULL CHECK(status IN ('active','withdrawn','superseded','conflict')),
  supersedes_id INTEGER REFERENCES assertions(id),
  change_reason TEXT,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('evidence','my_note')),
  recorded_by TEXT NOT NULL,
  CHECK((status IN ('active','conflict')) = (recorded_to IS NULL)),
  CHECK(supersedes_id IS NULL OR supersedes_id <> id));
-- Only status, recorded_to and (once) change_reason may change, and only while the row is open:
-- active <-> conflict, or active/conflict -> withdrawn/superseded (which sets recorded_to).
-- supersedes_id is set on INSERT of the new row only. A closed row never changes again.
CREATE TRIGGER assertions_closed_guard BEFORE UPDATE ON assertions
  WHEN OLD.recorded_to IS NOT NULL OR OLD.status IN ('withdrawn','superseded')
  BEGIN SELECT RAISE(ABORT, 'closed assertion is append-only'); END;
CREATE TRIGGER assertions_update_guard BEFORE UPDATE ON assertions
  WHEN NEW.statement <> OLD.statement OR NEW.data_json <> OLD.data_json OR NEW.kind <> OLD.kind
    OR NEW.entity_id IS NOT OLD.entity_id OR NEW.valid_from IS NOT OLD.valid_from
    OR NEW.valid_to IS NOT OLD.valid_to OR NEW.recorded_from <> OLD.recorded_from
    OR NEW.source_kind <> OLD.source_kind OR NEW.recorded_by <> OLD.recorded_by
    OR NEW.id <> OLD.id OR NEW.supersedes_id IS NOT OLD.supersedes_id
    OR (OLD.change_reason IS NOT NULL AND NEW.change_reason IS NOT OLD.change_reason)
  BEGIN SELECT RAISE(ABORT, 'assertion content is append-only'); END;
CREATE TRIGGER assertions_status_guard BEFORE UPDATE ON assertions
  WHEN NOT (NEW.status = OLD.status OR (OLD.status IN ('active','conflict')
                                         AND NEW.status IN ('active','conflict','withdrawn','superseded')))
    OR (NEW.recorded_to IS NOT NULL AND NEW.recorded_to < OLD.recorded_from)
  BEGIN SELECT RAISE(ABORT, 'assertion status change not allowed (append-only)'); END;
CREATE TRIGGER assertions_no_delete BEFORE DELETE ON assertions BEGIN SELECT RAISE(ABORT, 'append-only'); END;
-- REPLACE conflict resolution deletes the old row without firing DELETE triggers (recursive_triggers
-- is off by default), so an INSERT that reuses an existing id is refused before any conflict handling.
-- id is the only uniqueness constraint on this table; keep it that way or guard the new key too.
CREATE TRIGGER assertions_no_replace BEFORE INSERT ON assertions
  WHEN EXISTS(SELECT 1 FROM assertions WHERE id = NEW.id)
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TABLE evidence(id INTEGER PRIMARY KEY, assertion_id INTEGER NOT NULL REFERENCES assertions(id),
  version_id INTEGER NOT NULL REFERENCES versions(id),
  chunk_id INTEGER,  -- hint only, no foreign key: chunks may be dropped and rebuilt; locator_json is checked
  locator_json TEXT NOT NULL,
  quote TEXT NOT NULL, quote_sha256 TEXT NOT NULL);
CREATE TRIGGER evidence_no_update BEFORE UPDATE ON evidence BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER evidence_no_delete BEFORE DELETE ON evidence BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER evidence_no_replace BEFORE INSERT ON evidence
  WHEN EXISTS(SELECT 1 FROM evidence WHERE id = NEW.id)
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TABLE links(id INTEGER PRIMARY KEY, from_id INTEGER NOT NULL REFERENCES assertions(id),
  to_id INTEGER NOT NULL REFERENCES assertions(id),
  relation TEXT NOT NULL CHECK(relation IN ('supersedes','conflicts','supports','contradicts','relates')),
  note TEXT, recorded_at TEXT NOT NULL, recorded_by TEXT NOT NULL, resolved_at TEXT,
  resolved_by_id INTEGER REFERENCES assertions(id));

CREATE TABLE deliveries(id INTEGER PRIMARY KEY, at TEXT NOT NULL, client TEXT NOT NULL,
  tool TEXT NOT NULL, locator_json TEXT NOT NULL, chars INTEGER NOT NULL);

-- Scan history and summary (status.last_scan, interrupted-scan cleanup; design M1 §3.5).
CREATE TABLE scans(id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('running','done','aborted')),
  partial INTEGER NOT NULL DEFAULT 0, roots_json TEXT NOT NULL,
  counts_json TEXT NOT NULL DEFAULT '{}', note TEXT);

-- Derived index state (design M2 §4.3): 'chunk_version', 'fts_synced_at', 'fts_rows',
-- 'index_generation' (integer text, +1 per index run).
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

-- Index run history (status.last_index; a 'running' row left by a stopped run becomes 'aborted').
CREATE TABLE index_runs(id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('running','done','aborted')),
  model_key TEXT, chunk_version TEXT NOT NULL, counts_json TEXT NOT NULL DEFAULT '{}', note TEXT);
"""

# M3 (design M3 §3.4): tool call log, delivery rows tied to their call, document change events.
# Additive only: v1 above is not edited for M3, so two serve processes that start together apply this
# once (BEGIN IMMEDIATE) and a store made before M3 is upgraded in place. All three tables are
# append-only; tool_calls has a second unique key (call_id), guarded like versions (document_id, seq).
SCHEMA_V2 = """
CREATE TABLE tool_calls(id INTEGER PRIMARY KEY, at TEXT NOT NULL, client TEXT NOT NULL,
  client_version TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL, call_id TEXT NOT NULL UNIQUE,
  args_json TEXT NOT NULL DEFAULT '{}', pieces INTEGER NOT NULL DEFAULT 0, chars INTEGER NOT NULL DEFAULT 0,
  ok INTEGER NOT NULL, error_code TEXT, elapsed_ms REAL, pid INTEGER);
CREATE INDEX tool_calls_at ON tool_calls(at, id);
CREATE TRIGGER tool_calls_no_update BEFORE UPDATE ON tool_calls BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER tool_calls_no_delete BEFORE DELETE ON tool_calls BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER tool_calls_no_replace BEFORE INSERT ON tool_calls
  WHEN EXISTS(SELECT 1 FROM tool_calls WHERE id = NEW.id)
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER tool_calls_no_replace_call BEFORE INSERT ON tool_calls
  WHEN EXISTS(SELECT 1 FROM tool_calls WHERE call_id = NEW.call_id)
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;

ALTER TABLE deliveries ADD COLUMN call_id TEXT NOT NULL DEFAULT '';
CREATE INDEX deliveries_call ON deliveries(call_id);
CREATE TRIGGER deliveries_no_update BEFORE UPDATE ON deliveries BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER deliveries_no_delete BEFORE DELETE ON deliveries BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER deliveries_no_replace BEFORE INSERT ON deliveries
  WHEN EXISTS(SELECT 1 FROM deliveries WHERE id = NEW.id)
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TABLE document_events(id INTEGER PRIMARY KEY, at TEXT NOT NULL,
  scan_id INTEGER REFERENCES scans(id), document_id INTEGER NOT NULL REFERENCES documents(id),
  version_id INTEGER REFERENCES versions(id),
  kind TEXT NOT NULL CHECK(kind IN ('added','changed','restored','deleted','reparsed')));
CREATE INDEX document_events_at ON document_events(at, id);
CREATE INDEX document_events_doc ON document_events(document_id, id);
CREATE TRIGGER document_events_no_update BEFORE UPDATE ON document_events BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER document_events_no_delete BEFORE DELETE ON document_events BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER document_events_no_replace BEFORE INSERT ON document_events
  WHEN EXISTS(SELECT 1 FROM document_events WHERE id = NEW.id)
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""

# M4 (design M4 §2.3): structured knowledge on the v1 tables. Additive only: assertions get the
# attribute/relation of a fact (predicate, object_entity_id); assertion_events keeps every state change
# (recorded, retracted, superseded, conflict marked/cleared) with its reason, actor and time as a new
# row; entities and links get append-only guards (aliases_json, note and a link's resolution may be set);
# valid_from/valid_to are dates YYYY-MM-DD with valid_from < valid_to.
SCHEMA_V3 = """
ALTER TABLE assertions ADD COLUMN predicate TEXT;
ALTER TABLE assertions ADD COLUMN object_entity_id INTEGER REFERENCES entities(id);
CREATE INDEX assertions_subject ON assertions(entity_id, kind, predicate);
CREATE INDEX assertions_object ON assertions(object_entity_id);
CREATE INDEX assertions_supersedes ON assertions(supersedes_id);
-- The v1 update guard does not know the new columns: the same rule (content never changes) for them.
CREATE TRIGGER assertions_v3_update_guard BEFORE UPDATE ON assertions
  WHEN NEW.predicate IS NOT OLD.predicate OR NEW.object_entity_id IS NOT OLD.object_entity_id
  BEGIN SELECT RAISE(ABORT, 'assertion content is append-only'); END;
-- Dates and their order (M0 appendix B L-13); recorded_to >= recorded_from stays the v1 trigger's.
CREATE TRIGGER assertions_v3_dates BEFORE INSERT ON assertions
  WHEN (NEW.valid_from IS NOT NULL AND NEW.valid_from NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
    OR (NEW.valid_to IS NOT NULL AND NEW.valid_to NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
    OR (NEW.valid_from IS NOT NULL AND NEW.valid_to IS NOT NULL AND NEW.valid_to <= NEW.valid_from)
  BEGIN SELECT RAISE(ABORT, 'valid_from/valid_to must be YYYY-MM-DD and valid_from < valid_to'); END;
-- A fact has a subject and a predicate; a decision or a task has neither predicate nor object.
CREATE TRIGGER assertions_v3_shape BEFORE INSERT ON assertions
  WHEN (NEW.kind = 'fact' AND (NEW.entity_id IS NULL OR NEW.predicate IS NULL))
    OR (NEW.kind IN ('decision', 'todo') AND (NEW.predicate IS NOT NULL OR NEW.object_entity_id IS NOT NULL))
  BEGIN SELECT RAISE(ABORT, 'assertion shape: fact needs entity_id and predicate; decision/todo have neither'); END;

-- Entities: identity (kind, name, created_*) never changes; aliases_json and note may (new aliases);
-- merged_into is set once (M5).
CREATE TRIGGER entities_update_guard BEFORE UPDATE ON entities
  WHEN NEW.id <> OLD.id OR NEW.kind <> OLD.kind OR NEW.name <> OLD.name OR NEW.created_at <> OLD.created_at
    OR NEW.created_by <> OLD.created_by OR (OLD.merged_into IS NOT NULL AND NEW.merged_into IS NOT OLD.merged_into)
  BEGIN SELECT RAISE(ABORT, 'entity identity is append-only'); END;
CREATE TRIGGER entities_no_delete BEFORE DELETE ON entities BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER entities_no_replace BEFORE INSERT ON entities
  WHEN EXISTS(SELECT 1 FROM entities WHERE id = NEW.id) BEGIN SELECT RAISE(ABORT, 'append-only'); END;
-- The second unique key (kind, name) too.
CREATE TRIGGER entities_no_replace_name BEFORE INSERT ON entities
  WHEN EXISTS(SELECT 1 FROM entities WHERE kind = NEW.kind AND name = NEW.name) BEGIN SELECT RAISE(ABORT, 'append-only'); END;

-- Supersede and conflict links: content never changes; resolved_at/resolved_by_id go NULL -> value once.
CREATE INDEX links_from ON links(from_id, relation);
CREATE INDEX links_to ON links(to_id, relation);
CREATE TRIGGER links_update_guard BEFORE UPDATE ON links
  WHEN NEW.id <> OLD.id OR NEW.from_id <> OLD.from_id OR NEW.to_id <> OLD.to_id OR NEW.relation <> OLD.relation
    OR NEW.note IS NOT OLD.note OR NEW.recorded_at <> OLD.recorded_at OR NEW.recorded_by <> OLD.recorded_by
    OR (OLD.resolved_at IS NOT NULL AND (NEW.resolved_at IS NOT OLD.resolved_at OR NEW.resolved_by_id IS NOT OLD.resolved_by_id))
    OR (NEW.resolved_at IS NULL AND NEW.resolved_by_id IS NOT NULL)
  BEGIN SELECT RAISE(ABORT, 'link is append-only'); END;
CREATE TRIGGER links_no_delete BEFORE DELETE ON links BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER links_no_replace BEFORE INSERT ON links
  WHEN EXISTS(SELECT 1 FROM links WHERE id = NEW.id) BEGIN SELECT RAISE(ABORT, 'append-only'); END;

-- State change history: reason, actor, other row, time. Append-only.
CREATE TABLE assertion_events(id INTEGER PRIMARY KEY,
  assertion_id INTEGER NOT NULL REFERENCES assertions(id),
  at TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('recorded','retracted','superseded','conflict_marked','conflict_cleared')),
  reason TEXT,
  related_id INTEGER REFERENCES assertions(id),
  recorded_by TEXT NOT NULL,
  call_id TEXT);
CREATE INDEX assertion_events_assertion ON assertion_events(assertion_id, id);
CREATE TRIGGER assertion_events_no_update BEFORE UPDATE ON assertion_events BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER assertion_events_no_delete BEFORE DELETE ON assertion_events BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER assertion_events_no_replace BEFORE INSERT ON assertion_events
  WHEN EXISTS(SELECT 1 FROM assertion_events WHERE id = NEW.id) BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""

# Review M4 v2 N1: every source check (binding, the read tools' re-check, requote, the cut copy,
# get_snippet by locator, binding.validate_locator) looks a unit up by (version_id, locator_json); without
# this index each lookup scans all units of the version. v3 may already be applied to a real home, so the
# index is its own migration (additive only).
SCHEMA_V4 = """
CREATE INDEX units_locator ON units(version_id, locator_json);
"""

# M5 (design M5 remote §2.2): the operating server mirrors what collectors push. Additive only; a collector
# home gets the same tables and leaves them empty. Tokens keep only their SHA-256 (the plain token is shown
# once by ``token issue``); a token row never changes except ``last_used_at`` and one ``revoked_at``, and its
# two unique keys (id, token_sha256) are guarded against REPLACE like versions. ``source_roots.collector_id``
# names the collector that owns a root on the server (NULL on a collector or a local home). ``pushes`` is one
# row per push run (with its server ``scans`` row), ``push_batches`` the idempotency record per batch id,
# ``push_parts`` the progress of a split version being applied part by part (row ``part`` 0: its state; other
# rows: the text pieces of a unit that continues into the next part); ``collector_documents``,
# ``collector_versions`` and ``collector_events`` map a collector's own rowids to the server's (they are
# re-pointed when a collector store is rebuilt; the server rows themselves stay).
SCHEMA_V5 = """
CREATE TABLE collectors(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL, last_push_at TEXT, retired_at TEXT, note TEXT);

CREATE TABLE tokens(id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('mcp','push')),
  name TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL,
  collector_id INTEGER REFERENCES collectors(id),
  created_at TEXT NOT NULL, revoked_at TEXT, last_used_at TEXT,
  CHECK((kind = 'push') = (collector_id IS NOT NULL)));
CREATE TRIGGER tokens_update_guard BEFORE UPDATE ON tokens
  WHEN NEW.id <> OLD.id OR NEW.kind <> OLD.kind OR NEW.name <> OLD.name OR NEW.token_sha256 <> OLD.token_sha256
    OR NEW.prefix <> OLD.prefix OR NEW.collector_id IS NOT OLD.collector_id OR NEW.created_at <> OLD.created_at
    OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at)
  BEGIN SELECT RAISE(ABORT, 'token is append-only (revoke once, last_used_at only)'); END;
CREATE TRIGGER tokens_no_delete BEFORE DELETE ON tokens BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER tokens_no_replace BEFORE INSERT ON tokens
  WHEN EXISTS(SELECT 1 FROM tokens WHERE id = NEW.id)
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER tokens_no_replace_sha BEFORE INSERT ON tokens
  WHEN EXISTS(SELECT 1 FROM tokens WHERE token_sha256 = NEW.token_sha256)
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;

ALTER TABLE source_roots ADD COLUMN collector_id INTEGER REFERENCES collectors(id);

CREATE TABLE pushes(id INTEGER PRIMARY KEY, collector_id INTEGER NOT NULL REFERENCES collectors(id),
  scan_id INTEGER REFERENCES scans(id), started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('running','done','aborted')),
  batches INTEGER NOT NULL DEFAULT 0, bytes_in INTEGER NOT NULL DEFAULT 0,
  counts_json TEXT NOT NULL DEFAULT '{}', client_version TEXT, note TEXT);
CREATE TABLE push_batches(batch_id TEXT PRIMARY KEY, push_id INTEGER NOT NULL REFERENCES pushes(id),
  kind TEXT NOT NULL, applied_at TEXT NOT NULL, bytes_in INTEGER NOT NULL, response_json TEXT NOT NULL);
CREATE TABLE push_parts(push_id INTEGER NOT NULL REFERENCES pushes(id), local_version_id INTEGER NOT NULL,
  part INTEGER NOT NULL, parts INTEGER NOT NULL, body_json TEXT NOT NULL, received_at TEXT NOT NULL,
  PRIMARY KEY(push_id, local_version_id, part));

CREATE TABLE collector_documents(collector_id INTEGER NOT NULL REFERENCES collectors(id),
  local_document_id INTEGER NOT NULL, document_id INTEGER NOT NULL UNIQUE REFERENCES documents(id),
  PRIMARY KEY(collector_id, local_document_id));
CREATE TABLE collector_versions(collector_id INTEGER NOT NULL REFERENCES collectors(id),
  local_version_id INTEGER NOT NULL, version_id INTEGER NOT NULL UNIQUE REFERENCES versions(id),
  units_sha256 TEXT,
  PRIMARY KEY(collector_id, local_version_id));
CREATE TABLE collector_events(collector_id INTEGER NOT NULL REFERENCES collectors(id),
  local_event_id INTEGER NOT NULL, event_id INTEGER NOT NULL REFERENCES document_events(id),
  PRIMARY KEY(collector_id, local_event_id));
"""

# (version, name, sql). Never edit an applied entry; append a new one.
MIGRATIONS = [(1, 'initial_schema_v1', SCHEMA_V1), (2, 'tool_calls_events_v2', SCHEMA_V2),
              (3, 'knowledge_v3', SCHEMA_V3), (4, 'units_locator_v4', SCHEMA_V4), (5, 'remote_sync_v5', SCHEMA_V5)]
SCHEMA_VERSION = MIGRATIONS[-1][0]
EVENT_KINDS = ('added', 'changed', 'restored', 'deleted', 'reparsed')
# Knowledge (design M4 §2): the kinds M4 writes ('term' stays in the v1 CHECK, unused), task states,
# assertion statuses and assertion event kinds.
ENTITY_KINDS = ('person', 'project', 'customer', 'org', 'term', 'other')
ASSERTION_KINDS = ('fact', 'decision', 'todo')
ASSERTION_STATUSES = ('active', 'conflict', 'withdrawn', 'superseded')
TASK_STATES = ('open', 'done', 'cancelled')
EVENT_KINDS_ASSERTION = ('recorded', 'retracted', 'superseded', 'conflict_marked', 'conflict_cleared')
DOCUMENT_STATUSES = ('active', 'deleted', 'skipped')
CALL_COLUMNS = ('at', 'client', 'client_version', 'tool', 'call_id', 'args_json', 'pieces', 'chars', 'ok',
                'error_code', 'elapsed_ms', 'pid')
DELIVERY_COLUMNS = ('at', 'client', 'tool', 'locator_json', 'chars')

LOCK_NAME = 'scan.lock'
INDEX_LOCK_NAME = 'index.lock'
STALE_LOCK_AGE = timedelta(hours=24)
DEV_SCHEMA_HINT = ('development schema change: delete {db} and scan again (only before the first real install, '
                   'design M1 §10.1)')

FTS_SQL = ("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
           "text, content='chunks', content_rowid='id', tokenize='trigram')")


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def statements(script: str) -> list[str]:
    """Split a script into complete statements (trigger bodies keep their inner ``;``)."""
    result, buffer = [], ''
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                result.append(buffer.strip())
            buffer = ''
    if buffer.strip():
        raise ValueError('incomplete SQL statement at end of script')
    return result


def fts5_available() -> bool:
    """FTS5 with the trigram tokenizer (SQLite >= 3.34) works in this Python."""
    try:
        db = sqlite3.connect(':memory:')
        try:
            db.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
        finally:
            db.close()
        return True
    except sqlite3.Error:
        return False


def _up_to_date(db: sqlite3.Connection) -> bool:
    """Every migration is recorded with its checksum, judged by reading only (no write lock); raises
    on a changed checksum as ``migrate`` does."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone() is None:
        return False
    recorded = dict(db.execute('SELECT version, checksum FROM schema_migrations'))
    for version, _, sql in MIGRATIONS:
        if version not in recorded:
            return False
        if recorded[version] != hashlib.sha256(sql.encode('utf-8')).hexdigest():
            raise RuntimeError(f'migration {version} checksum changed; add a new migration instead')
    return True


def migrate(db: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in one immediate transaction; return the versions applied now.

    A store that is already up to date is recognised by reading alone, so ``serve`` (and every
    command) starts while another process holds the write lock, e.g. a running ``scan`` (review M3
    v2 N1). Only a store that needs a migration takes ``BEGIN IMMEDIATE`` (two servers starting a new
    store together still apply it once)."""
    if _up_to_date(db):
        return []
    applied = []
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, '
                   'checksum TEXT NOT NULL, applied_at TEXT NOT NULL)')
        for version, name, sql in MIGRATIONS:
            checksum = hashlib.sha256(sql.encode('utf-8')).hexdigest()
            row = db.execute('SELECT checksum FROM schema_migrations WHERE version=?', (version,)).fetchone()
            if row:
                if row[0] != checksum:
                    raise RuntimeError(f'migration {version} checksum changed; add a new migration instead')
                continue
            for statement in statements(sql):
                db.execute(statement)
            db.execute('INSERT INTO schema_migrations VALUES(?,?,?,?)', (version, name, checksum, now()))
            applied.append(version)
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    return applied


WAL_ATTEMPTS = 50               # x WAL_RETRY_SECONDS: two serve processes opening a new store together
WAL_RETRY_SECONDS = 0.1


def _wal(db: sqlite3.Connection) -> None:
    """``journal_mode=WAL``. Switching a new database into WAL needs an exclusive lock and SQLite answers
    "database is locked" at once, without the busy handler, when another connection does the same (two
    clients starting their servers together, design M3 §7); so retry for up to 5 seconds."""
    for attempt in range(WAL_ATTEMPTS):
        try:
            db.execute('PRAGMA journal_mode=WAL')
            return
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc) or attempt == WAL_ATTEMPTS - 1:
                raise
            time.sleep(WAL_RETRY_SECONDS)


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the store at ``path``: WAL, foreign keys, busy timeout, schema."""
    db = sqlite3.connect(path, isolation_level=None)
    db.execute('PRAGMA busy_timeout=5000')
    _wal(db)
    db.execute('PRAGMA foreign_keys=ON')
    migrate(db)
    if fts5_available():
        db.execute(FTS_SQL)
    return db


def open_home(home: Path) -> sqlite3.Connection:
    """Store of a validated, ensured data home, migrated (``serve`` calls this once at start)."""
    return connect(db_path(home))


def open_read(home: Path) -> sqlite3.Connection:
    """Read-only connection to an existing store: no migration, no schema writes (tool calls)."""
    path = db_path(home)
    if not path.is_file():
        raise FileNotFoundError(f'store not created yet: {path}')
    db = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True, isolation_level=None)
    db.execute('PRAGMA busy_timeout=5000')
    return db


def migration_checksum(version: int) -> str:
    sql = next(sql for number, _, sql in MIGRATIONS if number == version)
    return hashlib.sha256(sql.encode('utf-8')).hexdigest()


def schema_changed(error: Exception) -> bool:
    """The migration refused a store made from an earlier edit of schema v1."""
    return isinstance(error, RuntimeError) and 'checksum changed' in str(error)


class Locked(Exception):
    """Another process holds a lock file in the data home (exit code 3)."""


class ScanLocked(Locked):
    """Another scan holds ``scan.lock`` (exit code 3)."""


def lock_path(home: Path, name: str) -> Path:
    return Path(home)/name


def scan_lock_path(home: Path) -> Path:
    return lock_path(home, LOCK_NAME)


def _lock_started(path: Path) -> datetime | None:
    """Start time written in the lock, or the file time when the content is unreadable."""
    try:
        started = datetime.fromisoformat(json.loads(path.read_text(encoding='utf-8'))['started_at'])
        return started if started.tzinfo else started.astimezone()
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        return datetime.fromtimestamp(os.stat(path).st_mtime).astimezone()
    except OSError:
        return None


def acquire_lock(home: Path, name: str, at: datetime | None = None, *,
                 stale_after: timedelta = STALE_LOCK_AGE) -> tuple[Path, str | None]:
    """Create ``<home>/<name>`` exclusively; ``(path, warning)``. A lock older than ``stale_after`` (24 hours
    unless the caller says otherwise; ``push`` uses ``push.PUSH_LOCK_STALE``) is removed with a warning; a newer one raises ``Locked`` (``ScanLocked`` for ``scan.lock``). The
    process id is recorded but never probed (on Windows ``os.kill(pid, 0)`` terminates the
    process). ``scan`` holds ``scan.lock`` and ``index`` holds ``index.lock`` (design M2 §4.6)."""
    path = lock_path(home, name)
    what = name.removesuffix('.lock')
    error = ScanLocked if name == LOCK_NAME else Locked
    at = at or datetime.now().astimezone()
    warning = None
    for attempt in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            started = _lock_started(path)
            if attempt == 1 and started is not None and at - started > stale_after:
                info = path.read_text(encoding='utf-8', errors='replace')[:200]
                os.remove(path)
                hours = stale_after.total_seconds() / 3600
                warning = f'warning: removed a stale {what} lock older than {hours:g} hours ({info.strip()})'
                continue
            try:
                info = json.loads(path.read_text(encoding='utf-8'))
                holder = f"pid {info.get('pid')}, since {info.get('started_at')}"
            except (OSError, ValueError, AttributeError):
                holder = 'unknown holder'
            raise error(f'another {what} is running ({holder}); if not, delete {path}') from None
        try:
            os.write(fd, json.dumps({'pid': os.getpid(), 'started_at': at.isoformat(timespec='seconds')}).encode())
        finally:
            os.close(fd)
        return path, warning
    raise error(f'cannot take the {what} lock {path}')


def release_lock(path: Path) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def acquire_scan_lock(home: Path, at: datetime | None = None) -> tuple[Path, str | None]:
    return acquire_lock(home, LOCK_NAME, at)


release_scan_lock = release_lock


def last_scan(db: sqlite3.Connection) -> dict | None:
    """Last finished scan for ``status``: ``{at, added, changed, deleted, failed}`` or None."""
    row = db.execute("SELECT finished_at, counts_json FROM scans WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    counts = json.loads(row[1] or '{}').get('total', {})
    return {'at': row[0], **{key: counts.get(key, 0) for key in ('added', 'changed', 'deleted', 'failed')}}


def last_index(db: sqlite3.Connection) -> dict | None:
    """Last finished index run for ``status``: ``{at, chunks, embedded, reused, model, model_key}`` or None."""
    row = db.execute("SELECT finished_at, model_key, counts_json FROM index_runs WHERE status='done' "
                     'ORDER BY id DESC LIMIT 1').fetchone()
    if row is None:
        return None
    counts = json.loads(row[2] or '{}')
    return {'at': row[0], 'chunks': counts.get('chunks_total', 0), 'embedded': counts.get('embedded', 0),
            'reused': counts.get('reused_vectors', 0), 'model': counts.get('model'), 'model_key': row[1]}


COLLECTOR_STATUS_KEYS = ('name', 'roots', 'documents', 'last_push_at')     # design M5 remote §4.7
LAST_PUSH_KEYS = ('collector', 'at', 'documents', 'units', 'vectors')


def collector_status(db: sqlite3.Connection) -> list[dict]:
    """``status.collectors`` on the operating server (v5 tables): one row per collector, counts only."""
    rows = db.execute(
        'SELECT c.name, (SELECT COUNT(*) FROM source_roots r WHERE r.collector_id = c.id), '
        '(SELECT COUNT(*) FROM collector_documents m WHERE m.collector_id = c.id), c.last_push_at '
        'FROM collectors c ORDER BY c.id').fetchall()
    return [dict(zip(COLLECTOR_STATUS_KEYS, row)) for row in rows]


def last_push(db: sqlite3.Connection) -> dict | None:
    """``status.last_push``: the last finished push ``{collector, at, documents, units, vectors}`` or None
    (``pushes.counts_json`` holds the sent counts under those names)."""
    row = db.execute("SELECT c.name, p.finished_at, p.counts_json FROM pushes p JOIN collectors c "
                     "ON c.id = p.collector_id WHERE p.status = 'done' ORDER BY p.id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    counts = json.loads(row[2] or '{}')
    return {'collector': row[0], 'at': row[1], **{key: counts.get(key, 0) for key in ('documents', 'units', 'vectors')}}


# -- chunks and the FTS index (design M2 §4.3, L-8) ------------------------------------------
# chunks_fts is an external-content FTS5 table: it does not follow chunks by itself, and a delete
# must hand it the old text. There are no triggers; the only SQL that writes or removes chunk rows
# lives in insert_chunks and drop_chunks (and the 'rebuild' command in rebuild_fts), checked by
# tests/test_index.py::test_chunk_sql_lives_only_in_store_insert_and_drop. Callers own the
# transaction (one version per transaction in ``index``, the fill transaction in ``collect``).

CHUNK_INSERT_BATCH = 1000

# Latest active versions only (design M2 §4.1): what ``index`` chunks and ``search`` returns.
ELIGIBLE_VERSIONS = ("d.status = 'active' AND v.superseded_at IS NULL AND v.parse_status = 'ok' "
                     "AND v.unit_count > 0")


def eligible_versions_sql() -> str:
    """``FROM`` body for eligible versions, aliases ``v`` (versions) and ``d`` (documents), ending in
    a ``WHERE`` clause: ``f'SELECT v.id FROM {store.eligible_versions_sql()} AND ...'``."""
    return f'versions v JOIN documents d ON d.id = v.document_id WHERE {ELIGIBLE_VERSIONS}'


def latest_chunks_sql() -> str:
    """``FROM`` body for the chunks of eligible versions, aliases ``c`` (chunks), ``v``, ``d``, ending
    in a ``WHERE`` clause: ``f'SELECT c.id FROM {store.latest_chunks_sql()} AND c.chunk_version = ?'``.
    It does not filter ``chunk_version``; the caller adds that condition."""
    return (f'chunks c JOIN versions v ON v.id = c.version_id JOIN documents d ON d.id = v.document_id '
            f'WHERE {ELIGIBLE_VERSIONS}')


def fts_available(db: sqlite3.Connection) -> bool:
    """This store has ``chunks_fts`` and this Python can use it (FTS5 with trigram)."""
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'").fetchone()
    return exists is not None and fts5_available()


CHUNK_ID_HIGH = 'chunk_id_high'  # meta: the highest chunk id ever given (review M3-2)


def take_chunk_ids(db: sqlite3.Connection, count: int) -> int:
    """The first of ``count`` new chunk ids, never used before in this store: ``chunks.id`` is a plain
    rowid (SQLite would give a deleted highest id again), so ids come from a counter in ``meta`` that
    only grows (``max(counter, MAX(id))`` for a store made before it existed). A ``get_snippet`` call
    with a ``chunk_id`` from an older search therefore finds that chunk or nothing, never another text
    (review M3-2). ``chunks_fts`` rows use the same id (external content, ``rowid = chunks.id``)."""
    high = max(int(meta_get(db, CHUNK_ID_HIGH, '0')), db.execute('SELECT COALESCE(MAX(id), 0) FROM chunks').fetchone()[0])
    meta_set(db, CHUNK_ID_HIGH, str(high + count))
    return high + 1


def insert_chunks(db: sqlite3.Connection, version_id: int, chunk_version: str, specs=(), *,
                  copy_from: int | None = None, append: bool = False) -> int:
    """Store the chunks of one version under ``chunk_version`` and add them to ``chunks_fts``.

    ``specs``: objects with ``ordinal, unit_first, unit_last, char_start, char_end, text``
    (``chunks.ChunkSpec``); ``text_sha256`` is computed here. Or ``copy_from``: a version id whose
    ``chunk_version`` rows are copied (same sha256 and parser version, design M2 §4.1 step 2), in
    which case ``specs`` must be empty. The version must have no rows under ``chunk_version`` yet.
    Returns the number of rows stored. No transaction control: the caller commits.
    ``append``: the version may already hold rows of ``chunk_version`` (the operating server builds a split
    version's chunks part by part; the specs continue its ordinals). A copy is done in SQL, so a large version
    is never held in memory.
    """
    if not append and db.execute('SELECT 1 FROM chunks WHERE version_id=? AND chunk_version=? LIMIT 1',
                                 (version_id, chunk_version)).fetchone():
        raise ValueError(f'version {version_id} already has chunks of {chunk_version}')
    first_new = None
    if copy_from is not None:
        if specs:
            raise ValueError('pass specs or copy_from, not both')
        count = db.execute('SELECT COUNT(*) FROM chunks WHERE version_id=? AND chunk_version=?',
                           (copy_from, chunk_version)).fetchone()[0]
        stored = 0
        if count:
            first_new = take_chunk_ids(db, count)
            stored = db.execute(
                'INSERT INTO chunks(id, version_id, chunk_version, ordinal, unit_first, unit_last, char_start, '
                'char_end, text, text_sha256) SELECT ? + ROW_NUMBER() OVER (ORDER BY ordinal) - 1, ?, chunk_version, '
                'ordinal, unit_first, unit_last, char_start, char_end, text, text_sha256 FROM chunks '
                'WHERE version_id=? AND chunk_version=? ORDER BY ordinal',
                (first_new, version_id, copy_from, chunk_version)).rowcount
    else:
        rows = [(spec.ordinal, spec.unit_first, spec.unit_last, spec.char_start, spec.char_end, spec.text,
                 hashlib.sha256(spec.text.encode('utf-8')).hexdigest()) for spec in specs]
        sql = ('INSERT INTO chunks(id, version_id, chunk_version, ordinal, unit_first, unit_last, char_start, '
               'char_end, text, text_sha256) VALUES(?,?,?,?,?,?,?,?,?,?)')
        stored = 0
        for at in range(0, len(rows), CHUNK_INSERT_BATCH):
            part = rows[at:at + CHUNK_INSERT_BATCH]
            first = take_chunk_ids(db, len(part))
            first_new = first if first_new is None else first_new
            db.executemany(sql, [(first + n, version_id, chunk_version, *row) for n, row in enumerate(part)])
            stored += len(part)
    if stored:
        if fts_in_step(db):
            db.execute('INSERT INTO chunks_fts(rowid, text) SELECT id, text FROM chunks '
                       'WHERE version_id=? AND chunk_version=? AND id >= ?', (version_id, chunk_version, first_new))
        else:
            fts_mark_unsynced(db)
        bump_data_generation(db)
    return stored


def drop_chunks(db: sqlite3.Connection, version_id: int, keep: str | None = None) -> int:
    """Remove the chunks of one version (all, or every ``chunk_version`` other than ``keep``) and
    their ``chunks_fts`` entries. Vectors are content-addressed and stay (``index --gc`` removes
    unused ones). Returns the number of rows removed. No transaction control: the caller commits."""
    where, params = 'version_id=?', [version_id]
    if keep is not None:
        where, params = 'version_id=? AND chunk_version<>?', [version_id, keep]
    if fts_in_step(db):
        # Only rows the index holds (an FTS5 'delete' of a row it does not hold corrupts the index).
        db.execute("INSERT INTO chunks_fts(chunks_fts, rowid, text) SELECT 'delete', id, text FROM chunks "
                   f'WHERE {where} AND id IN (SELECT id FROM chunks_fts_docsize)', params)
        removed = db.execute(f'DELETE FROM chunks WHERE {where}', params).rowcount
    else:
        removed = db.execute(f'DELETE FROM chunks WHERE {where}', params).rowcount
        if removed:
            fts_mark_unsynced(db)
    if removed:
        bump_data_generation(db)
    return removed


def fts_in_step(db: sqlite3.Connection) -> bool:
    """``chunks_fts`` can be kept in step here: FTS5 works and no earlier write skipped the index. Once
    a write skipped it (``fts_dirty``), later writes leave it alone too (a stale entry may share a
    reused chunk id) until ``rebuild_fts``."""
    return fts_available(db) and meta_get(db, 'fts_dirty') is None


def fts_mark_unsynced(db: sqlite3.Connection) -> None:
    """Chunks changed without ``chunks_fts`` (a Python without FTS5, or an index already out of step):
    record ``fts_dirty`` and forget the last sync, so the next ``index`` with FTS5 rebuilds the index
    and ``status``/``doctor`` report it (review M2-2)."""
    meta_set(db, 'fts_dirty', '1')
    db.execute("DELETE FROM meta WHERE key IN ('fts_rows', 'fts_synced_at')")


def bump_data_generation(db: sqlite3.Connection) -> None:
    """``meta.data_generation`` + 1. Every commit that can change which chunks are eligible (chunk rows
    written or removed here; documents and versions changed by ``collect.scan``) bumps it, so a
    long-lived ``search.Engine`` re-maps its vector cache before the next query (review M2-1)."""
    meta_set(db, 'data_generation', str(int(meta_get(db, 'data_generation', '0') or 0) + 1))


def rebuild_fts(db: sqlite3.Connection) -> int:
    """Rebuild ``chunks_fts`` from ``chunks`` (``index --rebuild-fts``) and record it in ``meta``;
    returns the row count, or -1 when this store or Python has no FTS5. No transaction control."""
    if not fts_available(db):
        return -1
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    db.execute("DELETE FROM meta WHERE key = 'fts_dirty'")
    rows = fts_rows(db)
    meta_set(db, 'fts_rows', str(rows))
    meta_set(db, 'fts_synced_at', now())
    return rows


def fts_rows(db: sqlite3.Connection) -> int:
    """Rows the FTS index really holds. ``COUNT(*) FROM chunks_fts`` would count the external content
    table (``chunks``) and never show a gap; the ``chunks_fts_docsize`` shadow table has one row per
    indexed chunk."""
    return db.execute('SELECT COUNT(*) FROM chunks_fts_docsize').fetchone()[0]


def meta_get(db: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def meta_set(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute('INSERT INTO meta(key, value, updated_at) VALUES(?,?,?) '
               'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
               (key, str(value), now()))


def fts_differences(db: sqlite3.Connection) -> tuple[int, int]:
    """``(missing, extra)``: chunk ids the FTS index lacks, and ids it holds that are no chunk. Equal row
    counts can hide both (review M2-2), so consistency compares the id sets."""
    missing = db.execute('SELECT COUNT(*) FROM chunks WHERE id NOT IN (SELECT id FROM chunks_fts_docsize)').fetchone()[0]
    extra = db.execute('SELECT COUNT(*) FROM chunks_fts_docsize WHERE id NOT IN (SELECT id FROM chunks)').fetchone()[0]
    return missing, extra


def fts_status(db: sqlite3.Connection) -> dict:
    """``{available, rows, chunks, missing, extra, synced_at, consistent}`` for ``status`` and ``doctor``.
    ``rows``/``missing``/``extra`` are None without FTS5. ``consistent``: FTS5 is there, the id sets
    match, and the last chunk change was made with FTS5 (``synced_at`` set, unless there are no chunks)."""
    chunks = db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
    available = fts_available(db)
    rows = fts_rows(db) if available else None
    missing, extra = fts_differences(db) if available else (None, None)
    synced_at = meta_get(db, 'fts_synced_at')
    dirty = meta_get(db, 'fts_dirty') is not None
    return {'available': available, 'rows': rows, 'chunks': chunks, 'missing': missing, 'extra': extra,
            'synced_at': synced_at, 'dirty': dirty,
            'consistent': available and missing == 0 and extra == 0 and not dirty}


# -- M3: tool call log, delivery rows, document events (design M3 §2.4, §2.5, §3) -----------------
# No transaction control here either: ``server`` wraps one call's rows in BEGIN IMMEDIATE ... COMMIT,
# ``collect`` adds events inside its own transaction. Nothing here stores query or document text.

def _json(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                                                           sort_keys=True)


def record_call(db: sqlite3.Connection, row) -> int:
    """Insert one ``tool_calls`` row (keys ``CALL_COLUMNS``; ``at, client, tool, call_id, ok`` required,
    ``args_json`` a summary without query or body text, str or JSON-able); returns its id."""
    values = {'client_version': '', 'args_json': '{}', 'pieces': 0, 'chars': 0, 'error_code': None,
              'elapsed_ms': None, 'pid': None, **row}
    if unknown := set(values) - set(CALL_COLUMNS):
        raise ValueError(f'unknown tool_calls columns {sorted(unknown)}')
    values['args_json'] = _json(values['args_json'])
    values['ok'] = int(bool(values['ok']))
    return db.execute(f'INSERT INTO tool_calls({",".join(CALL_COLUMNS)}) VALUES({",".join("?" * len(CALL_COLUMNS))})',
                      [values[k] for k in CALL_COLUMNS]).lastrowid


def record_deliveries(db: sqlite3.Connection, call_id: str, rows) -> int:
    """Insert one ``deliveries`` row per delivered piece (keys ``DELIVERY_COLUMNS``; ``locator_json`` a
    locator dict or its JSON) under ``call_id``; returns the number of rows."""
    batch = []
    for row in rows:
        if unknown := set(row) - set(DELIVERY_COLUMNS):
            raise ValueError(f'unknown deliveries columns {sorted(unknown)}')
        batch.append((row['at'], row['client'], row['tool'], _json(row['locator_json']), int(row['chars']), call_id))
    if batch:
        db.executemany('INSERT INTO deliveries(at, client, tool, locator_json, chars, call_id) VALUES(?,?,?,?,?,?)',
                       batch)
    return len(batch)


def call_counts(db: sqlite3.Connection) -> dict:
    """``{'total', 'by_client'}`` over ``tool_calls`` (client names as the clients sent them)."""
    by_client = dict(db.execute('SELECT client, COUNT(*) FROM tool_calls GROUP BY client ORDER BY client'))
    return {'total': sum(by_client.values()), 'by_client': by_client}


def add_document_event(db: sqlite3.Connection, scan_id: int | None, document_id: int, version_id: int | None,
                       kind: str, at: str | None = None) -> int:
    """Record one document change (``EVENT_KINDS``) found by a scan; returns the event id."""
    if kind not in EVENT_KINDS:
        raise ValueError(f'unknown document event kind {kind!r}')
    return db.execute('INSERT INTO document_events(at, scan_id, document_id, version_id, kind) VALUES(?,?,?,?,?)',
                      (at or now(), scan_id, document_id, version_id, kind)).lastrowid


def _document_filter_sql(filters) -> tuple[str, list]:
    """``AND ...`` on alias ``d`` for already checked ``path_prefix`` (normcased) and ``ext`` filters
    (``search.check_filters``), as ``search._filter_sql`` does."""
    sql, params = '', []
    if filters.get('path_prefix'):
        escaped = filters['path_prefix'].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        sql += " AND d.path_key LIKE ? ESCAPE '\\'"
        params.append(escaped + '%')
    if filters.get('ext'):
        sql += f" AND d.ext IN ({','.join('?' * len(filters['ext']))})"
        params += list(filters['ext'])
    return sql, params


def documents_query(db: sqlite3.Connection, filters, limit: int, offset: int, *,
                    chunk_version: str) -> tuple[list[dict], int]:
    """``(rows, total)`` for ``list_documents`` (design M3 §2.4), ordered by ``path_key``.

    ``filters``: ``path_prefix``/``ext`` (checked), ``status`` (``DOCUMENT_STATUSES`` or ``'all'``),
    ``changed_since`` (checked ISO: latest version observed or document deleted at or after it).
    ``latest`` is the highest-seq version or None; ``indexed`` means it has chunks of ``chunk_version``."""
    where, params = _document_filter_sql(filters)
    status = filters.get('status') or 'active'
    if status != 'all':
        where += ' AND d.status = ?'
        params.append(status)
    if filters.get('changed_since'):
        where += ' AND (julianday(v.observed_at) >= julianday(?) OR julianday(d.deleted_at) >= julianday(?))'
        params += [filters['changed_since']] * 2
    body = ('FROM documents d LEFT JOIN versions v ON v.id = '
            '(SELECT id FROM versions WHERE document_id = d.id ORDER BY seq DESC LIMIT 1) '
            f'WHERE 1 = 1{where}')
    total = db.execute(f'SELECT COUNT(*) {body}', params).fetchone()[0]
    latest_keys = ('version_id', 'seq', 'sha256', 'size', 'observed_at', 'parse_status', 'parse_error',
                   'unit_count', 'text_chars')
    rows = []
    for row in db.execute(
            'SELECT d.id, d.path, d.ext, d.status, d.skip_reason, d.root_id, d.first_seen_at, d.last_seen_at, '
            'd.deleted_at, v.id, v.seq, v.sha256, v.size, v.observed_at, v.parse_status, v.parse_error, '
            'v.unit_count, v.text_chars, '
            'EXISTS(SELECT 1 FROM chunks c WHERE c.version_id = v.id AND c.chunk_version = ?) '
            f'{body} ORDER BY d.path_key LIMIT ? OFFSET ?', [chunk_version, *params, limit, offset]):
        rows.append({'id': row[0], 'path': row[1], 'ext': row[2], 'status': row[3], 'skip_reason': row[4],
                     'root_id': row[5], 'first_seen_at': row[6], 'last_seen_at': row[7], 'deleted_at': row[8],
                     'latest': None if row[9] is None else dict(zip(latest_keys, row[9:18])),
                     'indexed': bool(row[18])})
    return rows, total


def document_events_query(db: sqlite3.Connection, filters, limit: int, offset: int) -> tuple[list[dict], int]:
    """``(rows, total)`` for ``recent_changes`` (design M3 §2.5), newest first.

    ``filters``: ``since``/``until`` (checked ISO, on the event time), ``kinds`` (subset of
    ``EVENT_KINDS``), ``path_prefix``/``ext`` (checked)."""
    where, params = _document_filter_sql(filters)
    if filters.get('since'):
        where += ' AND julianday(e.at) >= julianday(?)'
        params.append(filters['since'])
    if filters.get('until'):
        where += ' AND julianday(e.at) <= julianday(?)'
        params.append(filters['until'])
    if filters.get('kinds'):
        where += f" AND e.kind IN ({','.join('?' * len(filters['kinds']))})"
        params += list(filters['kinds'])
    body = ('FROM document_events e JOIN documents d ON d.id = e.document_id '
            f'LEFT JOIN versions v ON v.id = e.version_id WHERE 1 = 1{where}')
    total = db.execute(f'SELECT COUNT(*) {body}', params).fetchone()[0]
    rows = [{'id': row[0], 'at': row[1], 'kind': row[2],
             'doc': {'id': row[3], 'path': row[4], 'ext': row[5], 'status': row[6]},
             'version_id': row[7], 'seq': row[8], 'sha256': row[9], 'parse_status': row[10], 'parse_error': row[11],
             'scan_id': row[12]}
            for row in db.execute(
                'SELECT e.id, e.at, e.kind, d.id, d.path, d.ext, d.status, e.version_id, v.seq, v.sha256, '
                f'v.parse_status, v.parse_error, e.scan_id {body} ORDER BY julianday(e.at) DESC, e.id DESC '
                'LIMIT ? OFFSET ?', [*params, limit, offset])]
    return rows, total


# -- M4: knowledge write transactions and counts (design M4 §6.4, §8.2) ------------------------------
# ``knowledge`` is a read-only-to-sources module and does not import sqlite3 (tests/test_readonly.py);
# it opens and ends its one write transaction through these two functions.

class Busy(Exception):
    """Another connection held the write lock past this connection's busy timeout (``store_busy``)."""


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    return 'locked' in str(exc) or 'busy' in str(exc)


def begin_immediate(db: sqlite3.Connection) -> None:
    """``BEGIN IMMEDIATE`` (the write lock for the whole transaction); ``Busy`` when it does not come free,
    with no transaction open."""
    try:
        db.execute('BEGIN IMMEDIATE')
    except sqlite3.OperationalError as exc:
        if not _is_busy(exc):
            raise
        raise Busy(str(exc)) from None


def commit(db: sqlite3.Connection) -> None:
    """``COMMIT``; when the store is busy the transaction is rolled back and ``Busy`` raised, so a failed
    commit always means nothing was written."""
    try:
        db.execute('COMMIT')
    except sqlite3.OperationalError as exc:
        if not _is_busy(exc):
            raise
        db.execute('ROLLBACK')
        raise Busy(str(exc)) from None


def knowledge_counts(db: sqlite3.Connection) -> dict:
    """``{entities, assertions: {active, conflict, withdrawn, superseded}, tasks_open, evidence, events}``
    for ``status``: counts only. ``tasks_open``: open task rows whose state is ``open``."""
    by_status = dict(db.execute('SELECT status, COUNT(*) FROM assertions GROUP BY status'))
    tasks_open = db.execute("SELECT COUNT(*) FROM assertions WHERE kind = 'todo' AND recorded_to IS NULL "
                            "AND json_extract(data_json, '$.state') = 'open'").fetchone()[0]
    return {'entities': db.execute('SELECT COUNT(*) FROM entities').fetchone()[0],
            'assertions': {status: by_status.get(status, 0) for status in ASSERTION_STATUSES},
            'tasks_open': tasks_open,
            'evidence': db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'events': db.execute('SELECT COUNT(*) FROM assertion_events').fetchone()[0]}

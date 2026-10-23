"""SQLite store: connection, schema v1, migration ledger, append-only triggers (design §3).

Source records (documents/versions/units/chunks) and knowledge records
(entities/assertions/evidence/links) live in one WAL database. ``chunk_vectors`` and the
FTS5 index ``chunks_fts`` are derived and may be rebuilt. ``chunks_fts`` is created outside
the migration ledger only when this SQLite has FTS5 with the trigram tokenizer, so a
Python without FTS5 still opens the store (lexical search then falls back to LIKE).

Schema v1 is edited in place until the first real install (M5): no data home has been created
from it yet (design M1 §10.1), and a development store made from an earlier v1 is refused by
the checksum check (delete it and scan again). From the M5 install on, v1 is frozen and every
change is a new migration.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
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

CREATE TABLE chunk_vectors(chunk_id INTEGER NOT NULL REFERENCES chunks(id),
  model_key TEXT NOT NULL,
  text_sha256 TEXT NOT NULL, dims INTEGER NOT NULL, vector BLOB NOT NULL, embedded_at TEXT NOT NULL,
  PRIMARY KEY(chunk_id, model_key));
CREATE INDEX chunk_vectors_text ON chunk_vectors(model_key, text_sha256);

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
  version_id INTEGER NOT NULL REFERENCES versions(id), chunk_id INTEGER REFERENCES chunks(id),
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
"""

# (version, name, sql). Never edit an applied entry; append a new one.
MIGRATIONS = [(1, 'initial_schema_v1', SCHEMA_V1)]

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


def migrate(db: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in one immediate transaction; return the versions applied now."""
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


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the store at ``path``: WAL, foreign keys, busy timeout, schema."""
    db = sqlite3.connect(path, isolation_level=None)
    db.execute('PRAGMA busy_timeout=5000')
    db.execute('PRAGMA journal_mode=WAL')
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

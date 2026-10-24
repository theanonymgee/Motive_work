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
import json
import os
import sqlite3
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

# (version, name, sql). Never edit an applied entry; append a new one.
MIGRATIONS = [(1, 'initial_schema_v1', SCHEMA_V1)]

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


def acquire_lock(home: Path, name: str, at: datetime | None = None) -> tuple[Path, str | None]:
    """Create ``<home>/<name>`` exclusively; ``(path, warning)``. A lock older than 24 hours is
    removed with a warning; a newer one raises ``Locked`` (``ScanLocked`` for ``scan.lock``). The
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
            if attempt == 1 and started is not None and at - started > STALE_LOCK_AGE:
                info = path.read_text(encoding='utf-8', errors='replace')[:200]
                os.remove(path)
                warning = f'warning: removed a stale {what} lock older than 24 hours ({info.strip()})'
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


def insert_chunks(db: sqlite3.Connection, version_id: int, chunk_version: str, specs=(), *,
                  copy_from: int | None = None) -> int:
    """Store the chunks of one version under ``chunk_version`` and add them to ``chunks_fts``.

    ``specs``: objects with ``ordinal, unit_first, unit_last, char_start, char_end, text``
    (``chunks.ChunkSpec``); ``text_sha256`` is computed here. Or ``copy_from``: a version id whose
    ``chunk_version`` rows are copied (same sha256 and parser version, design M2 §4.1 step 2), in
    which case ``specs`` must be empty. The version must have no rows under ``chunk_version`` yet.
    Returns the number of rows stored. No transaction control: the caller commits.
    """
    if db.execute('SELECT 1 FROM chunks WHERE version_id=? AND chunk_version=? LIMIT 1',
                  (version_id, chunk_version)).fetchone():
        raise ValueError(f'version {version_id} already has chunks of {chunk_version}')
    if copy_from is not None:
        if specs:
            raise ValueError('pass specs or copy_from, not both')
        stored = db.execute(
            'INSERT INTO chunks(version_id, chunk_version, ordinal, unit_first, unit_last, char_start, char_end, '
            'text, text_sha256) SELECT ?, chunk_version, ordinal, unit_first, unit_last, char_start, char_end, '
            'text, text_sha256 FROM chunks WHERE version_id=? AND chunk_version=? ORDER BY ordinal',
            (version_id, copy_from, chunk_version)).rowcount
    else:
        sql = ('INSERT INTO chunks(version_id, chunk_version, ordinal, unit_first, unit_last, char_start, char_end, '
               'text, text_sha256) VALUES(?,?,?,?,?,?,?,?,?)')
        stored, batch = 0, []
        for spec in specs:
            batch.append((version_id, chunk_version, spec.ordinal, spec.unit_first, spec.unit_last, spec.char_start,
                          spec.char_end, spec.text, hashlib.sha256(spec.text.encode('utf-8')).hexdigest()))
            if len(batch) >= CHUNK_INSERT_BATCH:
                db.executemany(sql, batch)
                stored, batch = stored + len(batch), []
        if batch:
            db.executemany(sql, batch)
            stored += len(batch)
    if stored:
        if fts_in_step(db):
            db.execute('INSERT INTO chunks_fts(rowid, text) SELECT id, text FROM chunks '
                       'WHERE version_id=? AND chunk_version=?', (version_id, chunk_version))
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

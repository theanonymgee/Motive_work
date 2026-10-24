"""Incremental collector: walk the source roots, detect changes, record versions and units (design M1 §2.4, §3).

Read-only to sources (design §4): an original file is opened in exactly two places,
``read_source`` (whole file, for hashing and parsing the same bytes) and ``stream_sha256``
(files over the size limit), both with mode ``'rb'``. Nothing here writes, moves, re-times or
deletes a file; the only writes are to the store. Parsers get bytes, never a path.

Detection: (size, mtime_ns) first, then sha256. A file whose stat changes while it is read is
``unstable`` and left for the next scan. A returning hash (A -> B -> A) and a reappearing file
are always a new ``versions`` row; units are copied from any earlier version with the same
sha256 and parser version (design §3.3). A move is a delete plus an add (units reused).

Files are committed in batches of ``commit_every``; deletions are judged only after every root
was walked to the end, and never under a folder that could not be listed. Events go to the
``on_event`` callback (the CLI logs them): path, short hash, status and a fixed code, never
document text.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime

from . import parsers, paths, store

EXCLUDED_DIRS = frozenset(name.casefold() for name in (
    '.git', 'node_modules', '.venv', 'venv', '__pycache__', '$RECYCLE.BIN', 'System Volume Information',
    '.Trash-1000'))
DEFAULT_FILE_PATTERNS = ('~$*', '*.tmp', '.~lock*', '~*.tmp', 'Thumbs.db', 'desktop.ini')
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
RECALL_ATTRIBUTES = paths.RECALL_ATTRIBUTES
LOCKED_SHA256 = '0' * 64  # "no hash yet" placeholder of a locked version (design §3.1)
COUNT_KEYS = (
    'seen', 'added', 'changed', 'touched', 'restored', 'deleted', 'unchanged', 'unstable', 'reparsed',
    'parsed_ok', 'reused', 'failed', 'locked', 'too_large', 'unsupported', 'encrypted', 'skipped',
    'excluded', 'ignored', 'links_skipped', 'errors', 'new_versions', 'units')


class SourceLocked(Exception):
    """The file is open elsewhere without read sharing (or access is denied)."""


def _is_locked(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or exc.errno in (13, 32) or getattr(exc, 'winerror', None) in (32, 33)


def read_source(path: str) -> tuple[bytes, str, os.stat_result]:
    """``(bytes, sha256 hex, stat after reading)``; one of the two places that open an original."""
    try:
        with open(path, 'rb') as handle:
            data = handle.read()
        after = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        if _is_locked(exc):
            raise SourceLocked() from None
        raise
    return data, hashlib.sha256(data).hexdigest(), after


def stream_sha256(path: str) -> tuple[str, os.stat_result]:
    """sha256 of a file over the size limit without holding it in memory; the other opening place."""
    try:
        with open(path, 'rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        after = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        if _is_locked(exc):
            raise SourceLocked() from None
        raise
    return digest, after


def file_attributes(info: os.stat_result) -> int:
    """Windows file attributes (0 where the platform has none; separate so tests can stand in)."""
    return getattr(info, 'st_file_attributes', 0) or 0


def matches(name: str, patterns) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def new_counts() -> dict[str, int]:
    return dict.fromkeys(COUNT_KEYS, 0)


def _printable(path: str) -> str:
    return path.encode('utf-8', 'backslashreplace').decode('utf-8')


@dataclass(frozen=True)
class _Latest:
    id: int
    seq: int
    sha256: str
    size: int
    mtime_ns: int
    parse_status: str
    parser_version: str | None
    parse_error: str | None


@dataclass(frozen=True)
class _Doc:
    id: int
    root_id: int
    path: str
    status: str
    latest: _Latest | None


_LOOKUP = ('SELECT d.id, d.root_id, d.path, d.status, v.id, v.seq, v.sha256, v.size, v.mtime_ns, v.parse_status, '
           'v.parser_version, v.parse_error FROM documents d LEFT JOIN versions v ON v.document_id = d.id '
           'AND v.seq = (SELECT MAX(seq) FROM versions WHERE document_id = d.id) WHERE d.path_key = ?')
_VERSION_FIELDS = 'parser, parser_version, encoding, parse_status, parse_error, unit_count, text_chars'


class _Scan:
    def __init__(self, db, settings, *, retry_failed, reparse, rehash, on_event):
        self.db = db
        self.settings = settings
        self.limits = settings.limits
        self.retry_failed, self.reparse, self.rehash = retry_failed, reparse, rehash
        self.on_event = on_event
        self.seen: set[int] = set()
        self.unreadable: list[str] = []
        self.pending = 0

    # -- plumbing ---------------------------------------------------------------------------
    def emit(self, event, path='', sha=None, status=None, code=None):
        if self.on_event is not None:
            self.on_event({'event': event, 'path': _printable(path), 'sha12': (sha or '')[:12],
                           'status': status or '', 'code': code or ''})

    def tick(self):
        self.pending += 1
        if self.pending >= self.settings.commit_every:
            store.bump_data_generation(self.db)  # documents/versions changed: long-lived searches re-map
            self.db.execute('COMMIT')
            self.db.execute('BEGIN IMMEDIATE')
            self.pending = 0

    def lookup(self, key: str) -> _Doc | None:
        row = self.db.execute(_LOOKUP, (key,)).fetchone()
        if row is None:
            return None
        latest = _Latest(*row[4:]) if row[4] is not None else None
        return _Doc(row[0], row[1], row[2], row[3], latest)

    def relocate(self, doc: _Doc, root_id: int, path: str):
        """Same document (same ``path_key``) seen under another root or with another spelling (case)."""
        if doc.root_id != root_id:
            self.db.execute('UPDATE documents SET root_id=? WHERE id=?', (root_id, doc.id))
        if doc.path != path:
            self.db.execute('UPDATE documents SET path=? WHERE id=?', (path, doc.id))

    def mark_seen(self, doc_id: int):
        self.db.execute('UPDATE documents SET last_seen_at=? WHERE id=?', (store.now(), doc_id))
        self.seen.add(doc_id)

    def insert_document(self, root_id, path, key, ext, status='active', skip_reason=None) -> int:
        now = store.now()
        doc_id = self.db.execute(
            'INSERT INTO documents(root_id, path, path_key, ext, status, skip_reason, first_seen_at, last_seen_at) '
            'VALUES(?,?,?,?,?,?,?,?)', (root_id, path, key, ext, status, skip_reason, now, now)).lastrowid
        self.seen.add(doc_id)
        return doc_id

    def activate(self, doc_id: int):
        self.db.execute("UPDATE documents SET status='active', skip_reason=NULL, deleted_at=NULL WHERE id=?", (doc_id,))

    def supersede(self, version_id: int):
        self.db.execute('UPDATE versions SET superseded_at=? WHERE id=? AND superseded_at IS NULL',
                        (store.now(), version_id))

    def needs_retry(self, latest: _Latest, current: str) -> bool:
        status, code = latest.parse_status, latest.parse_error
        # too_large is final except parse_time (depends on the machine's load) and the codes no parser
        # raises any more: xml_tree (the old tree parsers, pptx-2/xlsx-2, whose parts the stream parsers now
        # read) and xml_text (one XML text node; the parsers of the resource design use unit_chars).
        if status in ('locked', 'pending') or (self.retry_failed and (
                status == 'failed' or (status, code) == ('too_large', 'parse_time'))):
            return True
        retryable = status != 'too_large' or code in ('parse_time', 'xml_tree', 'xml_text')
        return self.reparse and retryable and latest.parser_version != current

    # -- versions and units -----------------------------------------------------------------
    def reuse_source(self, sha: str, parser_version: str, exclude: int | None) -> int | None:
        row = self.db.execute(
            "SELECT id FROM versions WHERE sha256=? AND parser_version=? AND parse_status='ok' AND unit_count<=? "
            "AND text_chars<=? AND id<>? ORDER BY id DESC LIMIT 1",
            (sha, parser_version, self.limits.max_units, self.limits.max_text_chars,
             -1 if exclude is None else exclude)).fetchone()
        return row[0] if row else None

    def record(self, doc_id, seq, sha, info, data, ext, counts, fill=None) -> tuple[str, str | None]:
        """New version row (or fill/redo ``fill`` in place) with its units; ``(status, code)``."""
        parser = parsers.parser_for(ext)
        units, source = (), None
        if data is None:
            fields = (parser.name, parser.version, None, 'too_large', 'file_bytes', 0, 0)
            counts['too_large'] += 1
        elif (source := self.reuse_source(sha, parser.version, fill)) is not None:
            fields = self.db.execute(f'SELECT {_VERSION_FIELDS} FROM versions WHERE id=?', (source,)).fetchone()
            counts['reused'] += 1
        else:
            result = parsers.parse(data, ext, self.limits)
            units = result.units
            fields = (result.parser, result.parser_version, result.encoding, result.status, result.error,
                      len(units), sum(len(unit.text) for unit in units))
            counts['parsed_ok' if result.status == 'ok' else result.status] += 1
        if fill is None:
            version_id = self.db.execute(
                f'INSERT INTO versions(document_id, seq, sha256, size, mtime_ns, observed_at, {_VERSION_FIELDS}) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (doc_id, seq, sha, info.st_size, info.st_mtime_ns, store.now(), *fields)).lastrowid
            counts['new_versions'] += 1
        else:
            version_id = fill
            store.drop_chunks(self.db, fill)  # with their FTS rows; vectors are content-addressed and stay
            self.db.execute('DELETE FROM units WHERE version_id=?', (fill,))
            self.db.execute(
                'UPDATE versions SET sha256=?, size=?, mtime_ns=?, parser=?, parser_version=?, encoding=?, '
                'parse_status=?, parse_error=?, unit_count=?, text_chars=? WHERE id=?',
                (sha, info.st_size, info.st_mtime_ns, *fields, fill))
        if source is not None:
            self.db.execute('INSERT INTO units(version_id, ordinal, kind, locator_json, text, text_sha256) '
                            'SELECT ?, ordinal, kind, locator_json, text, text_sha256 FROM units '
                            'WHERE version_id=? ORDER BY ordinal', (version_id, source))
        elif units:
            self.db.executemany(
                'INSERT INTO units(version_id, ordinal, kind, locator_json, text, text_sha256) VALUES(?,?,?,?,?,?)',
                [(version_id, unit.ordinal, unit.kind, parsers.unit_json(unit.unit), unit.text,
                  hashlib.sha256(unit.text.encode('utf-8')).hexdigest()) for unit in units])
        counts['units'] += fields[5]
        return fields[3], fields[4]

    # -- one file ---------------------------------------------------------------------------
    def placeholder(self, root_id, path, ext, counts):
        key = os.path.normcase(path)
        doc = self.lookup(key)
        counts['skipped'] += 1
        if doc is None:
            self.insert_document(root_id, path, key, ext, 'skipped', 'cloud_placeholder')
        else:
            if doc.status == 'deleted':
                self.db.execute("UPDATE documents SET status='skipped', skip_reason='cloud_placeholder', "
                                "deleted_at=NULL WHERE id=?", (doc.id,))
            self.relocate(doc, root_id, path)
            self.mark_seen(doc.id)
        self.emit('skipped', path, code='cloud_placeholder')

    def locked(self, root_id, doc, path, key, ext, info, counts):
        counts['locked'] += 1
        if doc is None or doc.latest is None:
            doc_id = self.insert_document(root_id, path, key, ext) if doc is None else doc.id
            if doc is not None:
                self.activate(doc_id)
                self.mark_seen(doc_id)
            self.db.execute('INSERT INTO versions(document_id, seq, sha256, size, mtime_ns, observed_at, parse_status) '
                            "VALUES(?, 1, ?, ?, ?, ?, 'locked')",
                            (doc_id, LOCKED_SHA256, info.st_size, info.st_mtime_ns, store.now()))
            counts['new_versions'] += 1
            counts['added'] += 1
            self.emit('added', path, status='locked')
            return
        self.mark_seen(doc.id)  # a known file stays as it was until it can be read
        self.emit('locked', path, status='locked')

    def process_file(self, root_id, path, ext, counts):
        key = os.path.normcase(path)
        doc = self.lookup(key)
        if doc is not None:
            self.relocate(doc, root_id, path)
        try:
            before = os.stat(path, follow_symlinks=False)
        except OSError:
            if doc is not None:
                self.mark_seen(doc.id)
            counts['errors'] += 1
            self.emit('error', path, code='stat_failed')
            return
        if file_attributes(before) & RECALL_ATTRIBUTES and not self.settings.read_cloud_placeholders:
            self.placeholder(root_id, path, ext, counts)  # became online-only after the folder was listed
            return
        latest = doc.latest if doc is not None else None
        current = parsers.parser_for(ext).version
        if (doc is not None and doc.status == 'active' and latest is not None and not self.rehash
                and (before.st_size, before.st_mtime_ns) == (latest.size, latest.mtime_ns)
                and not self.needs_retry(latest, current)):
            self.mark_seen(doc.id)
            counts['unchanged'] += 1
            return
        try:
            if before.st_size > self.limits.max_file_bytes:
                data = None
                sha, after = stream_sha256(path)
            else:
                data, sha, after = read_source(path)
        except SourceLocked:
            self.locked(root_id, doc, path, key, ext, before, counts)
            return
        except OSError as exc:
            if doc is not None:
                self.mark_seen(doc.id)
            vanished = isinstance(exc, FileNotFoundError)
            counts['unstable' if vanished else 'errors'] += 1
            self.emit('unstable' if vanished else 'error', path, code='vanished' if vanished else 'read_failed')
            return
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            if doc is not None:
                self.mark_seen(doc.id)
            counts['unstable'] += 1
            self.emit('unstable', path, code='changed_while_reading')
            return
        status = code = None
        if doc is None:
            doc_id = self.insert_document(root_id, path, key, ext)
            status, code = self.record(doc_id, 1, sha, after, data, ext, counts)
            outcome = 'added'
        elif latest is None:
            doc_id = doc.id
            self.activate(doc_id)
            status, code = self.record(doc_id, 1, sha, after, data, ext, counts)
            outcome = 'added'
        elif latest.parse_status == 'locked' and latest.sha256 == LOCKED_SHA256:
            doc_id = doc.id
            self.activate(doc_id)
            status, code = self.record(doc_id, latest.seq, sha, after, data, ext, counts, fill=latest.id)
            outcome = 'reparsed'
        elif doc.status in ('deleted', 'skipped'):
            doc_id = doc.id
            self.activate(doc_id)
            self.supersede(latest.id)
            status, code = self.record(doc_id, latest.seq + 1, sha, after, data, ext, counts)
            outcome = 'restored'
        elif sha != latest.sha256:
            doc_id = doc.id
            self.supersede(latest.id)
            status, code = self.record(doc_id, latest.seq + 1, sha, after, data, ext, counts)
            outcome = 'changed'
        elif self.needs_retry(latest, current):
            doc_id = doc.id
            status, code = self.record(doc_id, latest.seq, sha, after, data, ext, counts, fill=latest.id)
            outcome = 'reparsed'
        elif after.st_mtime_ns != latest.mtime_ns:
            doc_id = doc.id
            self.db.execute('UPDATE versions SET mtime_ns=? WHERE id=?', (after.st_mtime_ns, latest.id))
            outcome = 'touched'
        else:
            doc_id = doc.id
            outcome = 'unchanged'  # --rehash found the same content
        self.mark_seen(doc_id)
        counts[outcome] += 1
        if outcome != 'unchanged':
            self.emit(outcome, path, sha, status, code)

    # -- walking ----------------------------------------------------------------------------
    def walk(self, root_id, spec, counts):
        name_patterns = DEFAULT_FILE_PATTERNS + tuple(self.settings.exclude)
        stack = [(str(spec.path), '')]
        while stack:
            folder, rel = stack.pop()
            try:
                with os.scandir(folder) as listing:
                    entries = sorted(listing, key=lambda item: item.name)
            except OSError:
                counts['errors'] += 1
                self.unreadable.append(os.path.normcase(folder))
                self.emit('error', folder, code='folder_unreadable')
                continue
            folders = []
            for entry in entries:
                name = entry.name
                path = os.path.join(folder, name)
                relpath = f'{rel}/{name}' if rel else name
                try:
                    path.encode('utf-8')
                except UnicodeEncodeError:
                    counts['errors'] += 1
                    self.emit('error', path, code='name_not_utf8')
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    junction = entry.is_junction()
                    is_link = entry.is_symlink() or junction
                    is_dir = entry.is_dir(follow_symlinks=False) or junction
                except OSError:
                    # Unknown whether a file or a folder: nothing at or under this path is judged deleted.
                    counts['errors'] += 1
                    self.unreadable.append(os.path.normcase(path))
                    self.emit('error', path, code='stat_failed')
                    continue
                attributes = file_attributes(info)
                hidden = attributes & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
                linkish = is_link or bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT and not attributes & RECALL_ATTRIBUTES)
                if is_dir:
                    if name.casefold() in EXCLUDED_DIRS or hidden or matches(relpath, spec.exclude):
                        counts['excluded'] += 1
                    elif linkish:
                        counts['links_skipped'] += 1
                        self.emit('link_skipped', path)
                    else:
                        folders.append((path, relpath))
                    continue
                if hidden or matches(name, name_patterns) or matches(relpath, spec.exclude):
                    counts['excluded'] += 1
                    continue
                if linkish:
                    counts['links_skipped'] += 1
                    self.emit('link_skipped', path)
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext not in parsers.SUPPORTED_EXTENSIONS:
                    counts['ignored'] += 1
                    continue
                counts['seen'] += 1
                if attributes & RECALL_ATTRIBUTES and not self.settings.read_cloud_placeholders:
                    self.placeholder(root_id, path, ext, counts)
                else:
                    self.process_file(root_id, path, ext, counts)
                self.tick()
            stack.extend(reversed(folders))

    def judge_deleted(self, root_id, counts):
        """Active or skipped documents of a fully walked root that were not seen (design §3.4).

        Never a document at or under a path that could not be listed or stat'ed (``unreadable``)."""
        unknown = set(self.unreadable)
        hidden_under = [folder.rstrip(os.sep) + os.sep for folder in self.unreadable]
        rows = self.db.execute("SELECT id, path, path_key FROM documents WHERE root_id=? AND status IN "
                               "('active','skipped')", (root_id,)).fetchall()
        for doc_id, path, key in rows:
            if doc_id in self.seen or key in unknown or any(key.startswith(prefix) for prefix in hidden_under):
                continue
            self.db.execute("UPDATE documents SET status='deleted', deleted_at=? WHERE id=?", (store.now(), doc_id))
            counts['deleted'] += 1
            self.emit('deleted', path)


def scan(db, roots, settings, *, partial=False, retry_failed=False, reparse=False, rehash=False,
         on_event=None) -> dict:
    """Collect ``roots`` (``[(root_id, RootSpec), ...]``) and return the summary.

    ``db`` is an autocommit connection (``store.open_home``). The caller holds ``scan.lock``.
    On any exception the open batch is rolled back, the scan row becomes ``aborted`` and no
    deletion is judged; the exception propagates.
    """
    started = datetime.now().astimezone()
    job = _Scan(db, settings, retry_failed=retry_failed, reparse=reparse, rehash=rehash, on_event=on_event)
    left = db.execute("SELECT COUNT(*) FROM scans WHERE status='running'").fetchone()[0]
    if left:
        db.execute("UPDATE scans SET status='aborted', finished_at=?, note='left running by an earlier scan' "
                   "WHERE status='running'", (store.now(),))
        job.emit('warning', code='stale_running_scan_aborted', status=str(left))
    flags = [name for name, on in (('rehash', rehash), ('reparse', reparse), ('retry-failed', retry_failed)) if on]
    mode = '+'.join(['partial' if partial else 'full', *flags])
    started_at = started.isoformat(timespec='seconds')
    scan_id = db.execute("INSERT INTO scans(started_at, status, partial, roots_json) VALUES(?, 'running', ?, ?)",
                         (started_at, int(bool(partial)),
                          json.dumps([str(spec.path) for _, spec in roots], ensure_ascii=False))).lastrowid
    per_root = [(spec, new_counts()) for _, spec in roots]

    def counts_json():
        total = new_counts()
        for _, counts in per_root:
            for key in COUNT_KEYS:
                total[key] += counts[key]
        return total, json.dumps({'total': total, 'roots': {str(spec.path): counts for spec, counts in per_root}},
                                 ensure_ascii=False)

    db.execute('BEGIN IMMEDIATE')
    try:
        for (root_id, spec), (_, counts) in zip(roots, per_root):
            job.walk(root_id, spec, counts)
        for (root_id, _), (_, counts) in zip(roots, per_root):
            job.judge_deleted(root_id, counts)
        store.bump_data_generation(db)
        db.execute('COMMIT')
    except BaseException as exc:
        if db.in_transaction:
            db.execute('ROLLBACK')
        db.execute("UPDATE scans SET status='aborted', finished_at=?, counts_json=?, note=? WHERE id=?",
                   (store.now(), counts_json()[1], type(exc).__name__, scan_id))
        raise
    total, encoded = counts_json()
    finished = datetime.now().astimezone()
    db.execute("UPDATE scans SET status='done', finished_at=?, counts_json=? WHERE id=?",
               (finished.isoformat(timespec='seconds'), encoded, scan_id))
    return {'scan_id': scan_id, 'status': 'done', 'started_at': started_at,
            'finished_at': finished.isoformat(timespec='seconds'), 'partial': bool(partial), 'mode': mode,
            'elapsed': (finished - started).total_seconds(),
            'roots': [{'path': str(spec.path), 'counts': counts} for spec, counts in per_root], 'total': total}

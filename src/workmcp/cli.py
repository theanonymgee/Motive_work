"""Command line: ``doctor``, ``serve``, ``scan``, ``verify``, ``sources`` (design M0 §8, M1 §9) and
``index``, ``search``, ``fetch-model``, ``verify-model`` (design M2 §1.2).

A data-home rule violation stops every command before anything is created: the reason goes
to stderr, stdout stays empty and the exit code is 2. ``scan`` exit codes: 0 done (parse
failures are records, not errors), 2 data home, 3 another scan holds the lock, 4 sources or
settings. ``verify``: 0 binding ok, 1 failures, 2/4 as above.

No document text reaches stdout, stderr or the log: the collector and the binding check report
paths, short hashes, statuses and fixed codes only. The scan log is
``<home>/logs/scan-YYYYMMDD.log`` (outside the repository); ``serve`` never logs to a file.

M2: ``index`` exit codes 0 done, 2 data home, 3 another index holds ``index.lock``, 4 model name or
settings; ``search`` 0, 2, 4 (empty query, a value out of range, no store); ``fetch-model`` 0 when
every file is in place, 1 otherwise, 4 without ``--from``; ``verify-model`` 0/1. ``index`` and
``search`` import their modules (numpy) only when they run; ``scan``/``verify``/``doctor`` do not.
``search`` prints snippets to the console on purpose (the user reads them locally); the index log
has counts and fixed codes only, never document text or queries.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, models, paths, store

LOG_FORMAT = '%(asctime)s | %(message)s'


def _tolerant(stream) -> None:
    """Keep the stream's encoding (readers of a pipe expect it) but print ``?`` for a character it lacks."""
    try:
        stream.reconfigure(errors='replace')
    except (AttributeError, ValueError, OSError):
        pass


def _home():
    """``(home, None)`` or ``(None, exit code 2)`` after printing the rule broken."""
    try:
        return paths.resolve_home(), None
    except paths.DataPathError as exc:
        print(exc, file=sys.stderr)
        return None, 2


def _root_lines(home) -> list[str]:
    from . import config
    try:
        cfg = config.load(home)
    except config.ConfigError as exc:
        return [f'sources: config error: {exc}']
    if not cfg.sources:
        return [f'sources: none configured ({cfg.file})']
    lines = []
    for check in config.check_roots(cfg.sources, home, paths.find_repo_root()):
        if check.error:
            lines.append(f'source root {check.entry.raw}: {check.error}')
        else:
            lines.append(f'source root {check.spec.path}: ok')
            lines += [f'  {warning}' for warning in check.spec.warnings]
    return lines


def doctor() -> int:
    home, code = _home()
    if code:
        return code
    db_file = paths.db_path(home)
    schema, index_lines = None, []
    if db_file.is_file():
        db = sqlite3.connect(f'{db_file.as_uri()}?mode=ro', uri=True)
        try:
            schema = db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
            stored = db.execute('SELECT checksum FROM schema_migrations WHERE version=1').fetchone()
            if stored and stored[0] != store.migration_checksum(1):
                schema = f'{schema}; {store.DEV_SCHEMA_HINT.format(db=db_file)}'
            else:
                index_lines = _index_lines(db)
        except sqlite3.Error:
            schema = 'unreadable'
        finally:
            db.close()
    model = models.quick_status(models.DEFAULT_MODEL, home)
    fts5 = store.fts5_available()
    lines = [
        f'workmcp {__version__}',
        f'python: {".".join(map(str, sys.version_info[:3]))} ({sys.executable})',
        f'mcp: {importlib.metadata.version("mcp")}',
        f'sqlite: {sqlite3.sqlite_version}, fts5 trigram: {"yes" if fts5 else "no (lexical search falls back to LIKE)"}',
        f'data home: {home} (rules ok; {"exists" if home.exists() else "not created yet"})',
        *filter(None, [paths.drive_warning(home, os.environ)]),
        f'store: {db_file} ({f"schema v{schema}" if schema is not None else "not created yet"})',
        *index_lines,
        f'model {model["name"]}: {"available" if model["available"] else "unavailable"} - {model["message"]}',
        *_root_lines(home),
    ]
    print('\n'.join(lines))
    return 0


def _index_lines(db) -> list[str]:
    """``index: chunks N (chunk_version ...), vectors M (model key ...), fts rows K (synced ...)`` and a
    warning when ``chunks_fts`` and ``chunks`` disagree (design M2 §1.2, §4.3). Counts only."""
    try:
        versions = [row[0] for row in db.execute('SELECT DISTINCT chunk_version FROM chunks ORDER BY 1')]
        vectors = db.execute('SELECT model_key, COUNT(*) FROM chunk_vectors GROUP BY model_key ORDER BY 1').fetchall()
        fts = store.fts_status(db)
    except sqlite3.Error:
        return ['index: unreadable']
    chunk_part = f"chunks {fts['chunks']:,} (chunk_version {', '.join(versions) or 'none'})"
    vector_part = 'vectors ' + (', '.join(f'{count:,} (model key {key[:12]})' for key, count in vectors) or '0')
    if fts['available']:
        synced = f"synced {fts['synced_at']}" if fts['synced_at'] else 'never synced'
        fts_part = f"fts rows {fts['rows']:,} ({synced})"
    else:
        fts_part = 'fts unavailable (lexical search falls back to LIKE)'
    lines = [f'index: {chunk_part}, {vector_part}, {fts_part}']
    if fts['available'] and not fts['consistent']:
        lines.append(f"warning: chunks_fts has {fts['rows']:,} rows for {fts['chunks']:,} chunks; "
                     f'run workmcp index --rebuild-fts')
    return lines


def _open_store(home):
    """Migrated store, or ``(None, 2)`` with the development-schema hint."""
    try:
        return store.open_home(home), None
    except RuntimeError as exc:
        if store.schema_changed(exc):
            print(store.DEV_SCHEMA_HINT.format(db=paths.db_path(home)), file=sys.stderr)
            return None, 2
        raise


def _file_logger(home, kind: str):
    """``<home>/logs/<kind>-YYYYMMDD.log`` (``scan`` or ``index``), never the console."""
    log_file = home/'logs'/f'{kind}-{datetime.now():%Y%m%d}.log'
    handler = logging.FileHandler(log_file, encoding='utf-8')
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger = logging.getLogger(f'workmcp.{kind}')
    logger.setLevel(logging.INFO)
    logger.propagate = False  # never to the console: the summary is the console output
    logger.addHandler(handler)
    return logger, handler, log_file


def _log_event(logger, event: dict) -> None:
    logger.info(' | '.join((event['event'], event['path'], event['sha12'], event['status'], event['code'])))


def _counts_lines(counts: dict) -> list[str]:
    c = counts
    return [
        f"    seen {c['seen']}  added {c['added']}  changed {c['changed']}  touched {c['touched']}  "
        f"restored {c['restored']}  deleted {c['deleted']}  unchanged {c['unchanged']}  unstable {c['unstable']}  "
        f"reparsed {c['reparsed']}",
        f"    parsed ok {c['parsed_ok']}  reused {c['reused']}  failed {c['failed']}  locked {c['locked']}  "
        f"too_large {c['too_large']}  unsupported {c['unsupported']}  encrypted {c['encrypted']}  "
        f"skipped {c['skipped']}",
        f"    excluded {c['excluded']}  ignored {c['ignored']}  links_skipped {c['links_skipped']}  errors {c['errors']}",
    ]


def summary_lines(result: dict, log_file) -> list[str]:
    lines = [f"scan #{result['scan_id']} started {result['started_at']}  roots {len(result['roots'])}  "
             f"mode {result['mode']}"]
    for root in result['roots']:
        lines.append(f"  {root['path']}")
        lines += _counts_lines(root['counts'])
    total = result['total']
    lines.append(f"total {total['seen']:,} files  {total['new_versions']:,} new versions  {total['units']:,} units  "
                 f"{result['elapsed']:.1f}s   log: {log_file}")
    return lines


def _entries(args, cfg):
    from . import config
    if args.root:
        return [config.SourceEntry(raw) for raw in args.root], False
    return list(cfg.sources), True


def scan(args) -> int:
    from . import collect, config
    home, code = _home()
    if code:
        return code
    paths.ensure_home(home)
    try:
        cfg = config.load(home)
    except config.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 4
    entries, from_config = _entries(args, cfg)
    if not entries:
        print(f'no sources configured: write {config.config_file(home)} [[sources]] or pass --root', file=sys.stderr)
        return 4
    checks = config.check_roots(entries, home, paths.find_repo_root())
    if errors := [check.error for check in checks if check.error]:
        for error in errors:
            print(error, file=sys.stderr)
        return 4
    specs = [check.spec for check in checks]
    logger, handler, log_file = _file_logger(home, 'scan')
    try:
        warnings = [warning for spec in specs for warning in spec.warnings]
        try:
            lock, stale = store.acquire_scan_lock(home)
        except store.ScanLocked as exc:
            print(exc, file=sys.stderr)
            logger.info(f'refused | {home} |  | locked | scan_lock_held')
            return 3
        try:
            if stale:
                warnings.append(stale)
            for warning in warnings:
                print(warning, file=sys.stderr)
                logger.info(f'warning | {warning}')
            db, code = _open_store(home)
            if code:
                return code
            try:
                ids = config.sync_roots(db, specs, from_config=from_config)

                def on_event(event):
                    _log_event(logger, event)
                    if event['event'] == 'warning':
                        print(f"warning: {event['code']} ({event['status']})", file=sys.stderr)

                try:
                    result = collect.scan(db, [(ids[spec.key], spec) for spec in specs], cfg.scan,
                                          partial=not from_config, retry_failed=args.retry_failed,
                                          reparse=args.reparse, rehash=args.rehash, on_event=on_event)
                except Exception as exc:  # class name only: a message could carry a file name or value
                    logger.info(f'aborted |  |  | aborted | {type(exc).__name__}')
                    print(f'scan aborted ({type(exc).__name__}); see {log_file}', file=sys.stderr)
                    return 1
            finally:
                db.close()
        finally:
            store.release_scan_lock(lock)
        logger.info(f"done | scan #{result['scan_id']} |  | done | {result['mode']}")
        print('\n'.join(summary_lines(result, log_file)))
        return 0
    finally:
        logger.removeHandler(handler)
        handler.close()


def verify_lines(report: dict) -> list[str]:
    v, u = report['versions'], report['units']
    un = v['unverifiable']
    lines = [
        f"verify {report['checked_at']}  versions {v['total']} checked {v['checked']} ok {v['ok']} "
        f"failed {v['failed']}  unverifiable {sum(un.values())} (stale {un['stale']}, missing {un['missing']}, "
        f"locked {un['locked']}, unstable {un['unstable']}, parser {un['parser_version_mismatch']}, "
        f"not_ok {un['skipped_not_ok']}, placeholder {un['placeholder']}, timeout {un['timeout']})",
        f"units {u['bound']}/{u['total']} bound ({u['rate'] * 100:.1f}%)  chunks {report['chunks_checked']}  "
        f"evidence {report['evidence_checked']}",
        *report['notice'],
    ]
    if report['ok']:
        lines.append('binding ok' if v['checked'] else 'binding ok (nothing verified)')
    else:
        lines.append('binding FAILED')
        lines += [f"{f['path']} @ {f['sha12']} / {f['unit_label']} : {f['problem']}" for f in report['failures']]
    return lines


def verify(args) -> int:
    from . import binding, config
    home, code = _home()
    if code:
        return code
    if not paths.db_path(home).is_file():
        print(f'store not created yet ({paths.db_path(home)}); run workmcp scan first', file=sys.stderr)
        return 4
    try:
        cfg = config.load(home)
    except config.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 4
    db, code = _open_store(home)
    if code:
        return code
    try:
        root_ids = None
        if args.root:
            root_ids = []
            for raw in args.root:
                key = os.path.normcase(paths._strip_long_prefix(str(Path(raw).resolve())))
                row = db.execute('SELECT id FROM source_roots WHERE path=?', (key,)).fetchone()
                if row is None:
                    print(f'not a scanned source root: {raw}', file=sys.stderr)
                    return 4
                root_ids.append(row[0])
        report = binding.verify(db, root_ids=root_ids, document_ids=args.doc or None, limits=cfg.scan.limits,
                                read_cloud_placeholders=cfg.scan.read_cloud_placeholders)
    finally:
        db.close()
    print('\n'.join(verify_lines(report)))
    return 0 if report['ok'] else 1


def _model_name_error(name: str, allow_none: bool) -> str | None:
    known = sorted(models.MANIFEST) + (['none'] if allow_none else [])
    if name in known:
        return None
    return f'unknown model {name!r}; known: {", ".join(known)}'


def _index_embedder(name: str, home, settings):
    """``(embedder or None, vectors note or None)`` for ``index`` (design M2 §3.4): no model, or a
    model that cannot load, means chunks and FTS only with the reason; never an error exit."""
    if name == 'none':
        return None, None
    status = models.quick_status(name, home)
    if not status['available']:
        return None, f"model_missing: {status['message']}"
    from . import embedder
    try:
        return embedder.OnnxEmbedder(name, home, batch_size=settings.embed_batch, threads=settings.threads or None), None
    except ImportError as exc:
        return None, f'runtime_import_failed:{type(exc).__name__}'
    except models.ModelFileError:
        bad = {key: value for key, value in models.verify(name, home).items() if value != 'ok'}
        return None, 'model_unverified(' + ', '.join(f'{key}: {value}' for key, value in bad.items()) + ')'


def index_summary_lines(result: dict, log_file) -> list[str]:
    """Console summary of ``index.run_index`` (design M2 §4.4): counts and times, no document text."""
    r, elapsed = result, result['elapsed']
    model = f"model {r['model']} (key {r['model_key'][:12]})" if r['model_key'] else 'model none'
    vectors = (f"vectors {r['vectors_for_model']:,} for this model" if r['model_key']
               else f"vectors: skipped ({r['vectors_note'] or 'model none'})")
    fts = 'fts unavailable' if r['fts_rows'] is None else f"fts rows {r['fts_rows']:,}"
    return [
        f"index #{r['index_id']} started {r['started_at']}  {model}  chunk_version {r['chunk_version']}",
        f"  versions {r['versions_eligible']:,} eligible  chunked {r['chunked']:,} (reused {r['reused_chunks']:,}, "
        f"unchanged {r['unchanged']:,})  dropped {r['dropped_versions']:,} versions  "
        f"skipped_locked {r['skipped_locked']:,}",
        f"  chunks {r['chunks_total']:,} total  embedded {r['embedded']:,}  reused {r['reused_vectors']:,}  "
        f"truncated {r['truncated']:,}  remaining {r['remaining']:,}  (limit {r['limit'] or 'none'})",
        f"  {fts}  {vectors}",
        f"  load {elapsed['load']:.1f}s  chunk {elapsed['chunk']:.1f}s  embed {elapsed['embed']:.1f}s  "
        f"total {elapsed['total']:.1f}s   log: {log_file}",
    ]


def _log_index_event(logger, event: dict) -> None:
    logger.info(' | '.join(str(event.get(key) or '') for key in ('event', 'path', 'sha12', 'chunks', 'note')))


def index_command(args) -> int:
    from . import config
    home, code = _home()
    if code:
        return code
    paths.ensure_home(home)
    try:
        cfg = config.load(home)
    except config.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 4
    name = args.model or cfg.index.model
    if error := _model_name_error(name, allow_none=True):
        print(error, file=sys.stderr)
        return 4
    if args.limit is not None and args.limit < 1:
        print('--limit must be a positive integer', file=sys.stderr)
        return 4
    logger, handler, log_file = _file_logger(home, 'index')
    try:
        try:
            lock, stale = store.acquire_lock(home, store.INDEX_LOCK_NAME)
        except store.Locked as exc:
            print(exc, file=sys.stderr)
            logger.info(f'refused | {home} |  |  | index_lock_held')
            return 3
        try:
            if stale:
                print(stale, file=sys.stderr)
                logger.info(f'warning | {stale}')
            db, code = _open_store(home)
            if code:
                return code
            try:
                from . import index
                try:
                    embedder, note = _index_embedder(name, home, cfg.index)
                    result = index.run_index(db, home, embedder=embedder, settings=cfg.index,
                                       on_event=lambda event: _log_index_event(logger, event), limit=args.limit,
                                       doc_ids=args.doc or None, rebuild_fts=args.rebuild_fts, gc=args.gc,
                                       keep_history=args.keep_history, vectors_note=note)
                except NotImplementedError:
                    print('workmcp index: not implemented yet (M2 stage 1)', file=sys.stderr)
                    return 1
                except Exception as exc:  # class name only: a message could carry a file name or value
                    logger.info(f'aborted |  |  |  | {type(exc).__name__}')
                    print(f'index aborted ({type(exc).__name__}); see {log_file}', file=sys.stderr)
                    return 1
            finally:
                db.close()
        finally:
            store.release_lock(lock)
        logger.info(f"done | index #{result['index_id']} |  |  | done")
        print('\n'.join(index_summary_lines(result, log_file)))
        return 0
    finally:
        logger.removeHandler(handler)
        handler.close()


def search_command(args) -> int:
    from . import config, search
    home, code = _home()
    if code:
        return code
    if not paths.db_path(home).is_file():
        print(f'store not created yet ({paths.db_path(home)}); run workmcp scan and workmcp index first',
              file=sys.stderr)
        return 4
    try:
        cfg = config.load(home)
    except config.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 4
    if args.mode not in search.MODES:
        print(f'--mode must be one of {", ".join(search.MODES)}', file=sys.stderr)
        return 4
    engine = search.Engine(home, cfg.search)
    try:
        result = engine.search(args.query, mode=args.mode, limit=args.limit, path_prefix=args.path_prefix,
                               ext=args.ext or None, since=args.since, until=args.until)
    except search.SearchError as exc:
        print(exc, file=sys.stderr)
        return 4
    except NotImplementedError:
        print('workmcp search: not implemented yet (M2 stage 1)', file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print('\n'.join(search.format_hits(result)))
    return 0


FETCH_FROM_HINT = ('fetch-model needs --from FOLDER: a folder already on this PC (an earlier download or an offline '
                   'bundle). M2 does not download over the network; python -m workmcp.models fetch is the explicit '
                   'download module and is not used.')


def fetch_model_command(args) -> int:
    home, code = _home()
    if code:
        return code
    if error := _model_name_error(args.name, allow_none=False):
        print(error, file=sys.stderr)
        return 4
    if not args.source:
        print(FETCH_FROM_HINT, file=sys.stderr)
        return 4
    if not Path(args.source).is_absolute() or not Path(args.source).is_dir():
        print(f'--from must be an existing absolute folder: {args.source}', file=sys.stderr)
        return 4
    paths.ensure_home(home)
    try:
        return models.cli_fetch_model(args, home)
    except NotImplementedError:
        print('workmcp fetch-model: not implemented yet (M2 stage 1)', file=sys.stderr)
        return 1


def verify_model_command(args) -> int:
    home, code = _home()
    if code:
        return code
    if error := _model_name_error(args.name, allow_none=False):
        print(error, file=sys.stderr)
        return 4
    return models.cli_verify_model(args, home)


def sources(args) -> int:
    from . import config
    home, code = _home()
    if code:
        return code
    try:
        cfg = config.load(home)
    except config.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 4
    table, counts = {}, {}
    if paths.db_path(home).is_file():
        db = store.open_read(home)
        try:
            table = {path: (root_id, enabled) for root_id, path, enabled in
                     db.execute('SELECT id, path, enabled FROM source_roots ORDER BY id')}
            for root_id, status, number in db.execute(
                    'SELECT root_id, status, COUNT(*) FROM documents GROUP BY root_id, status'):
                counts.setdefault(root_id, {})[status] = number
        finally:
            db.close()

    def stored(key):
        if key not in table:
            return 'not scanned yet'
        root_id, enabled = table[key]
        c = counts.get(root_id, {})
        return (f"enabled {enabled}  documents {sum(c.values())} (active {c.get('active', 0)}, "
                f"deleted {c.get('deleted', 0)}, skipped {c.get('skipped', 0)})")

    lines = [f"sources in {cfg.file}{'' if cfg.exists else ' (file not found)'}"]
    listed = set()
    for check in config.check_roots(cfg.sources, home, paths.find_repo_root()):
        if check.error:
            lines.append(f'  {check.entry.raw}  {check.error.code}: {check.error.message}')
            continue
        listed.add(check.spec.key)
        lines.append(f'  {check.spec.path}  ok  {stored(check.spec.key)}')
        lines += [f'    {warning}' for warning in check.spec.warnings]
    others = [key for key in table if key not in listed]
    if others:
        lines.append('other roots in the store (not in config.toml):')
        lines += [f'  {key}  {stored(key)}' for key in others]
    print('\n'.join(lines))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='workmcp')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor', help='check data home, Python, SQLite/FTS5, mcp, model files and source roots')
    sub.add_parser('serve', help='run the stdio MCP server')
    scan_parser = sub.add_parser('scan', help='collect the source roots incrementally')
    scan_parser.add_argument('--root', action='append', metavar='PATH', help='scan this folder instead of config.toml')
    scan_parser.add_argument('--retry-failed', action='store_true', help='parse failed files again')
    scan_parser.add_argument('--reparse', action='store_true', help='re-parse versions stored by another parser version')
    scan_parser.add_argument('--rehash', action='store_true', help='hash every file even when size and mtime match')
    verify_parser = sub.add_parser('verify', help='check that stored units re-derive from the current files')
    verify_parser.add_argument('--root', action='append', metavar='PATH')
    verify_parser.add_argument('--doc', action='append', type=int, metavar='ID')
    sub.add_parser('sources', help='list the configured source roots and their state')
    index_parser = sub.add_parser('index', help='chunk and embed the collected versions incrementally')
    index_parser.add_argument('--model', metavar='NAME', help='manifest model or "none" (default: config [index] model)')
    index_parser.add_argument('--doc', action='append', type=int, metavar='ID', help='only this document')
    index_parser.add_argument('--limit', type=int, metavar='N', help='embed at most N chunk texts in this run')
    index_parser.add_argument('--rebuild-fts', action='store_true', help='rebuild the full-text index first')
    index_parser.add_argument('--gc', action='store_true', help='drop vectors no chunk uses')
    index_parser.add_argument('--keep-history', action='store_true', help='keep chunks of superseded versions')
    search_parser = sub.add_parser('search', help='hybrid search with exact sources')
    search_parser.add_argument('query')
    search_parser.add_argument('--mode', default='hybrid', metavar='hybrid|lexical|semantic')
    search_parser.add_argument('--limit', type=int, default=10, metavar='N')
    search_parser.add_argument('--path-prefix', metavar='P')
    search_parser.add_argument('--ext', action='append', metavar='.EXT')
    search_parser.add_argument('--since', metavar='ISO')
    search_parser.add_argument('--until', metavar='ISO')
    search_parser.add_argument('--json', action='store_true', help='the M3 tool result as JSON')
    fetch_parser = sub.add_parser('fetch-model', help='copy and verify model files from a folder on this PC')
    fetch_parser.add_argument('name', metavar='NAME')
    fetch_parser.add_argument('--from', dest='source', metavar='FOLDER')
    verify_model_parser = sub.add_parser('verify-model', help='check the model files against the manifest SHA-256')
    verify_model_parser.add_argument('name', nargs='?', default=models.DEFAULT_MODEL, metavar='NAME')
    _tolerant(sys.stderr)
    args = parser.parse_args(argv)
    if args.command != 'serve':  # serve's stdout is the JSON-RPC channel: left untouched
        _tolerant(sys.stdout)
    if args.command == 'doctor':
        return doctor()
    if args.command == 'scan':
        return scan(args)
    if args.command == 'verify':
        return verify(args)
    if args.command == 'sources':
        return sources(args)
    if args.command == 'index':
        return index_command(args)
    if args.command == 'search':
        return search_command(args)
    if args.command == 'fetch-model':
        return fetch_model_command(args)
    if args.command == 'verify-model':
        return verify_model_command(args)
    from . import server  # imports the MCP SDK only when serving
    return server.main()

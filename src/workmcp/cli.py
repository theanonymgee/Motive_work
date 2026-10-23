"""Command line: ``workmcp doctor`` (human report) and ``workmcp serve`` (stdio MCP server).

A data-home rule violation stops every command before anything is created: the reason goes
to stderr, stdout stays empty and the exit code is 2.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import os
import sqlite3
import sys

from . import __version__, models, paths, store


def doctor() -> int:
    try:
        home = paths.resolve_home()
    except paths.DataPathError as exc:
        print(exc, file=sys.stderr)
        return 2
    db_file = paths.db_path(home)
    schema = None
    if db_file.is_file():
        db = sqlite3.connect(f'{db_file.as_uri()}?mode=ro', uri=True)
        try:
            schema = db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
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
        f'model {model["name"]}: {"available" if model["available"] else "unavailable"} - {model["message"]}',
    ]
    print('\n'.join(lines))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='workmcp')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor', help='check data home, Python, SQLite/FTS5, mcp and model files')
    sub.add_parser('serve', help='run the stdio MCP server')
    args = parser.parse_args(argv)
    if args.command == 'doctor':
        return doctor()
    from . import server  # imports the MCP SDK only when serving
    return server.main()

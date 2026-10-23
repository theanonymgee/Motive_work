"""MCP tool implementations as plain functions (dict in, dict out; design §4).

Every response is an envelope ``{"ok": true, "result", "delivered_sources", "notice"}`` or
``{"ok": false, "error": {"code", "message", "retryable"}}``. ``delivered_sources`` lists
only the sources whose text this response carries; ``status`` carries none.
"""
from __future__ import annotations

from pathlib import Path

from . import models, store


def ok(result, *, delivered_sources=(), notice=()) -> dict:
    return {'ok': True, 'result': result, 'delivered_sources': list(delivered_sources), 'notice': list(notice)}


def error(code: str, message: str, *, retryable: bool = False) -> dict:
    return {'ok': False, 'error': {'code': code, 'message': message, 'retryable': retryable}}


def status(home: Path) -> dict:
    """Data home, store counts, last finished scan, model availability and FTS5 support; no source text."""
    try:
        db = store.open_read(home)
    except FileNotFoundError as exc:
        return error('store_missing', str(exc))
    try:
        count = {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                 for table in ('documents', 'versions', 'chunks', 'chunk_vectors', 'deliveries')}
        schema = db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
        last_scan = store.last_scan(db)
    finally:
        db.close()
    result = {
        'home': str(home),
        'schema_version': schema,
        'db': {'documents': count['documents'], 'versions': count['versions'], 'chunks': count['chunks'],
               'vectors': count['chunk_vectors']},
        'model': models.quick_status(models.DEFAULT_MODEL, home),
        'fts5': store.fts5_available(),
        'last_scan': last_scan,
        'delivery_log_count': count['deliveries'],
    }
    return ok(result, notice=['아직 수집(scan)한 적이 없습니다.'] if result['last_scan'] is None else [])

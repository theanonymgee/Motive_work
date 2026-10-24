"""Test-only wrapper: install an audit hook, then run the package (design M0 §7.2, M1 §4.3).

Usage: ``python tests/_audited_server.py LOG_JSON [serve [ROOT]|imports|probe|scan ROOT|index ROOT|search ROOT|embed]``

- ``serve [ROOT]`` (default): ``workmcp.server.main()``; stdin/stdout are the MCP stdio transport. With
  ROOT (the source folder the test scanned into the data home beforehand), ROOT is the source root of
  the source-write judgement and every open under it is counted (``source_opens``, must be 0: the tools
  read the store, never the originals; design M3 §12).
- ``imports``: import every package module, build the server and call ``tools.status``.
- ``probe``: positive control. Tries a non-loopback connect, a name lookup, a process start and a
  fixed-port loopback listener (all blocked by the hook before they happen) and one loopback
  socket pair on port 0 (allowed); then,
  with a probe folder as the source root, a write and a delete inside it (both blocked).
- ``scan ROOT``: source-read audit with ROOT as the source root: ``config.validate_root`` ->
  ``collect.scan`` -> ``binding.verify`` -> a second ``collect.scan`` (must be all unchanged).
  Settings come from ``config.toml`` in the data home (the test sets a small ``max_file_mb`` so a
  large file goes through ``stream_sha256`` under the hook too).
  The report has counts only (per status/code and per parser), never document text.
- ``index ROOT`` (M2): one scan of ROOT, then ``index.run_index`` with ``HashEmbedder(1024)`` twice (the
  second must chunk and embed nothing). Source opens during the index phase are counted (must be 0:
  indexing reads the store, never the originals).
- ``search ROOT`` (M2): as ``index``, then ``search.Engine`` (``HashEmbedder`` factory) over six
  queries in each mode and ``maybe_unload``; every ``sqlite3.connect`` of the search phase is
  recorded (all must be ``mode=ro``) and source opens are counted (must be 0).
- ``embed`` (M2): the real model from the data home, only when it is there: ``mode_embed`` delegates to
  ``tests/_embed_mode.py`` ``run(home)``; otherwise ``skipped: model_missing``.
  A mode whose package function is still a stage-0 stub reports ``skipped`` (the test skips).

Runtime boundary: no socket connect/bind/send to an address other than loopback (127.0.0.1,
::1), no name resolution and no process creation. Loopback is allowed only because the
Windows asyncio event loop makes an internal socket pair for wake-ups, so a loopback ``bind``
must ask for port 0 (any free port, as ``socket.socketpair`` does): a fixed-port listener is a
local server another process could reach and is a violation (M0 review v2 N-L5). With a source root set,
also no write to anything under it: no ``open`` with a write mode or write flags (delete-on-close
``O_TEMPORARY`` counts), no remove, rename, utime, chmod, truncate, mkdir, rmdir, link or
``shutil`` call on a path under it, no SQLite database opened under it, and no such call on a
name relative to a directory descriptor (``dir_fd``; its folder is unknown, so it is refused).
Paths are compared after dropping a Win32 long-path or device prefix.
A violating event is recorded, then blocked (the hook raises), so it never happens. The event
log is written to LOG_JSON (never stdout, which belongs to JSON-RPC in ``serve`` mode).
"""
import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote  # string handling only; no network

LOOPBACK = frozenset({'127.0.0.1', '::1'})
ADDRESS_EVENTS = frozenset({'socket.connect', 'socket.bind', 'socket.sendto', 'socket.sendmsg'})
HARMLESS_SOCKET_EVENTS = frozenset({'socket.__new__', 'socket.gethostname'})
PROCESS_EVENTS = ('subprocess.', 'os.system', 'os.exec', 'os.spawn', 'os.posix_spawn', 'os.startfile', 'os.fork',
                  '_winapi.CreateProcess', 'pty.spawn')
NETWORK_EVENTS = ('http.client.', 'urllib.', 'webbrowser.', 'ftplib.', 'smtplib.', 'imaplib.', 'poplib.',
                  'telnetlib.', 'nntplib.')
WATCHED = ('socket.', *PROCESS_EVENTS, *NETWORK_EVENTS)
PROBE_ADDRESS = ('203.0.113.1', 9)  # TEST-NET-3 documentation address; never actually contacted
PROBE_LISTEN_PORT = 8765  # a fixed loopback port; the bind is blocked before it happens

# Source-write events (measured on this PC, design M1 §4.3): 'rb' arrives as mode 'r'; os.open has
# mode None and the flags; Path.touch/os.utime -> os.utime; os.replace -> os.rename; os.unlink -> os.remove.
WRITE_MODE_CHARS = frozenset('wax+')
# O_TEMPORARY (0x40, Windows) deletes the file when it is closed; O_SHORT_LIVED (0x1000) creates a
# temporary file. Both only exist on Windows.
O_TEMPORARY = getattr(os, 'O_TEMPORARY', 0)
O_SHORT_LIVED = getattr(os, 'O_SHORT_LIVED', 0)
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | O_TEMPORARY | O_SHORT_LIVED
SOURCE_WRITE_EVENTS = frozenset({'os.remove', 'os.unlink', 'os.rename', 'os.rmdir', 'os.mkdir', 'os.utime',
                                 'os.chmod', 'os.chown', 'os.chflags', 'os.truncate', 'os.link', 'os.symlink',
                                 'os.setxattr', 'os.removexattr'})
# (path position, dir_fd position) pairs in the audit events above (CPython posixmodule). The event
# carries dir_fd -1 when none was given; Windows has no dir_fd support at all.
DIR_FD_ARGS = {'os.remove': ((0, 1),), 'os.unlink': ((0, 1),), 'os.rmdir': ((0, 1),), 'os.mkdir': ((0, 2),),
               'os.rename': ((0, 2), (1, 3)), 'os.link': ((0, 2), (1, 3)), 'os.symlink': ((1, 2),),
               'os.utime': ((0, 3),), 'os.chmod': ((0, 2),), 'os.chown': ((0, 3),)}
LONG_PREFIXES = (r'\\?\UNC' + '\\', r'\\?' + '\\', r'\\.' + '\\')  # Win32 long-path and device prefixes


def is_loopback(address) -> bool:
    return isinstance(address, tuple) and len(address) >= 2 and address[0] in LOOPBACK


def judge(event: str, args: tuple) -> str | None:
    """None when the event is inside the runtime boundary, else the reason it is not."""
    if event in ADDRESS_EVENTS:
        address = args[1] if len(args) > 1 else None
        if event == 'socket.bind' and is_loopback(address) and address[1] != 0:
            return f'socket.bind loopback listener on fixed port {address[1]!r}'
        if is_loopback(address) or (event == 'socket.sendmsg' and address is None):  # sendmsg on a connected socket
            return None
        return f'{event} to non-loopback address {address!r}'
    if event.startswith('socket.'):
        return None if event in HARMLESS_SOCKET_EVENTS else f'{event} (name resolution or other socket call)'
    if event.startswith(PROCESS_EVENTS):
        return f'process creation {event}'
    if event.startswith(NETWORK_EVENTS):
        return f'network library {event}'
    return None


def _plain(text: str) -> str:
    r"""``text`` without a Win32 long-path or device prefix (``\\?\C:\x`` -> ``C:\x``)."""
    if os.name != 'nt':
        return text
    text = text.replace('/', '\\')
    for prefix in LONG_PREFIXES:
        if text[:len(prefix)].upper() == prefix.upper():
            return (r'\\' if 'UNC' in prefix else '') + text[len(prefix):]
    return text


def _key(path) -> str | None:
    """Normalised absolute form of a path argument; None for file descriptors and other values."""
    if isinstance(path, (str, bytes, os.PathLike)):
        try:
            return os.path.normcase(os.path.abspath(_plain(os.fsdecode(path))))
        except (TypeError, ValueError):
            return None
    return None


def _sqlite_path(database):
    """The file a ``sqlite3.connect`` argument names (``file:`` URIs too); None for ``:memory:``."""
    if isinstance(database, bytes):
        database = os.fsdecode(database)
    if isinstance(database, os.PathLike):
        database = os.fspath(database)
    if not isinstance(database, str) or database in ('', ':memory:'):
        return None
    if database.startswith('file:'):
        database = database[len('file:'):].split('?', 1)[0].split('#', 1)[0]
        if database.startswith('//'):
            database = database[2:]  # file:///C:/x -> /C:/x (empty authority)
        if os.name == 'nt' and len(database) > 2 and database[0] == '/' and database[2] == ':':
            database = database[1:]
        database = unquote(database)  # Path.as_uri() percent-encodes spaces and non-ASCII names
        if database in ('', ':memory:'):
            return None
    return database


def under(path, root) -> bool:
    """``path`` is ``root`` or inside it (case-insensitive on Windows, no link resolution)."""
    key, base = _key(path), _key(root)
    if key is None or base is None:
        return False
    return key == base or key.startswith(base if base.endswith(os.sep) else base + os.sep)


def is_source_event(event: str) -> bool:
    return event in ('open', 'sqlite3.connect') or event in SOURCE_WRITE_EVENTS or event.startswith('shutil.')


def judge_source_write(event: str, args: tuple, root) -> str | None:
    """None unless the event would write to, create, move, remove or re-time something under ``root``."""
    if event == 'open':
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        if not under(path, root):
            return None
        if isinstance(mode, str):
            return f'open {mode!r} under the source root' if WRITE_MODE_CHARS & set(mode) else None
        if mode is None and isinstance(flags, int) and flags & WRITE_FLAGS:
            return f'os.open flags {flags} under the source root'
        return None
    if event == 'sqlite3.connect':
        database = _sqlite_path(args[0] if args else None)
        return 'sqlite3.connect on a database under the source root' if under(database, root) else None
    if event in SOURCE_WRITE_EVENTS or event.startswith('shutil.'):
        if any(under(arg, root) for arg in args):
            return f'{event} on a path under the source root'
        for path_at, fd_at in DIR_FD_ARGS.get(event, ()):
            if fd_at >= len(args) or isinstance(args[fd_at], bool) or not isinstance(args[fd_at], int):
                continue
            name = args[path_at]
            if args[fd_at] >= 0 and isinstance(name, (str, bytes, os.PathLike)) and not os.path.isabs(os.fsdecode(name)):
                return f'{event} on a name relative to a directory descriptor (folder unknown)'
    return None


EVENTS: list[dict] = []
SOURCE_ROOT: list[str] = []  # set by scan/probe; empty means no source-write judgement
CONNECTS: list[str] = []     # sqlite3.connect targets while RECORD_CONNECTS holds an entry (search phase)
RECORD_CONNECTS: list[bool] = []


def _detail(args) -> str:
    try:
        return repr(args)[:300]
    except Exception:  # repr of a half-built object
        return '<unrepresentable>'


def hook(event, args):
    if RECORD_CONNECTS and event == 'sqlite3.connect':
        CONNECTS.append(str(args[0]) if args else '')
    if SOURCE_ROOT and is_source_event(event):
        root = SOURCE_ROOT[0]
        reason = judge_source_write(event, args, root)
        # Every open under the root is kept (proof that the audit saw the reads); other opens are not.
        if reason or (event == 'open' and args and under(args[0], root)):
            EVENTS.append({'event': event, 'detail': _detail(args), 'violation': reason, 'source': True})
        if reason:
            raise PermissionError(f'blocked by test audit hook: {reason}')
        return
    if not event.startswith(WATCHED):
        return
    reason = judge(event, args)
    EVENTS.append({'event': event, 'detail': _detail(args), 'violation': reason})
    if reason:
        raise PermissionError(f'blocked by test audit hook: {reason}')


def imports() -> dict:
    import importlib
    import pkgutil

    import workmcp
    names = [m.name for m in pkgutil.walk_packages(workmcp.__path__, 'workmcp.') if m.name != 'workmcp.__main__']
    for name in names:
        importlib.import_module(name)
    from workmcp import paths, server, store, tools
    home = paths.ensure_home(paths.resolve_home())
    store.open_home(home).close()
    server.build_server(home)
    assert tools.status(home)['ok']
    return {'modules': names, 'loaded': sorted(sys.modules)}


def probe() -> dict:
    import socket
    import sqlite3
    import subprocess

    blocked = {}

    def attempt(name, action):
        try:
            action()
            blocked[name] = False
        except PermissionError:
            blocked[name] = True

    def outside_connect():
        with socket.socket() as sock:
            sock.settimeout(0.5)
            sock.connect(PROBE_ADDRESS)

    attempt('connect', outside_connect)
    attempt('getaddrinfo', lambda: socket.getaddrinfo(PROBE_ADDRESS[0], PROBE_ADDRESS[1]))
    attempt('subprocess', lambda: subprocess.run([sys.executable, '-c', 'pass'], check=False))

    def loopback_pair():
        with socket.socket() as server, socket.socket() as client:
            server.bind(('127.0.0.1', 0))
            server.listen(1)
            client.connect(server.getsockname())

    attempt('loopback', loopback_pair)

    def loopback_listener():
        with socket.socket() as server:
            server.bind(('127.0.0.1', PROBE_LISTEN_PORT))

    attempt('loopback_listener', loopback_listener)

    source = Path.cwd()/'probe-source'
    source.mkdir(exist_ok=True)
    original = source/'original.txt'
    original.write_bytes(b'synthetic probe file\n')
    SOURCE_ROOT.append(str(source))
    try:
        attempt('source_read', original.read_bytes)
        attempt('source_write', lambda: original.write_bytes(b'changed'))
        attempt('source_create', lambda: (source/'new.txt').write_bytes(b'x'))
        attempt('source_remove', original.unlink)
        attempt('source_utime', lambda: os.utime(original))
        if O_TEMPORARY:  # read-only open that deletes the file on close (review M1 P12)
            attempt('source_open_temporary',
                    lambda: os.close(os.open(original, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | O_TEMPORARY)))
        attempt('source_sqlite', lambda: sqlite3.connect(source/'probe.db').close())
        attempt('source_sqlite_uri', lambda: sqlite3.connect(f'{(source/"probe.db").as_uri()}?mode=rwc', uri=True).close())
    finally:
        SOURCE_ROOT.clear()
    unchanged = (original.read_bytes() == b'synthetic probe file\n' and not (source/'new.txt').exists()
                 and not (source/'probe.db').exists())
    return {'blocked': blocked, 'source_unchanged': unchanged}


def scan(root: Path) -> dict:
    """``config.validate_root`` -> ``collect.scan`` -> ``binding.verify`` -> a second ``collect.scan``.

    The data home (``WORKMCP_HOME``) is outside ``root``, so store and lock writes are not source
    writes. Returns the two scans' totals and the binding summary (counts only, no text).
    """
    from workmcp import binding, collect, config, paths, store
    home = paths.ensure_home(paths.resolve_home())
    settings = config.load(home).scan
    path, warnings = config.validate_root(str(root), home, paths.find_repo_root())
    spec = config.RootSpec(path, str(root), warnings=warnings)
    lock, _ = store.acquire_scan_lock(home)
    try:
        db = store.open_home(home)
        try:
            ids = config.sync_roots(db, [spec], from_config=False)
            first = collect.scan(db, [(ids[spec.key], spec)], settings, partial=True)
            report = binding.verify(db, limits=settings.limits)
            second = collect.scan(db, [(ids[spec.key], spec)], settings, partial=True)
            statuses = dict(db.execute("SELECT parse_status || COALESCE('/' || parse_error, ''), COUNT(*) "
                                       "FROM versions GROUP BY 1").fetchall())
            by_parser = {}
            for parser, key, files, units in db.execute(
                    "SELECT parser, parse_status || COALESCE('/' || parse_error, ''), COUNT(*), SUM(unit_count) "
                    'FROM versions GROUP BY 1, 2'):
                entry = by_parser.setdefault(parser, {'files': 0, 'units': 0, 'statuses': {}})
                entry['files'] += files
                entry['units'] += units
                entry['statuses'][key] = files
        finally:
            db.close()
    finally:
        store.release_scan_lock(lock)
    return {'root': str(root), 'first': first['total'], 'second': second['total'], 'statuses': statuses,
            'by_parser': by_parser,
            'verify': {'ok': report['ok'], 'versions': report['versions'], 'units': report['units']}}


NOT_IMPLEMENTED = 'not_implemented (M2 stage 1)'
QUERY_COUNT = 6


def _collect_once(root: Path):
    """One scan of ``root`` into the data home (store and lock writes are outside ``root``)."""
    from workmcp import collect, config, paths, store
    home = paths.ensure_home(paths.resolve_home())
    path, warnings = config.validate_root(str(root), home, paths.find_repo_root())
    spec = config.RootSpec(path, str(root), warnings=warnings)
    lock, _ = store.acquire_scan_lock(home)
    try:
        db = store.open_home(home)
        try:
            ids = config.sync_roots(db, [spec], from_config=False)
            total = collect.scan(db, [(ids[spec.key], spec)], config.load(home).scan, partial=True)['total']
        finally:
            db.close()
    finally:
        store.release_scan_lock(lock)
    return home, total


def _source_opens(since: int) -> int:
    return sum(1 for e in EVENTS[since:] if e.get('source'))


def index(root: Path) -> dict:
    """``_collect_once`` -> ``index.run_index(HashEmbedder(1024))`` twice; counts only."""
    from workmcp import embedder, index as index_module, store
    home, scanned = _collect_once(root)
    mark = len(EVENTS)
    lock, _ = store.acquire_lock(home, store.INDEX_LOCK_NAME)
    try:
        db = store.open_home(home)
        try:
            vectors = embedder.HashEmbedder(1024)
            try:
                first = index_module.run_index(db, home, embedder=vectors)
            except NotImplementedError:
                return {'skipped': NOT_IMPLEMENTED, 'scanned': scanned}
            second = index_module.run_index(db, home, embedder=vectors)
            chunks = db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
            fts_rows = store.fts_rows(db) if store.fts_available(db) else None
        finally:
            db.close()
    finally:
        store.release_lock(lock)
    keys = ('chunked', 'embedded', 'reused_chunks', 'reused_vectors', 'dropped_versions', 'chunks_total')
    return {'scanned': scanned, 'first': {k: first[k] for k in keys}, 'second': {k: second[k] for k in keys},
            'chunks': chunks, 'fts_rows': fts_rows, 'source_opens_during_index': _source_opens(mark)}


def search(root: Path) -> dict:
    """``index`` then ``search.Engine`` with a ``HashEmbedder`` factory: 6 queries x 3 modes; counts only."""
    from fixtures import LINES_EN, LINES_KO
    from workmcp import config, embedder, paths, search as search_module
    report = index(root)
    if report.get('skipped'):
        return report
    home = paths.resolve_home()
    queries = [line[:12] for line in (*LINES_KO[:3], *LINES_EN[:3])][:QUERY_COUNT]
    engine = search_module.Engine(home, config.SearchSettings(), embedder_factory=lambda: embedder.HashEmbedder(1024))
    mark = len(EVENTS)
    RECORD_CONNECTS.append(True)
    hits = 0
    try:
        for query in queries:
            for mode in search_module.MODES:
                hits += len(engine.search(query, mode=mode)['hits'])
        released = engine.maybe_unload(now=float('inf'))
    except NotImplementedError:
        return {**report, 'skipped': NOT_IMPLEMENTED}
    finally:
        RECORD_CONNECTS.clear()
    return {**report, 'hits': hits, 'released': released, 'connects': list(CONNECTS),
            'source_opens_during_search': _source_opens(mark)}


def mode_embed(home: Path) -> dict:
    """Delegates to ``tests/_embed_mode.py`` ``run(home)`` (B's file: load ``OnnxEmbedder``, embed three
    texts and one query, return counts and providers only). Without that file: ``model_missing``."""
    if not (Path(__file__).resolve().parent/'_embed_mode.py').is_file():
        return {'skipped': 'model_missing'}
    import _embed_mode
    return _embed_mode.run(home)


def embed() -> dict:
    from workmcp import models, paths
    home = paths.resolve_home()  # read only: the test may point at the user's data home (review M2-L4)
    if not models.available(models.DEFAULT_MODEL, home):
        return {'skipped': 'model_missing'}
    try:
        return mode_embed(home)
    except NotImplementedError:
        return {'skipped': NOT_IMPLEMENTED}


def main(argv) -> int:
    log, mode = Path(argv[1]), (argv[2] if len(argv) > 2 else 'serve')
    sys.addaudithook(hook)
    report, code = {'mode': mode, 'completed': False}, 1
    try:
        if mode == 'serve':
            from workmcp import server
            if len(argv) > 3:
                SOURCE_ROOT.append(str(Path(argv[3]).resolve()))
            code = server.main()
            report['source_opens'] = _source_opens(0)
        elif mode == 'imports':
            report.update(imports())
            code = 0
        elif mode == 'probe':
            report.update(probe())
            code = 0
        elif mode in ('scan', 'index', 'search'):
            root = Path(argv[3]).resolve()
            SOURCE_ROOT.append(str(root))
            report.update({'scan': scan, 'index': index, 'search': search}[mode](root))
            code = 0
        elif mode == 'embed':
            report.update(embed())
            code = 0
        else:
            raise SystemExit(f'unknown mode {mode!r}')
        report['completed'] = True
    finally:
        SOURCE_ROOT.clear()  # writing the log must not be judged as a source write
        report['events'] = EVENTS
        log.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv))

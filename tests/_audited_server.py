"""Test-only wrapper: install an audit hook, then run the package (design M0 §7.2, M1 §4.3).

Usage: ``python tests/_audited_server.py LOG_JSON [serve|imports|probe|scan ROOT]``

- ``serve`` (default): ``workmcp.server.main()``; stdin/stdout are the MCP stdio transport.
- ``imports``: import every package module, build the server and call ``tools.status``.
- ``probe``: positive control. Tries a non-loopback connect, a name lookup, a process start and a
  fixed-port loopback listener (all blocked by the hook before they happen) and one loopback
  socket pair on port 0 (allowed); then,
  with a probe folder as the source root, a write and a delete inside it (both blocked).
- ``scan ROOT``: source-read audit with ROOT as the source root: ``config.validate_root`` ->
  ``collect.scan`` -> ``binding.verify`` -> a second ``collect.scan`` (must be all unchanged).
  The report has counts only (per status/code and per parser), never document text.

Runtime boundary: no socket connect/bind/send to an address other than loopback (127.0.0.1,
::1), no name resolution and no process creation. Loopback is allowed only because the
Windows asyncio event loop makes an internal socket pair for wake-ups, so a loopback ``bind``
must ask for port 0 (any free port, as ``socket.socketpair`` does): a fixed-port listener is a
local server another process could reach and is a violation (M0 review v2 N-L5). With a source root set,
also no write to anything under it: no ``open`` with a write mode or write flags, no remove,
rename, utime, chmod, truncate, mkdir, rmdir, link or ``shutil`` call on a path under it.
A violating event is recorded, then blocked (the hook raises), so it never happens. The event
log is written to LOG_JSON (never stdout, which belongs to JSON-RPC in ``serve`` mode).
"""
import json
import os
import sys
from pathlib import Path

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
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
SOURCE_WRITE_EVENTS = frozenset({'os.remove', 'os.unlink', 'os.rename', 'os.rmdir', 'os.mkdir', 'os.utime',
                                 'os.chmod', 'os.chown', 'os.chflags', 'os.truncate', 'os.link', 'os.symlink',
                                 'os.setxattr', 'os.removexattr'})


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


def _key(path) -> str | None:
    """Normalised absolute form of a path argument; None for file descriptors and other values."""
    if isinstance(path, (str, bytes, os.PathLike)):
        try:
            return os.path.normcase(os.path.abspath(os.fsdecode(path)))
        except (TypeError, ValueError):
            return None
    return None


def under(path, root) -> bool:
    """``path`` is ``root`` or inside it (case-insensitive on Windows, no link resolution)."""
    key, base = _key(path), _key(root)
    if key is None or base is None:
        return False
    return key == base or key.startswith(base if base.endswith(os.sep) else base + os.sep)


def is_source_event(event: str) -> bool:
    return event == 'open' or event in SOURCE_WRITE_EVENTS or event.startswith('shutil.')


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
    if event in SOURCE_WRITE_EVENTS or event.startswith('shutil.'):
        if any(under(arg, root) for arg in args):
            return f'{event} on a path under the source root'
    return None


EVENTS: list[dict] = []
SOURCE_ROOT: list[str] = []  # set by scan/probe; empty means no source-write judgement


def _detail(args) -> str:
    try:
        return repr(args)[:300]
    except Exception:  # repr of a half-built object
        return '<unrepresentable>'


def hook(event, args):
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
    finally:
        SOURCE_ROOT.clear()
    unchanged = original.read_bytes() == b'synthetic probe file\n' and not (source/'new.txt').exists()
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


def main(argv) -> int:
    log, mode = Path(argv[1]), (argv[2] if len(argv) > 2 else 'serve')
    sys.addaudithook(hook)
    report, code = {'mode': mode, 'completed': False}, 1
    try:
        if mode == 'serve':
            from workmcp import server
            code = server.main()
        elif mode == 'imports':
            report.update(imports())
            code = 0
        elif mode == 'probe':
            report.update(probe())
            code = 0
        elif mode == 'scan':
            root = Path(argv[3]).resolve()
            SOURCE_ROOT.append(str(root))
            report.update(scan(root))
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

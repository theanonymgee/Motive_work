"""Test-only wrapper: install an audit hook, then run the package (design M0 §7.2, M1 §4.3).

Usage: ``python tests/_audited_server.py LOG_JSON [serve|imports|probe|scan ROOT]``

- ``serve`` (default): ``workmcp.server.main()``; stdin/stdout are the MCP stdio transport.
- ``imports``: import every package module, build the server and call ``tools.status``.
- ``probe``: positive control. Tries a non-loopback connect, a name lookup and a process start
  (all blocked by the hook before they happen) and one loopback socket pair (allowed); then,
  with a probe folder as the source root, a write and a delete inside it (both blocked).
- ``scan ROOT``: source-read audit with ROOT as the source root. Stage-0 body (the collector
  does not exist yet): read every file under ROOT once and pass it to ``parsers.parse``. M1
  stage 1 replaces the body with ``config.validate_root`` -> ``collect.scan`` ->
  ``binding.verify`` -> a second ``collect.scan``; the hook and the judgement stay as they are.

Runtime boundary: no socket connect/bind/send to an address other than loopback (127.0.0.1,
::1), no name resolution and no process creation. Loopback is allowed only because the
Windows asyncio event loop makes an internal socket pair for wake-ups. With a source root set,
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
    """Stage-0 body: read each file under ``root`` once ('rb') and dispatch it to the parsers."""
    from workmcp import parsers
    statuses: dict[str, int] = {}
    files = 0
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.suffix.lower() not in parsers.SUPPORTED_EXTENSIONS:
            continue
        with open(path, 'rb') as handle:
            data = handle.read()
        result = parsers.parse(data, path.suffix, parsers.Limits())
        key = f'{result.status}/{result.error}' if result.error else result.status
        statuses[key] = statuses.get(key, 0) + 1
        files += 1
    return {'root': str(root), 'files': files, 'statuses': statuses}


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

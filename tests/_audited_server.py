"""Test-only wrapper: install an audit hook, then run the package (design §7.2 runtime layer).

Usage: ``python tests/_audited_server.py LOG_JSON [serve|imports|probe]``

- ``serve`` (default): ``workmcp.server.main()``; stdin/stdout are the MCP stdio transport.
- ``imports``: import every package module, build the server and call ``tools.status``.
- ``probe``: positive control. Tries a non-loopback connect, a name lookup and a process start
  (all blocked by the hook before they happen) and one loopback socket pair (allowed).

Runtime boundary: no socket connect/bind/send to an address other than loopback (127.0.0.1,
::1), no name resolution and no process creation. Loopback is allowed only because the
Windows asyncio event loop makes an internal socket pair for wake-ups. A violating event is
recorded, then blocked (the hook raises), so it never reaches the network. The event log is
written to LOG_JSON (never stdout, which belongs to JSON-RPC in ``serve`` mode).
"""
import json
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


EVENTS: list[dict] = []


def hook(event, args):
    if not event.startswith(WATCHED):
        return
    reason = judge(event, args)
    try:
        detail = repr(args)[:300]
    except Exception:  # repr of a half-built object
        detail = '<unrepresentable>'
    EVENTS.append({'event': event, 'detail': detail, 'violation': reason})
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
    return {'modules': names}


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
    return {'blocked': blocked}


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
        else:
            raise SystemExit(f'unknown mode {mode!r}')
        report['completed'] = True
    finally:
        report['events'] = EVENTS
        log.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv))

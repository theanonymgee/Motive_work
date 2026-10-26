"""Audit mode ``push`` body (design M5 remote §6.3), run by ``_audited_server.py LOG push PORT ROOT``.

The data home (``WORKMCP_HOME``) is a collector that the test scanned and indexed beforehand from ROOT, with
``[push] url = "http://127.0.0.1:PORT/workmcp"`` and a ``push.token`` file; the test runs a loopback server
on PORT. Under the wrapper's audit hook this module:

- narrows the hook's judgement (``set_judge``) to ``judge_push`` with the configured server as the ``PushTarget`` (name
  lookup, connects, ``http.client`` connections and ``urllib`` requests to that server only; everything else
  as the default judge, so any other destination, name lookup or process is a violation and is blocked);
- sets ROOT as the source root (every open under it is counted: ``source_opens`` must be 0) and records every
  ``sqlite3.connect`` target (all must be the collector store with ``mode=ro``);
- runs ``workmcp.cli.main(['push', '--allow-insecure-loopback'])`` twice (the second must send nothing) and
  returns exit codes, the report counts of both runs, the connect targets and the set of destinations.

The token is scrubbed from the recorded event details (``http.client.send`` carries the request header block).
Counts and addresses only; no unit text is returned.
"""
import contextlib
import io
import sys
from pathlib import Path

# Events judge_push decides itself; every other event goes to the wrapper's default judge.
PUSH_EVENTS = frozenset({'socket.getaddrinfo', 'socket.connect', 'http.client.connect', 'http.client.send',
                         'urllib.Request'})
REPORT_COUNTS = ('exit_code', 'up_to_date', 'batches', 'bytes_sent', 'state_requests', 'retries', 'converged')


def run(argv) -> dict:
    if len(argv) < 2:
        return {'skipped': 'not_implemented without a target (usage: push PORT ROOT; tests/test_push.py runs it)'}
    wrapper = sys.modules['__main__']
    port, root = int(argv[0]), str(Path(argv[1]).resolve())
    from workmcp import cli, config, paths, push, sync
    home = paths.resolve_home()
    url = config.load(home).push.url
    parts = sync.parse_url(url)
    if parts.port != port or parts.host not in wrapper.LOOPBACK:
        return {'skipped': f'[push] url must be a numeric loopback address on port {port}'}
    target = wrapper.PushTarget(parts.host, parts.port, frozenset({parts.host}), url.rstrip('/') + '/')
    token = paths.read_token(home)
    default = wrapper.judge

    destinations = set()

    def judged(event, args):
        if event == 'socket.connect' and len(args) > 1 and isinstance(args[1], tuple):
            destinations.add(f'{args[1][0]}:{args[1][1]}')
        elif event == 'http.client.connect' and len(args) > 2:
            destinations.add(f'{args[1]}:{args[2]}')
        elif event == 'urllib.Request' and args and isinstance(args[0], str):
            requested = sync.parse_url(args[0].split('?', 1)[0])
            destinations.add(f'{requested.host}:{requested.port}')
        return wrapper.judge_push(event, args, target) if event in PUSH_EVENTS else default(event, args)

    reports = []
    real_execute = push.execute

    def execute(*args, **kwargs):
        reports.append(real_execute(*args, **kwargs))
        return reports[-1]

    mark = len(wrapper.EVENTS)
    install = getattr(wrapper, 'set_judge', None)
    if install is not None:
        install(judged)
    else:
        wrapper.judge = judged
    wrapper.SOURCE_ROOT.append(root)
    wrapper.RECORD_CONNECTS.append(True)
    push.execute = execute
    codes = []
    try:
        for _ in range(2):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                codes.append(cli.main(['push', '--allow-insecure-loopback']))
    finally:
        push.execute = real_execute
        wrapper.RECORD_CONNECTS.clear()
        wrapper.SOURCE_ROOT.clear()
        if install is not None:
            wrapper.JUDGE.clear()
        else:
            wrapper.judge = default
    for event in wrapper.EVENTS[mark:]:
        event['detail'] = event['detail'].replace(token, '<token>')
    return {'exit_codes': codes, 'source_opens': wrapper._source_opens(mark), 'connects': list(wrapper.CONNECTS),
            'destinations': sorted(destinations),
            'first': {key: reports[0][key] for key in REPORT_COUNTS} if reports else None,
            'second': {key: reports[1][key] for key in REPORT_COUNTS} if len(reports) > 1 else None}

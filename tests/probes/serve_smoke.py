"""Stdio smoke check of the real ``serve`` the way a client starts it (design M3 §11.4). A manual script,
not a test module (its name does not match ``test_*.py``).

    uv run python tests/probes/serve_smoke.py [--config .mcp.json] [--query TEXT] [--mode hybrid]

Reads ``command``, ``args`` and ``env`` of the ``workmcp`` entry from the project ``.mcp.json`` (git-ignored,
made from ``doc/examples/mcp.json.example``), starts that process here in test code (the package never
starts one), sends ``initialize``, ``tools/list``, ``status``, ``search`` and ``get_snippet`` (first hit) on
stdin and prints a summary: tool names, response lengths in characters, the data home, model and engine
state, hit count, delivered source count and the elapsed time per reply. Document text is not printed
(the data home is synthetic anyway). Nothing leaves the PC.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUERY = '합성 프로젝트 주간 회의 일정'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', default=str(ROOT/'.mcp.json'))
    parser.add_argument('--query', default=DEFAULT_QUERY)
    parser.add_argument('--mode', default='hybrid')
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args(argv)
    entry = json.loads(Path(args.config).read_text(encoding='utf-8'))['mcpServers']['workmcp']
    env = dict(os.environ)  # as a client passes it, PYTHONPATH included: the config's -I must cope (review M3-4)
    env.update(entry.get('env', {}))
    proc = subprocess.Popen([entry['command'], *entry['args']], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, cwd=ROOT, text=True, encoding='utf-8')
    watchdog = threading.Timer(args.timeout, proc.kill)
    watchdog.start()
    started = time.perf_counter()

    def send(message):
        proc.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        proc.stdin.flush()

    def reply(number):
        while True:
            line = proc.stdout.readline()
            if not line:
                raise SystemExit(f'server closed stdout; stderr:\n{proc.stderr.read()}')
            message = json.loads(line)
            if message.get('id') == number:
                return message, len(line.rstrip('\n'))

    def tool(number, name, arguments):
        began = time.perf_counter()
        send({'jsonrpc': '2.0', 'id': number, 'method': 'tools/call',
              'params': {'name': name, 'arguments': arguments}})
        message, size = reply(number)
        text = message['result']['content'][0]['text']
        return json.loads(text), len(text), size, round(time.perf_counter() - began, 2)

    try:
        send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
              'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                         'clientInfo': {'name': 'serve-smoke', 'version': '0'}}})
        init, _ = reply(1)
        send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        listed, _ = reply(2)
        print('server:', init['result']['serverInfo'])
        print('tools:', [t['name'] for t in listed['result']['tools']])
        status, chars, line, seconds = tool(3, 'status', {})
        result = status['result']
        print(f"status: {chars} chars ({seconds} s); home {result['home']}; schema v{result['schema_version']}; "
              f"db {result['db']}; model available {result['model']['available']}; "
              f"engine loaded {result['engine']['loaded']}; virtualization {result['home_virtualization']}")
        for number, (query, mode) in enumerate(((args.query, args.mode), (args.query, 'semantic'),
                                                (args.query, args.mode)), 4):
            found, chars, line, seconds = tool(number, 'search', {'query': query, 'mode': mode})
            meta = found['result']['meta']
            print(f"search #{number} mode {mode}: {chars} chars (line {line}) in {seconds} s; ran {meta['mode']}; "
                  f"hits {len(found['result']['hits'])}; delivered {len(found['delivered_sources'])}; "
                  f"semantic_note {meta['semantic_note']}; engine {meta['engine']}; signals {meta['signals']}; "
                  f"truncated_hits {meta['truncated_hits']}; notice {len(found['notice'])}")
        hit = found['result']['hits'][0]
        piece, chars, line, seconds = tool(7, 'get_snippet', {'chunk_id': hit['chunk_id'], 'context_units': 2})
        print(f"get_snippet: {chars} chars in {seconds} s; ok {piece['ok']}; pieces "
              f"{len(piece['result']['pieces'])} + context {len(piece['result']['context_before'])}/"
              f"{len(piece['result']['context_after'])}; delivered {len(piece['delivered_sources'])}")
        proc.stdin.close()
        code = proc.wait(timeout=30)
        stderr = proc.stderr.read()
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
    print(f'exit {code} after {round(time.perf_counter() - started, 1)} s; stderr lines:')
    for line in stderr.splitlines():
        print('  ', line)
    return code


if __name__ == '__main__':
    sys.exit(main())

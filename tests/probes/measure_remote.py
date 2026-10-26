"""Operating server measurement on this PC (design M5 remote §7, V-M1..V-M3, V-L1..V-L6). A script, not a test
module (its name does not match ``test_*.py``).

    uv run python tests/probes/measure_remote.py [--embedder hash|real] [--work DIR] [--out FILE] [--rounds 2]

Steps, all on loopback with temporary homes (``--work``, default under %TEMP%, outside the repository):
a server home with one mcp and one push token -> a collector home with the search corpus and the synthetic
bundle, scanned and indexed (``HashEmbedder(1024)``, or the manifest model with ``--embedder real``) ->
``workmcp serve --http`` started (test launcher of ``tests/test_remote.py``) -> server memory (working set,
private bytes, peaks: ``K32GetProcessMemoryInfo`` on its process) at start -> first ``workmcp push`` (time,
batches, bytes, the server's ``push_batches`` sizes by kind) -> memory after the push -> second push (nothing
to send) -> through the SDK client: the corpus queries in lexical mode (warm, ``rounds`` times), the first
hybrid query (model load when the server has the model) -> memory -> the hybrid queries warm. Latencies are
client-side (HTTP round trip) and the server's ``meta.elapsed_ms.total``.

``--embedder real``: the collector embeds with the model of ``tests/_real_model``'s home (read in place) and the
server home gets the model files as hard links into its own ``models/`` (no copy, nothing written to the source
home; without a model or when a hard link is not possible the run stays ``hash``). Offline: only loopback.
"""
import argparse
import ctypes
import json
import os
import statistics
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))

import anyio  # noqa: E402

import _real_model  # noqa: E402
import test_e2e_remote as e2e  # noqa: E402
import test_remote as tr  # noqa: E402
from fixtures import bundle, corpus  # noqa: E402
from workmcp import embedder, index, models, paths, store  # noqa: E402


class Counters(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD), ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t), ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t), ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t), ('PagefileUsage', ctypes.c_size_t),
                ('PeakPagefileUsage', ctypes.c_size_t), ('PrivateUsage', ctypes.c_size_t)]


def memory(pid: int) -> dict:
    """Working set, peak working set, private bytes and peak commit of process ``pid`` in MiB (Windows)."""
    kernel32, psapi = ctypes.WinDLL('kernel32'), ctypes.WinDLL('psapi')
    kernel32.OpenProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)    # QUERY_LIMITED_INFORMATION | VM_READ
    if not handle:
        raise OSError(f'OpenProcess {pid} failed')
    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise OSError('GetProcessMemoryInfo failed')
    finally:
        kernel32.CloseHandle(handle)
    mib = 2**20
    return {'working_set': round(counters.WorkingSetSize / mib, 1), 'peak_working_set':
            round(counters.PeakWorkingSetSize / mib, 1), 'private': round(counters.PrivateUsage / mib, 1),
            'peak_commit': round(counters.PeakPagefileUsage / mib, 1)}


def link_model(source_home: Path, target_home: Path) -> bool:
    """The manifest model files of ``source_home`` as hard links under ``target_home`` (no copy)."""
    source = models.model_dir(models.DEFAULT_MODEL, source_home)
    target = models.model_dir(models.DEFAULT_MODEL, target_home)
    target.mkdir(parents=True, exist_ok=True)
    try:
        for path in source.iterdir():
            if path.is_file():
                os.link(path, target/path.name)
    except OSError as exc:
        print(f'no hard link ({type(exc).__name__}): the server stays without a model', file=sys.stderr)
        return False
    return models.available(models.DEFAULT_MODEL, target_home)


def stats(values: list[float]) -> dict:
    ordered = sorted(values)
    return {'n': len(values), 'p50': round(statistics.median(ordered), 1),
            'p95': round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1), 'max': round(ordered[-1], 1)}


def batch_sizes(server_home: Path) -> dict:
    db = store.open_read(server_home)
    try:
        rows = db.execute('SELECT kind, COUNT(*), SUM(bytes_in), MAX(bytes_in) FROM push_batches GROUP BY kind').fetchall()
        sizes = {kind: {'batches': n, 'bytes': total, 'max_bytes': largest} for kind, n, total, largest in rows}
        sizes['counts'] = {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                           for table in ('documents', 'versions', 'units', 'chunks', 'chunk_vectors')}
        sizes['unit_chars'] = db.execute('SELECT SUM(LENGTH(text)) FROM units').fetchone()[0]
    finally:
        db.close()
    return sizes


def server_pid(server, token: str) -> int:
    async def ask(client):
        return (await client.call('status'))['result']['process']['pid']

    async def bounded():
        with anyio.fail_after(60):
            return await e2e._session(f'{server.base}/mcp', token, ask)
    return anyio.run(bounded)[0]


def write_bulk(root: Path, mib: int) -> int:
    """``mib`` MiB of synthetic Korean/English text lines in 256 KiB text files (push throughput)."""
    root.mkdir(parents=True, exist_ok=True)
    line = '합성 대량 문장 {n:07d}: 가상 거래처와 일정 검토, synthetic bulk line for throughput. '
    files, target, n = 0, mib * 2**20, 0
    written = 0
    while written < target:
        text = '\n'.join(line.format(n=n + i) for i in range(2_600))
        n += 2_600
        data = text.encode('utf-8')
        (root/f'bulk_{files:04d}.txt').write_bytes(data)
        written += len(data)
        files += 1
    return files


def run(work: Path, use_real: bool, rounds: int, bulk_mib: int = 0) -> dict:
    server_home, tokens, _ = e2e.setup_server(work)
    model_home = _real_model.real_model_home() if use_real else None
    real = bool(model_home) and link_model(model_home, server_home)
    src = work/'collector-src'
    built = corpus.build()
    corpus.write(built, src/'corpus')
    bundle.write_bundle(src/'bundle')
    bulk_files = write_bulk(src/'bulk', bulk_mib) if bulk_mib else 0
    server = tr.start_server(server_home, tokens['mcp'])
    result = {'embedder': 'real' if real else 'hash', 'cpu_count': os.cpu_count(), 'bulk_mib': bulk_mib,
              'bulk_files': bulk_files}
    try:
        col = e2e.Collector(work, server.port, src=src, bundle=False)
        db = store.open_home(col.home)
        try:
            began = time.perf_counter()
            e2e.scan(db, col.root)
            vectors = (embedder.OnnxEmbedder(models.DEFAULT_MODEL, model_home) if real else embedder.HashEmbedder(1024))
            index.run_index(db, col.home, embedder=vectors)
            result['collect_seconds'] = round(time.perf_counter() - began, 1)
        finally:
            db.close()
        pid = server_pid(server, tokens['mcp'])     # the interpreter, not the venv launcher in front of it
        result['memory_start'] = memory(pid)
        began = time.perf_counter()
        first = e2e.pushed(col.push(tokens['push']))
        result['push_first'] = {'seconds': round(time.perf_counter() - began, 2), **e2e.counts(first),
                                'bytes_sent': int(first.split('bytes sent ')[1].split(' ')[0].replace(',', ''))}
        result['memory_after_push'] = memory(pid)
        began = time.perf_counter()
        second = e2e.pushed(col.push(tokens['push']))
        assert 'up to date' in second
        result['push_second'] = {'seconds': round(time.perf_counter() - began, 2), 'report': second.strip()}
        result['batches'] = batch_sizes(server_home)
        queries = [query.text for query in corpus.queries(built)][:60]

        async def work_(client):
            out = {'lexical_ms': [], 'lexical_server_ms': [], 'hybrid_ms': [], 'hybrid_server_ms': []}
            for _ in range(rounds):
                for text in queries:
                    began = time.perf_counter()
                    envelope = await client.call('search', query=text, mode='lexical', limit=10)
                    out['lexical_ms'].append((time.perf_counter() - began) * 1000)
                    out['lexical_server_ms'].append(envelope['result']['meta']['elapsed_ms']['total'])
            began = time.perf_counter()
            envelope = await client.call('search', query=queries[0], mode='hybrid', limit=10)
            out['first_hybrid_ms'] = round((time.perf_counter() - began) * 1000, 1)
            out['first_hybrid_engine'] = envelope['result']['meta'].get('engine')
            out['first_hybrid_mode'] = envelope['result']['meta']['mode']
            out['memory_after_first_hybrid'] = memory(pid)
            for _ in range(rounds):
                for text in queries:
                    began = time.perf_counter()
                    envelope = await client.call('search', query=text, mode='hybrid', limit=10)
                    out['hybrid_ms'].append((time.perf_counter() - began) * 1000)
                    out['hybrid_server_ms'].append(envelope['result']['meta']['elapsed_ms']['total'])
            status = await client.call('status')
            out['engine'] = status['result']['engine']
            return out

        async def bounded():
            with anyio.fail_after(1800):
                return await e2e._session(f'{server.base}/mcp', tokens['mcp'], work_)
        out, _ = anyio.run(bounded)
        for key in ('lexical_ms', 'lexical_server_ms', 'hybrid_ms', 'hybrid_server_ms'):
            out[key] = stats(out[key])
        result['queries'] = out
        result['memory_end'] = memory(pid)
    finally:
        code, stderr = server.stop()
    result['server_exit'] = code
    assert all(token not in json.dumps(result) for token in tokens.values())
    return result


def table(result: dict) -> str:
    q, p, b = result['queries'], result['push_first'], result['batches']
    mb = (p['bytes_sent'] / 2**20) / p['seconds'] if p['seconds'] else 0
    lines = [f"embedder {result['embedder']}  cpu {result['cpu_count']}  bulk {result['bulk_mib']} MiB "
             f"({result['bulk_files']} files)  collect (scan+index) {result['collect_seconds']} s",
             '| 항목 | 이 PC 측정 |', '|---|---|',
             f"| 서버 상주 메모리, 시작(모델 해제) | WS {result['memory_start']['working_set']} MiB, private "
             f"{result['memory_start']['private']} MiB |",
             f"| push 적용 뒤 | WS {result['memory_after_push']['working_set']} MiB (peak "
             f"{result['memory_after_push']['peak_working_set']}), private {result['memory_after_push']['private']} MiB |",
             f"| 첫 혼합 질의 뒤(모델 {'로드' if result['embedder'] == 'real' else '없음: 어휘 대체'}) | WS "
             f"{q['memory_after_first_hybrid']['working_set']} MiB, private {q['memory_after_first_hybrid']['private']} MiB |",
             f"| 끝 | WS {result['memory_end']['working_set']} MiB (peak {result['memory_end']['peak_working_set']}), "
             f"private {result['memory_end']['private']} MiB |",
             f"| 첫 push | {p['seconds']} s, 문서 {p.get('documents')}, 단위 {p.get('units')}, 벡터 {p.get('vectors')}, "
             f"배치 {p.get('batches')}, {p['bytes_sent']:,} B gzip ({mb:.2f} MiB/s) |",
             f"| 두 번째 push(변경 없음) | {result['push_second']['seconds']} s, 배치 0, 0 B |",
             '| 배치 크기(종류별 수 / 최대 바이트) | ' + ', '.join(
                 f"{kind} {v['batches']}/{v['max_bytes']:,}" for kind, v in b.items() if kind not in ('counts', 'unit_chars'))
             + ' |',
             f"| 서버 저장 | {b['counts']} 단위 글자 {b['unit_chars']:,} |",
             f"| 어휘 질의(따뜻함) | 클라이언트 p50 {q['lexical_ms']['p50']} / p95 {q['lexical_ms']['p95']} ms, 서버 p50 "
             f"{q['lexical_server_ms']['p50']} ms (n {q['lexical_ms']['n']}) |",
             f"| 첫 혼합 질의 | {q['first_hybrid_ms']} ms, mode {q['first_hybrid_mode']}, engine {q['first_hybrid_engine']} |",
             f"| 혼합 질의(따뜻함) | 클라이언트 p50 {q['hybrid_ms']['p50']} / p95 {q['hybrid_ms']['p95']} ms, 서버 p50 "
             f"{q['hybrid_server_ms']['p50']} / p95 {q['hybrid_server_ms']['p95']} ms |"]
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--embedder', choices=('hash', 'real'), default='hash')
    parser.add_argument('--work', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--bulk-mib', type=int, default=0, help='add N MiB of synthetic text (hash runs)')
    args = parser.parse_args(argv)
    work = args.work or Path(tempfile.mkdtemp(prefix='workmcp-measure-remote-'))
    work.mkdir(parents=True, exist_ok=True)
    if paths.find_repo_root() and str(work.resolve()).startswith(str(paths.find_repo_root())):
        raise SystemExit('--work must be outside the repository')
    result = run(work, args.embedder == 'real', args.rounds, args.bulk_mib)
    out = args.out or work/'measure-remote.json'
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    print(table(result))
    print(f'json: {out}')
    try:
        tr.LAUNCHED.clear()
    except AttributeError:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())

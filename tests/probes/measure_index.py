"""Index and scale probes P4-P7, P10 (design M2 §6.2 I1-I2, S1-S2, S5, M2; §6.3). A script, not a test
module: its name does not match ``test_*.py``, so the default pytest run never collects it.

    uv run python tests/probes/measure_index.py index --model-home HOME --work DIR [--out FILE]
    uv run python tests/probes/measure_index.py scale --work DIR [--rows 240000 --more 60000] [--out FILE]
    uv run python tests/probes/measure_index.py pair --model-home HOME --store-home HOME [--out FILE]

``index`` (P4-P6, real model): synthetic corpus -> scan -> ``index.run_index`` interrupted at the
fifth embedding batch (KeyboardInterrupt, as Ctrl-C) -> run again (must embed exactly the rest) ->
a third run (``chunked 0 embedded 0``, time) -> every stored vector compared with the text embedded
alone (vector = function of the text). Peak commit of the process against I1
(R_model + 1.5 GiB), seconds per chunk, ``truncated``, token counts of the chunks.

``scale`` (P7, S1/S2): a synthetic store with ``--rows`` distinct chunks and random unit vectors under
the ``HashEmbedder(1024)`` model key, written straight to the store (texts and vectors are synthetic;
rankings are meaningless, memory and time are not). One fresh process measures the cached float16
matrix (rows under ``max_vectors_in_memory``), then ``--more`` rows are added and another fresh
process measures the streaming path (over the cap).

``pair`` (P10, S5, M2): two fresh processes query one store at the same time with the real model
(read-only connections); each reports its load time, latencies, commit, and the commit after the
idle release (``Engine.maybe_unload``). Processes are started here, in test code, never by the package.

Results go to ``--out`` or ``<work>/measure``; nothing leaves the PC; synthetic data only.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))                      # probes/ (measure_model.memory)
sys.path.insert(0, str(HERE.parents[1]))                  # tests/ (fixtures, test_collect)
sys.path.insert(0, str(HERE.parents[2]/'src'))
MIB = 1 << 20
GIB = 1 << 30
I1_BATCH_ALLOWANCE = 1.5 * GIB      # design M2 §6.2 I1: R_model + 1.5 GiB at the default limits
QUERIES = ['업무협약 체결 일정', '가상 거래처 견적 재제출', 'PRJ-012 견적', '납품 지연 사유', '품질보증 결과 보고',
           '담당자 일정 재작성', 'Quote for the project', '시범 운영 범위', '예산 집행 현황', '요구사항 변경 요청서',
           '장비 추가 구성', '주간 보고 정리', '자재명세 확정', '비밀유지계약 검토', '위험 항목 집계',
           '방문 결과', '시험 환경', '착수 회의록', '인수검사 불량', '서비스수준 협의']


def percentile(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    k = (len(ordered) - 1) * q
    low = int(k)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (k - low), 1)


def mib(value):
    return round(value / MIB, 1)


# ---- P4-P6 ------------------------------------------------------------------------------------

class Interrupting:
    """Embedder wrapper that raises KeyboardInterrupt on the ``fail_at``-th ``embed`` call."""

    def __init__(self, inner, fail_at):
        self.inner, self.fail_at, self.calls = inner, fail_at, 0
        self.identity = inner.identity

    def embed(self, texts, *, query):
        self.calls += 1
        if self.calls == self.fail_at:
            raise KeyboardInterrupt
        return self.inner.embed(texts, query=query)

    def token_counts(self, texts, *, query=False):
        return self.inner.token_counts(texts, query=query)


def run_index_probe(args) -> dict:
    import numpy as np
    from fixtures import corpus as corpus_module
    from measure_model import memory
    from test_collect import add_root, scan
    from workmcp import config, index, models, paths, store
    from workmcp.embedder import OnnxEmbedder

    base = memory()
    work = Path(args.work)
    home = paths.ensure_home(work/'home')
    root = work/'corpus'
    corpus_module.write(corpus_module.build(7), root)
    db = store.open_home(home)
    out = {'probe': 'index', 'base_commit_mib': mib(base['commit'])}
    try:
        scan(db, add_root(db, root))
        started = time.perf_counter()
        model = OnnxEmbedder(models.DEFAULT_MODEL, Path(args.model_home))
        out['model_load_seconds'] = round(time.perf_counter() - started, 2)
        loaded = memory()
        out['r_model_mib'] = mib(loaded['commit'] - base['commit'])
        settings = config.IndexSettings()
        # P5: interrupt at the fifth batch (4 batches x 8 = 32 texts embedded; commits every 16 rows)
        interrupting = Interrupting(model, fail_at=5)
        small = config.IndexSettings(embed_batch=8, commit_every=16)
        try:
            index.run_index(db, home, embedder=interrupting, settings=small)
        except KeyboardInterrupt:
            pass
        committed = db.execute('SELECT COUNT(*) FROM chunk_vectors').fetchone()[0]
        status = db.execute('SELECT status FROM index_runs ORDER BY id DESC LIMIT 1').fetchone()[0]
        total = db.execute('SELECT COUNT(DISTINCT text_sha256) FROM chunks').fetchone()[0]
        started = time.perf_counter()
        resumed = index.run_index(db, home, embedder=model, settings=settings)
        seconds = time.perf_counter() - started
        out['p5'] = {'texts': total, 'committed_before_interrupt': committed, 'resumed_embedded': resumed['embedded'],
                     'ok': resumed['embedded'] == total - committed,
                     'run_status_after_interrupt': status}
        out['p4'] = {'chunks': resumed['chunks_total'], 'embedded_in_resume': resumed['embedded'],
                     'embed_seconds': round(seconds, 2),
                     'seconds_per_chunk': round(seconds / max(1, resumed['embedded']), 3),
                     'truncated': resumed['truncated']}
        started = time.perf_counter()
        third = index.run_index(db, home, embedder=model, settings=settings)
        out['p6'] = {'seconds': round(time.perf_counter() - started, 3), 'chunked': third['chunked'],
                     'embedded': third['embedded'], 'ok': third['chunked'] == 0 and third['embedded'] == 0}
        peak = memory()['peak_commit']
        out['i1'] = {'peak_commit_over_base_mib': mib(peak - base['commit']),
                     'bound_mib': mib(loaded['commit'] - base['commit'] + I1_BATCH_ALLOWANCE),
                     'ok': peak - base['commit'] <= loaded['commit'] - base['commit'] + I1_BATCH_ALLOWANCE}
        # vector = function of the text: every stored vector against the text embedded alone
        cosines, tokens = [], []
        for sha, blob in db.execute('SELECT text_sha256, vector FROM chunk_vectors').fetchall():
            text = db.execute('SELECT text FROM chunks WHERE text_sha256=? LIMIT 1', (sha,)).fetchone()[0]
            alone = model.embed([text], query=False)[0]
            cosines.append(float(np.dot(index.from_blob(blob, alone.shape[0]), alone)))
            tokens.append(model.token_counts([text])[0])
        chars = [len(t) for (t,) in db.execute('SELECT text FROM chunks')]
        out['vectors_vs_alone'] = {'n': len(cosines), 'min_cosine': round(min(cosines), 6), 'ok': min(cosines) >= 0.9999}
        out['chunks'] = {'chars_p50_p95_max': [percentile(chars, q) for q in (0.5, 0.95, 1.0)],
                         'tokens_p50_p95_max': [percentile(tokens, q) for q in (0.5, 0.95, 1.0)],
                         'over_max_tokens': sum(t > model.identity['max_tokens'] for t in tokens)}
    finally:
        db.close()
    return out


# ---- P7 ---------------------------------------------------------------------------------------

def build_scale_store(home: Path, start: int, count: int) -> None:
    """``count`` synthetic units/chunks (ordinals ``start+1..``) in one synthetic version, with random
    unit vectors under the HashEmbedder(1024) model key; ``index_generation`` + 1."""
    import numpy as np
    from workmcp import chunks, index, store
    from workmcp.embedder import HashEmbedder
    key = index.model_key(HashEmbedder(1024).identity)
    db = store.open_home(home)
    rng = np.random.default_rng(start + 1)
    try:
        db.execute('BEGIN IMMEDIATE')
        if start == 0:
            root = db.execute(r"INSERT INTO source_roots(path, added_at) VALUES('C:\synthetic-scale', 'x')").lastrowid
            doc = db.execute(r"INSERT INTO documents(root_id, path, path_key, ext, status, first_seen_at, last_seen_at) "
                             r"VALUES(?, 'C:\synthetic-scale\rows.txt', 'c:\synthetic-scale\rows.txt', '.txt', 'active', "
                             r"'x', 'x')", (root,)).lastrowid
            db.execute("INSERT INTO versions(id, document_id, seq, sha256, size, mtime_ns, observed_at, parse_status, "
                       "parser, parser_version, unit_count) VALUES(1, ?, 1, ?, 1, 1, '2026-10-24T00:00:00+09:00', 'ok', "
                       "'text', 'text-3', 1)", (doc, 'b' * 64))
        db.execute('COMMIT')
        words = ['합성', '대량', '행', '측정', '견적', '일정', '납품', '검수', 'synthetic', 'scale']
        block = 20_000
        for first in range(start, start + count, block):
            n = min(block, start + count - first)
            texts = [f'합성 대량 행 {first + i + 1:07d} ' + ' '.join(words[(first + i + j) % 10] for j in range(8))
                     for i in range(n)]
            vectors = rng.standard_normal((n, 1024)).astype(np.float32)
            vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
            db.execute('BEGIN IMMEDIATE')
            db.executemany("INSERT INTO units(version_id, ordinal, kind, locator_json, text, text_sha256) "
                           "VALUES(1, ?, 'line', ?, ?, ?)",
                           [(first + i + 1, json.dumps({'kind': 'line', 'line': first + i + 1}, sort_keys=True), t,
                             chunks.ChunkSpec(0, 0, 0, 0, 0, t).text_sha256()) for i, t in enumerate(texts)])
            # chunk rows go through store.insert_chunks only; one call per version, so stage them under a
            # scratch rule version per block and merge into the current one with store's copy path
            store.insert_chunks(db, 1, f'scale-{first}', [chunks.ChunkSpec(first + i + 1, first + i + 1, first + i + 1,
                                                                            0, len(t), t) for i, t in enumerate(texts)])
            db.executemany('INSERT INTO chunk_vectors VALUES(?, ?, 1024, ?, ?)',
                           [(key, chunks.ChunkSpec(0, 0, 0, 0, 0, t).text_sha256(), index.to_blob(v), 'x')
                            for t, v in zip(texts, vectors)])
            db.execute('COMMIT')
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE versions SET unit_count=? WHERE id=1', (start + count,))
        db.execute(f"UPDATE chunks SET chunk_version=? WHERE chunk_version LIKE 'scale-%'", (chunks.CHUNK_VERSION,))
        generation = int(store.meta_get(db, 'index_generation', '0')) + 1
        store.meta_set(db, 'index_generation', str(generation))
        db.execute('COMMIT')
    finally:
        db.close()


def scale_query(args) -> dict:
    """One fresh process: Engine(HashEmbedder) semantic and hybrid queries over the scale store."""
    from measure_model import memory
    from workmcp import search
    from workmcp.config import SearchSettings
    from workmcp.embedder import HashEmbedder
    base = memory()
    engine = search.Engine(Path(args.store_home), SearchSettings(), embedder_factory=lambda: HashEmbedder(1024))
    started = time.perf_counter()
    first = engine.search(QUERIES[0], mode='semantic')
    cold_ms = (time.perf_counter() - started) * 1000
    loaded = memory()
    times = {'semantic': [], 'hybrid': [], 'lexical': []}
    for mode in times:
        for text in QUERIES:
            began = time.perf_counter()
            engine.search(text, mode=mode)
            times[mode].append((time.perf_counter() - began) * 1000)
    after = memory()
    status = engine.status()
    engine.unload()
    released = memory()
    return {'rows': first['meta']['semantic']['vectors'], 'streamed': first['meta']['semantic']['streamed'],
            'cold_first_semantic_ms': round(cold_ms, 1), 'matrix_bytes_mib': mib(status['matrix_bytes']),
            'commit_after_load_mib': mib(loaded['commit'] - base['commit']),
            'commit_after_queries_mib': mib(after['commit'] - base['commit']),
            'peak_commit_mib': mib(after['peak_commit'] - base['commit']),
            's1_bound_mib': mib(status['matrix_bytes'] + 64 * MIB if status['matrix_bytes'] else (32 + 64) * MIB),
            'after_unload_mib': mib(released['commit'] - base['commit']),
            'latency_ms': {mode: {'p50': percentile(v, 0.5), 'p95': percentile(v, 0.95), 'max': percentile(v, 1.0)}
                           for mode, v in times.items()}}


def run_scale_probe(args) -> dict:
    from workmcp import paths
    work = Path(args.work)
    home = paths.ensure_home(work/'scale-home')
    out = {'probe': 'scale'}
    started = time.perf_counter()
    build_scale_store(home, 0, args.rows)
    out['build_seconds_cached'] = round(time.perf_counter() - started, 1)
    out['cached'] = child(['scale-query', '--store-home', str(home)])
    started = time.perf_counter()
    build_scale_store(home, args.rows, args.more)
    out['build_seconds_more'] = round(time.perf_counter() - started, 1)
    out['streamed'] = child(['scale-query', '--store-home', str(home)])
    out['db_mib'] = mib(sum(p.stat().st_size for p in paths.db_path(home).parent.glob('*')))
    return out


# ---- P10 --------------------------------------------------------------------------------------

def pair_worker(args) -> dict:
    """One of two concurrent processes: real model through Engine, 20 hybrid queries, idle release."""
    from measure_model import memory
    from workmcp import models, search
    from workmcp.config import SearchSettings
    from workmcp.embedder import OnnxEmbedder
    base = memory()
    settings = SearchSettings()
    engine = search.Engine(Path(args.store_home), settings, embedder_factory=lambda: OnnxEmbedder(
        models.DEFAULT_MODEL, Path(args.model_home), threads=settings.effective_threads()))
    while time.time() < args.start_at:      # both processes begin together
        time.sleep(0.01)
    started = time.perf_counter()
    first = engine.search(QUERIES[0], mode='hybrid')
    cold = (time.perf_counter() - started) * 1000
    times, errors = [], 0
    for _ in range(2):
        for text in QUERIES:
            began = time.perf_counter()
            try:
                engine.search(text, mode='hybrid')
            except Exception:  # noqa: BLE001 - counted, the criterion is zero
                errors += 1
            times.append((time.perf_counter() - began) * 1000)
    busy = memory()
    released = engine.maybe_unload(now=engine.last_used + settings.model_idle_seconds + 1)
    idle = memory()
    return {'pid': os.getpid(), 'threads': settings.effective_threads(), 'cold_first_ms': round(cold, 1),
            'hits_first': len(first['hits']), 'errors': errors,
            'p50_ms': percentile(times, 0.5), 'p95_ms': percentile(times, 0.95),
            'commit_busy_mib': mib(busy['commit'] - base['commit']),
            'peak_commit_mib': mib(busy['peak_commit'] - base['commit']),
            'released': released, 'commit_after_idle_release_mib': mib(idle['commit'] - base['commit'])}


def run_pair_probe(args) -> dict:
    start_at = time.time() + 3.0
    procs = [subprocess.Popen([sys.executable, str(HERE), 'pair-worker', '--model-home', args.model_home,
                               '--store-home', args.store_home, '--start-at', str(start_at)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
             for _ in range(2)]
    results = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=900)
        if proc.returncode != 0:
            raise RuntimeError(stderr[-2000:])
        results.append(json.loads(stdout.strip().splitlines()[-1]))
    return {'probe': 'pair', 'workers': results,
            'sum_peak_commit_mib': round(sum(r['peak_commit_mib'] for r in results), 1)}


def child(argv) -> dict:
    run = subprocess.run([sys.executable, str(HERE), *argv], capture_output=True, text=True, encoding='utf-8',
                         timeout=1800)
    if run.returncode != 0:
        raise RuntimeError(run.stderr[-2000:])
    return json.loads(run.stdout.strip().splitlines()[-1])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('probe', choices=('index', 'scale', 'scale-query', 'pair', 'pair-worker'))
    parser.add_argument('--model-home')
    parser.add_argument('--store-home')
    parser.add_argument('--work')
    parser.add_argument('--rows', type=int, default=240_000)
    parser.add_argument('--more', type=int, default=60_000)
    parser.add_argument('--start-at', type=float, default=0.0)
    parser.add_argument('--out')
    args = parser.parse_args(argv)
    if args.work is None and args.probe in ('index', 'scale'):
        args.work = tempfile.mkdtemp(prefix='workmcp-measure-')
    handlers = {'index': run_index_probe, 'scale': run_scale_probe, 'scale-query': scale_query,
                'pair': run_pair_probe, 'pair-worker': pair_worker}
    result = handlers[args.probe](args)
    text = json.dumps(result, ensure_ascii=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())

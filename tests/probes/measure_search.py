"""Search measurement harness (design M2 §8.4, probes P8-P9, guarantees S1-S3). A script, not a test
module: its name does not match ``test_*.py``, so the default pytest run never collects it.

    uv run python tests/probes/measure_search.py [--embedder real|hash] [--model-home HOME] [--work DIR]
                                                  [--seed 7] [--threads N] [--out FILE]
    uv run pytest tests/probes/measure_search.py -m probe     # hash run (always) + real run (if a model)

One fresh process per run. Steps: a temporary data home (``--work``, default under %TEMP%, outside
the repository) -> ``corpus.write`` -> scan -> ``index.run_index`` with the embedder (time, peak
commit; a second run must report ``chunked 0 embedded 0``) -> the cold first hybrid query (model
load + matrix load) -> every query of the set in each mode twice, the second (warm) time recorded
-> the metric tables (document@1/5/10, unit@10, MRR@10 per category and mode) -> hostile queries
(errors are codes, never exceptions) -> commit memory before and after the matrix load and after
the query runs (S1: matrix increase <= rows x dims x 2 + 64 MiB).

``--embedder real`` uses the manifest model from ``--model-home`` (default: ``tests/_real_model``'s
search, i.e. the default data home) through ``search.Engine(embedder_factory=...)``; the model is
read in place, not copied. ``--embedder hash`` uses ``HashEmbedder(1024)``: a functional run whose
quality numbers are not a model's (the report says so). Without a real model ``--embedder real``
exits 3 (pytest: skip). The result JSON goes to ``<work>/measure/``; the console gets the tables.
Offline: nothing here reaches the network.
"""
import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))                      # probes/ (measure_model.memory)
sys.path.insert(0, str(HERE.parents[1]))                  # tests/ (fixtures, _real_model)
sys.path.insert(0, str(HERE.parents[2]/'src'))
MIB = 1 << 20
MATRIX_SLACK = 64 * MIB
HOSTILE = ['"', '"""', '*', 'NEAR(a b, 2)', 'text:견적', '-견적 ^회의', '(견적) AND', '\x00\x01견적',
           '😀😀 견적', 'a' * 1000, "'; DROP TABLE chunks; --", '', 'x' * 1001]


def percentile(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    k = (len(ordered) - 1) * q
    low = int(k)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (k - low), 1)


def measure(args) -> dict:
    from fixtures import corpus as corpus_module
    from measure_model import memory
    from test_collect import add_root, scan
    from workmcp import index, models, paths, search, store
    from workmcp.config import SearchSettings
    from workmcp.embedder import HashEmbedder, OnnxEmbedder

    if args.embedder == 'real':
        from _real_model import real_model_home
        model_home = Path(args.model_home) if args.model_home else real_model_home()
        if model_home is None or not models.available(models.DEFAULT_MODEL, model_home):
            raise LookupError('no real model')
        settings = SearchSettings()

        def factory():
            return OnnxEmbedder(models.DEFAULT_MODEL, model_home, threads=args.threads or settings.effective_threads())
    else:
        def factory():
            return HashEmbedder(1024)

    work = Path(args.work or tempfile.mkdtemp(prefix='workmcp-measure-'))
    home = paths.ensure_home(work/'home')
    root = work/'corpus'
    base = memory()
    corpus = corpus_module.build(args.seed)
    corpus_module.write(corpus, root)
    result = {'embedder': args.embedder, 'seed': args.seed, 'home': str(home), 'base': base,
              'note': None if args.embedder == 'real' else 'HashEmbedder: not a model; quality numbers are reference only'}

    # Index (P4/P6 context: the stage-2 index probes are measure_model/A's; recorded here for the corpus).
    db = store.open_home(home)
    try:
        started = time.perf_counter()
        scan(db, add_root(db, root))
        result['scan_seconds'] = round(time.perf_counter() - started, 2)
        started = time.perf_counter()
        index_embedder = factory()
        load_seconds = time.perf_counter() - started
        started = time.perf_counter()
        first = index.run_index(db, home, embedder=index_embedder)
        result['index'] = {'model_load_seconds': round(load_seconds, 2),
                           'seconds': round(time.perf_counter() - started, 2),
                           'peak_commit_mib': round(memory()['peak_commit'] / MIB, 1),
                           **{k: first[k] for k in ('chunked', 'chunks_total', 'embedded', 'truncated',
                                                    'vectors_for_model')}}
        started = time.perf_counter()
        second = index.run_index(db, home, embedder=index_embedder)
        result['index_second'] = {'seconds': round(time.perf_counter() - started, 2),
                                  'chunked': second['chunked'], 'embedded': second['embedded']}
        result['corpus'] = {
            'documents': len(corpus.topics), 'queries': len(corpus.queries),
            'units': db.execute('SELECT COUNT(*) FROM units').fetchone()[0],
            'chunk_chars': [percentile([len(t) for (t,) in db.execute('SELECT text FROM chunks')], q)
                            for q in (0.5, 0.95, 1.0)]}
    finally:
        db.close()
    del index_embedder

    # Search: cold query, then warm runs per mode.
    engine = search.Engine(home, embedder_factory=factory)
    before = memory()
    started = time.perf_counter()
    engine.embedder()                                   # model load (P1 territory, measured by measure_model)
    model_seconds = time.perf_counter() - started
    with_model = memory()
    cold = engine.search(corpus.queries[0].text, mode='hybrid')   # matrix load + first query
    result['cold_first_query_ms'] = round((time.perf_counter() - started) * 1000, 1)
    result['model_load_seconds'] = round(model_seconds, 2)
    loaded = memory()
    status = engine.status()
    result['matrix'] = {**status, 'model_commit_mib': round((with_model['commit'] - before['commit']) / MIB, 1),
                        'commit_increase_mib': round((loaded['commit'] - with_model['commit']) / MIB, 1),
                        'semantic': cold['meta']['semantic']}
    report, latency = {}, {}
    for mode in ('lexical', 'semantic', 'hybrid'):
        times = []

        def run(text, mode=mode):
            engine.search(text, mode=mode)
            began = time.perf_counter()
            answer = engine.search(text, mode=mode)
            times.append((time.perf_counter() - began) * 1000)
            return answer

        report[mode] = corpus_module.evaluate(run, corpus, root, modes=(mode,))[mode]
        latency[mode] = {'p50_ms': percentile(times, 0.5), 'p95_ms': percentile(times, 0.95),
                         'max_ms': percentile(times, 1.0), 'n': len(times)}
    after = memory()
    result['memory'] = {'before_matrix_mib': round((before['commit'] - base['commit']) / MIB, 1),
                        'after_load_mib': round((loaded['commit'] - base['commit']) / MIB, 1),
                        'after_queries_mib': round((after['commit'] - base['commit']) / MIB, 1),
                        'peak_mib': round((after['peak_commit'] - base['commit']) / MIB, 1),
                        'matrix_after_queries_mib': round((after['commit'] - with_model['commit']) / MIB, 1),
                        's1_bound_mib': round((status['matrix_bytes'] + MATRIX_SLACK) / MIB, 1)}
    result['metrics'] = {mode: part['table'] for mode, part in report.items()}
    result['behaviour'] = {mode: part['behaviour'] for mode, part in report.items()}
    result['misses_hybrid'] = [
        {'id': qid, 'category': cat, 'top1': top,
         'gold_docs': next(q.gold_docs for q in corpus.queries if q.id == qid)}
        for qid, cat, scores, top in report['hybrid']['rows'] if not scores['doc@10']]
    result['latency'] = latency

    # Hostile queries (P9).
    hostile = []
    for text in HOSTILE:
        for mode in ('lexical', 'semantic', 'hybrid'):
            try:
                engine.search(text, mode=mode)
                hostile.append('ok')
            except search.SearchError as exc:
                hostile.append(exc.code)
            except Exception as exc:  # noqa: BLE001 - counted, the criterion is zero
                hostile.append(f'exception:{type(exc).__name__}')
    result['hostile'] = {'cases': len(hostile), 'exceptions': sum(h.startswith('exception') for h in hostile),
                         'codes': sorted(set(hostile))}
    engine.unload()
    result['after_unload_mib'] = round((memory()['commit'] - base['commit']) / MIB, 1)
    return result


def tables(result: dict) -> str:
    lines = [f"embedder {result['embedder']}  seed {result['seed']}" + (f"  ({result['note']})" if result['note'] else '')]
    idx = result['index']
    lines.append(f"index: {idx['chunks_total']} chunks, embedded {idx['embedded']}, truncated {idx['truncated']}, "
                 f"{idx['seconds']} s (model load {idx['model_load_seconds']} s), peak commit {idx['peak_commit_mib']} MiB; "
                 f"second run chunked {result['index_second']['chunked']} embedded {result['index_second']['embedded']}")
    lines.append(f"cold first hybrid query {result['cold_first_query_ms']} ms; matrix rows {result['matrix']['rows']} "
                 f"({result['matrix']['matrix_bytes'] / MIB:.1f} MiB), commit +{result['matrix']['commit_increase_mib']} MiB")
    lines.append('')
    lines.append('| mode | category | n | doc@1 | doc@5 | doc@10 | unit@10 | MRR@10 |')
    lines.append('|---|---|---|---|---|---|---|---|')
    for mode, table in result['metrics'].items():
        for category, row in table.items():
            lines.append(f"| {mode} | {category} | {row['n']} | {row['doc@1']:.2f} | {row['doc@5']:.2f} | "
                         f"{row['doc@10']:.2f} | {row['unit@10']:.2f} | {row['rr@10']:.2f} |")
    lines.append('')
    lines.append('| mode | p50 ms | p95 ms | max ms |')
    lines.append('|---|---|---|---|')
    for mode, row in result['latency'].items():
        lines.append(f"| {mode} | {row['p50_ms']} | {row['p95_ms']} | {row['max_ms']} |")
    lines.append(f"memory MiB over base: {json.dumps(result['memory'])}; after unload {result['after_unload_mib']}")
    lines.append(f"hostile: {json.dumps(result['hostile'])}; hybrid misses @10: {len(result['misses_hybrid'])}")
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--embedder', choices=('real', 'hash'), default='real')
    parser.add_argument('--model-home')
    parser.add_argument('--work')
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--threads', type=int, default=0)
    parser.add_argument('--out')
    args = parser.parse_args(argv)
    try:
        result = measure(args)
    except LookupError as exc:
        print(f'skipped: {exc}', file=sys.stderr)
        return 3
    out = Path(args.out) if args.out else (Path(result['home'])/'measure'/
                                           f"search-{args.embedder}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    print(tables(result))
    print(f'result: {out}')
    return 0


@pytest.mark.probe
@pytest.mark.parametrize('embedder', ['hash', 'real'])
def test_probe_search(tmp_path, embedder):
    """``pytest tests/probes/measure_search.py -m probe``: P8/P9 in a fresh process, S1/S3 criteria."""
    run = subprocess.run([sys.executable, str(HERE), '--embedder', embedder, '--work', str(tmp_path/'work'),
                          '--out', str(tmp_path/'search.json')], capture_output=True, text=True,
                         encoding='utf-8', timeout=3600)
    if run.returncode == 3:
        pytest.skip(run.stderr.strip())
    assert run.returncode == 0, run.stderr[-2000:]
    result = json.loads((tmp_path/'search.json').read_text(encoding='utf-8'))
    assert result['hostile']['exceptions'] == 0
    assert result['index_second']['chunked'] == 0 and result['index_second']['embedded'] == 0
    assert result['matrix']['commit_increase_mib'] <= result['memory']['s1_bound_mib']
    assert result['metrics']['lexical']['Q1_phrase']['doc@10'] >= 0.8


if __name__ == '__main__':
    sys.exit(main())

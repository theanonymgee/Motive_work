"""Search measurement harness (design M2 §8.4, probes P8-P9, guarantees S1-S3). A script, not a test
module: its name does not match ``test_*.py``, so the default pytest run never collects it.

    uv run python tests/probes/measure_search.py [--embedder real|hash] [--model-home HOME] [--work DIR]
                                                  [--seed 7] [--threads N] [--out FILE]
    uv run python tests/probes/measure_search.py --signals [--seeds 7 11 23] [--embedder real|hash] ...
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

``--signals`` (design M3 §9.3, absent-information signals): for each seed (default 7, 11, 23) a fresh
home -> ``corpus.write`` -> scan -> index with one shared embedder, then every Q1-Q7 query, the Q9
queries and ``corpus.absent_queries()`` (60) in hybrid mode (``meta.signals``) and lexical mode
(``bm25_top1``). "Answered" samples are the Q1-Q7 queries whose gold document is in the hybrid top
10; "absent" are Q9 and the 60 absent queries. The rules R1 ``cosine_top1 < t``, R2 ``cosine_gap <
d``, R3 no lexical candidate and no exact phrase, R4 = R3 and R1 are swept over thresholds; for each
the best recall on absent with a false-alarm rate on answered <= 10 % is reported, pooled, per seed,
and fitted on the first seed only then applied to the other seeds.
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


# ---- absent-information signals (design M3 §9.3) -----------------------------------------------------

FALSE_ALARM_MAX = 0.10
SIGNAL_FACTS = ('cosine_top1', 'cosine_top10', 'cosine_gap', 'lexical_candidates', 'exact_phrase_hits', 'bm25_top1')
RULES = ('R1', 'R2', 'R3', 'R4')


def _factory(args):
    """One shared embedder for every seed's index and engine (the model loads once)."""
    from workmcp import models
    from workmcp.config import SearchSettings
    from workmcp.embedder import HashEmbedder, OnnxEmbedder
    if args.embedder == 'real':
        from _real_model import real_model_home
        model_home = Path(args.model_home) if args.model_home else real_model_home()
        if model_home is None or not models.available(models.DEFAULT_MODEL, model_home):
            raise LookupError('no real model')
        shared = OnnxEmbedder(models.DEFAULT_MODEL, model_home,
                              threads=args.threads or SearchSettings().effective_threads())
    else:
        shared = HashEmbedder(1024)
    return lambda: shared


def signal_rows(seed: int, factory, work: Path) -> tuple[list[dict], dict]:
    """One seed: build, scan, index, then the facts of every query (synthetic query text only)."""
    from fixtures import corpus as corpus_module
    from test_collect import add_root, scan
    from workmcp import index, paths, search, store

    home = paths.ensure_home(work/f'seed-{seed}'/'home')
    root = work/f'seed-{seed}'/'corpus'
    corpus = corpus_module.build(seed)
    corpus_module.write(corpus, root)
    db = store.open_home(home)
    try:
        scan(db, add_root(db, root))
        started = time.perf_counter()
        indexed = index.run_index(db, home, embedder=factory())
        info = {'seed': seed, 'index_seconds': round(time.perf_counter() - started, 1),
                'chunks': indexed['chunks_total'], 'embedded': indexed['embedded']}
    finally:
        db.close()
    engine = search.Engine(home, embedder_factory=factory)
    queries = [(q.id, q.category, q.text, q) for q in corpus.queries if q.category != 'Q8_two_chars']
    queries += [(f'Q9x-{n:03d}', 'Q9x_absent', text, None) for n, text in enumerate(corpus_module.absent_queries(), 1)]
    rows = []
    for qid, category, text, query in queries:
        hybrid = engine.search(text, mode='hybrid')
        lexical = engine.search(text, mode='lexical')
        facts = {key: hybrid['meta']['signals'][key] for key in SIGNAL_FACTS if key != 'bm25_top1'}
        top_bm25 = [h['scores']['bm25'] for h in lexical['hits'][:1] if h['scores']['bm25'] is not None]
        facts['bm25_top1'] = round(top_bm25[0], 3) if top_bm25 else None
        absent = category.startswith('Q9')
        found = None if absent else bool(corpus_module.score_query(query, hybrid['hits'], root)['doc@10'])
        rows.append({'seed': seed, 'id': qid, 'category': category, 'text': text, 'absent': absent,
                     'gold_in_top10': found, 'hits': len(hybrid['hits']), 'low_default': hybrid['meta']['signals']['low'],
                     **facts})
    info['queries'] = len(rows)
    return rows, info


def _rule(name: str, row: dict, threshold) -> bool:
    lexical_none = row['lexical_candidates'] == 0 and row['exact_phrase_hits'] == 0
    if name == 'R1':
        return row['cosine_top1'] is not None and threshold is not None and row['cosine_top1'] < threshold
    if name == 'R2':
        return row['cosine_gap'] is not None and threshold is not None and row['cosine_gap'] < threshold
    if name == 'R3':
        return lexical_none
    return lexical_none and row['cosine_top1'] is not None and threshold is not None and row['cosine_top1'] < threshold


def _answered(rows):
    return [r for r in rows if not r['absent'] and r['gold_in_top10']]


def rates(name: str, rows: list[dict], threshold) -> dict:
    answered, absent = _answered(rows), [r for r in rows if r['absent']]
    hit = sum(_rule(name, r, threshold) for r in absent)
    alarm = sum(_rule(name, r, threshold) for r in answered)
    return {'recall': round(hit / len(absent), 3) if absent else None,
            'false_alarm': round(alarm / len(answered), 3) if answered else None,
            'absent_n': len(absent), 'answered_n': len(answered)}


def best_threshold(name: str, rows: list[dict]) -> dict:
    """The threshold with the highest recall at false alarm <= FALSE_ALARM_MAX (ties: lower false alarm,
    then the lower threshold); R3 has none."""
    if name == 'R3':
        return {'rule': name, 'threshold': None, **rates(name, rows, None)}
    best = None
    for threshold in (round(0.001 * k, 3) for k in range(0, 1001)):
        found = rates(name, rows, threshold)
        if found['false_alarm'] is None or found['false_alarm'] > FALSE_ALARM_MAX:
            continue
        key = (found['recall'], -found['false_alarm'], -threshold)
        if best is None or key > best[0]:
            best = (key, {'rule': name, 'threshold': threshold, **found})
    return best[1] if best else {'rule': name, 'threshold': None, 'recall': 0.0, 'false_alarm': None}


def distribution(rows: list[dict], fact: str) -> dict:
    out = {}
    for label, chosen in (('answered', _answered(rows)),
                          ('missed', [r for r in rows if not r['absent'] and r['gold_in_top10'] is False]),
                          ('absent', [r for r in rows if r['absent']])):
        values = [r[fact] for r in chosen if r[fact] is not None]
        out[label] = {'n': len(values), 'min': percentile4(values, 0.0), 'p5': percentile4(values, 0.05),
                      'p50': percentile4(values, 0.5), 'p95': percentile4(values, 0.95), 'max': percentile4(values, 1.0)}
    return out


def percentile4(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    k = (len(ordered) - 1) * q
    low = int(k)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (k - low), 4)


def measure_signals(args) -> dict:
    factory = _factory(args)
    work = Path(args.work or tempfile.mkdtemp(prefix='workmcp-signals-'))
    rows, seeds = [], []
    for seed in args.seeds:
        part, info = signal_rows(seed, factory, work)
        rows += part
        seeds.append(info)
    pooled = {name: best_threshold(name, rows) for name in RULES}
    per_seed = {name: {seed: rates(name, [r for r in rows if r['seed'] == seed], pooled[name]['threshold'])
                       for seed in args.seeds} for name in RULES}
    first = args.seeds[0]
    fitted = {name: best_threshold(name, [r for r in rows if r['seed'] == first]) for name in RULES}
    holdout = {name: rates(name, [r for r in rows if r['seed'] != first], fitted[name]['threshold'])
               for name in RULES}
    missed = [r for r in rows if not r['absent'] and r['gold_in_top10'] is False]
    by_category = {}
    for category in sorted({r['category'] for r in rows}):
        chosen = [r for r in rows if r['category'] == category]
        by_category[category] = {name: sum(_rule(name, r, pooled[name]['threshold']) for r in chosen) for name in RULES}
        by_category[category]['n'] = len(chosen)
    return {'embedder': args.embedder, 'seeds': seeds, 'work': str(work), 'false_alarm_max': FALSE_ALARM_MAX,
            'counts': {'answered': len(_answered(rows)), 'missed': len(missed),
                       'absent': sum(1 for r in rows if r['absent'])},
            'distribution': {fact: distribution(rows, fact) for fact in SIGNAL_FACTS},
            'pooled': pooled, 'per_seed': per_seed, 'fitted_on_first_seed': fitted, 'holdout': holdout,
            'missed_flagged': {name: sum(_rule(name, r, pooled[name]['threshold']) for r in missed) for name in RULES},
            'flagged_by_category': by_category, 'rows': rows}


def signal_tables(result: dict) -> str:
    counts = result['counts']
    lines = [f"signals: embedder {result['embedder']}, seeds {[s['seed'] for s in result['seeds']]}, "
             f"answered {counts['answered']}, missed {counts['missed']}, absent {counts['absent']}",
             f"index: {json.dumps(result['seeds'])}",
             '', '| fact | sample | n | min | p5 | p50 | p95 | max |', '|---|---|---|---|---|---|---|---|']
    for fact, parts in result['distribution'].items():
        for label, row in parts.items():
            lines.append(f"| {fact} | {label} | {row['n']} | {row['min']} | {row['p5']} | {row['p50']} | "
                         f"{row['p95']} | {row['max']} |")
    lines += ['', '| rule | threshold | recall (absent) | false alarm (answered) | missed flagged | '
              'fit on first seed: threshold / holdout recall / holdout false alarm |', '|---|---|---|---|---|---|']
    for name, row in result['pooled'].items():
        fit, hold = result['fitted_on_first_seed'][name], result['holdout'][name]
        lines.append(f"| {name} | {row['threshold']} | {row['recall']} | {row['false_alarm']} | "
                     f"{result['missed_flagged'][name]}/{counts['missed']} | "
                     f"{fit['threshold']} / {hold['recall']} / {hold['false_alarm']} |")
    lines += ['', '| rule | seed | recall | false alarm |', '|---|---|---|---|']
    for name, seeds in result['per_seed'].items():
        for seed, row in seeds.items():
            lines.append(f"| {name} | {seed} | {row['recall']} | {row['false_alarm']} |")
    lines += ['', '| category | n | ' + ' | '.join(RULES) + ' |', '|---|---|' + '---|' * len(RULES)]
    for category, row in result['flagged_by_category'].items():
        lines.append(f"| {category} | {row['n']} | " + ' | '.join(str(row[name]) for name in RULES) + ' |')
    return '\n'.join(lines)


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
    parser.add_argument('--signals', action='store_true', help='absent-information signals (design M3 §9.3)')
    parser.add_argument('--seeds', type=int, nargs='+', default=[7, 11, 23])
    args = parser.parse_args(argv)
    try:
        result = measure_signals(args) if args.signals else measure(args)
    except LookupError as exc:
        print(f'skipped: {exc}', file=sys.stderr)
        return 3
    name = 'signals' if args.signals else 'search'
    folder = Path(result['work']) if args.signals else Path(result['home'])
    out = Path(args.out) if args.out else (folder/'measure'/
                                           f"{name}-{args.embedder}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    print(signal_tables(result) if args.signals else tables(result))
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


@pytest.mark.probe
def test_probe_signals_hash(tmp_path):
    """``--signals`` end to end with HashEmbedder and one seed: every rule is reported."""
    run = subprocess.run([sys.executable, str(HERE), '--signals', '--embedder', 'hash', '--seeds', '7',
                          '--work', str(tmp_path/'work'), '--out', str(tmp_path/'signals.json')],
                         capture_output=True, text=True, encoding='utf-8', timeout=1800)
    assert run.returncode == 0, run.stderr[-2000:]
    result = json.loads((tmp_path/'signals.json').read_text(encoding='utf-8'))
    assert set(result['pooled']) == set(RULES) and result['counts']['absent'] == 65
    assert all(set(row) >= set(SIGNAL_FACTS) for row in result['rows'])


if __name__ == '__main__':
    sys.exit(main())

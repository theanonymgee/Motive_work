"""Model resource probes P1-P3 (design M2 §6.2 M1, §6.3). A script, not a test module: its name does not
match ``test_*.py``, so the default pytest run never collects it.

    uv run python tests/probes/measure_model.py [--home DATA_HOME] [--threads N] [--out FILE]
    uv run pytest tests/probes/measure_model.py -m probe      # same run in a fresh process, with the criteria

One process per run, so the commit numbers start from a clean base. Memory is the process commit
(``PagefileUsage``) and peak commit (``PeakPagefileUsage``) from ``K32GetProcessMemoryInfo``; the table
reports increases over the base (a). Stages: (a) after importing numpy/onnxruntime/tokenizers,
(b) after ``OnnxEmbedder`` loaded (files verified first), (c) after eight query embeddings,
(d) after ``close()`` + ``gc.collect()``. Throughput uses synthetic Korean/English passages at the
model's token limit and at 64 tokens. The result JSON goes to ``<home>/measure/`` (outside the repository).
Without a real model the run exits 3 (pytest: skip). Offline: nothing here reaches the network.

Criteria (M1 guarantee): load increase <= 3.0 GiB; (c)-(b) <= 300 MiB; (d)-(a) <= 200 MiB.
"""
import argparse
import ctypes
import gc
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                  # tests/ (for _real_model)
sys.path.insert(0, str(HERE.parents[2]/'src'))
MIB = 1 << 20
CRITERIA = {'load_increase_max_gib': 3.0, 'queries_increase_max_mib': 300, 'after_unload_max_mib': 200}
QUERIES = ['예산 승인 결과', '작년 계약 갱신 조건', 'delivery schedule change', '회의 결정사항 담당자',
           'invoice approval process', '보안 점검 항목', 'Q-0042', '신규 입사자 교육 일정']
KO = '합성 회의록에서 예산 항목과 담당자 일정을 확인하고 후속 조치를 정리했다. '
EN = 'The synthetic review covered budget items, owners and the follow-up schedule for the quarter. '


class Counters(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD), ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t), ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t), ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t), ('PagefileUsage', ctypes.c_size_t),
                ('PeakPagefileUsage', ctypes.c_size_t), ('PrivateUsage', ctypes.c_size_t)]


def memory() -> dict:
    """Current and peak commit and working set of this process, in bytes."""
    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    kernel32, psapi = ctypes.WinDLL('kernel32'), ctypes.WinDLL('psapi')
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise OSError('GetProcessMemoryInfo failed')
    return {'commit': counters.PagefileUsage, 'peak_commit': counters.PeakPagefileUsage,
            'working_set': counters.WorkingSetSize}


def passage(loaded, unit: str, tokens: int) -> str:
    text = unit
    while loaded.token_counts([text])[0] < tokens:
        text += unit
    return text


def measure(home: Path, threads: int | None) -> dict:
    import numpy  # noqa: F401  (imported before the base so (a) includes the runtimes)
    import onnxruntime  # noqa: F401
    import tokenizers  # noqa: F401
    from workmcp import embedder, models
    gc.collect()
    stages = {'a_after_imports': memory()}
    started = time.perf_counter()
    models.require_verified(models.DEFAULT_MODEL, home)
    verify_seconds = time.perf_counter() - started
    started = time.perf_counter()
    loaded = embedder.OnnxEmbedder(models.DEFAULT_MODEL, home, threads=threads, verify=False)
    load_seconds = time.perf_counter() - started
    stages['b_after_load'] = memory()
    started = time.perf_counter()
    loaded.embed(QUERIES, query=True)
    query_seconds = time.perf_counter() - started
    stages['c_after_8_queries'] = memory()

    limit = loaded.identity['max_tokens']
    throughput = {}
    for label, tokens in (('64_tokens', 64), (f'{limit}_tokens', limit)):
        texts = [passage(loaded, KO if i % 2 == 0 else EN, tokens) for i in range(16)]
        counts = loaded.token_counts(texts)
        started = time.perf_counter()
        loaded.embed(texts, query=False)
        seconds = time.perf_counter() - started
        throughput[label] = {'passages': len(texts), 'mean_tokens': round(sum(min(c, limit) for c in counts)/len(counts)),
                             'seconds': round(seconds, 2), 'seconds_per_passage': round(seconds/len(texts), 3),
                             'batch_size': loaded.batch_size}
        stages[f'peak_after_{label}'] = memory()
    providers = loaded.providers
    loaded.close()
    del loaded
    gc.collect()
    time.sleep(1.0)
    stages['d_after_unload'] = memory()

    base = stages['a_after_imports']['commit']
    increase = {name: round((m['commit'] - base)/MIB, 1) for name, m in stages.items()}
    peak = {name: round((m['peak_commit'] - base)/MIB, 1) for name, m in stages.items()}
    load_mib = increase['b_after_load']
    queries_mib = increase['c_after_8_queries'] - load_mib
    unload_mib = increase['d_after_unload']
    verdict = {'P1_load_increase_mib': load_mib, 'P1_ok': load_mib <= CRITERIA['load_increase_max_gib']*1024,
               'P2_queries_increase_mib': round(queries_mib, 1), 'P2_ok': queries_mib <= CRITERIA['queries_increase_max_mib'],
               'P3_after_unload_mib': unload_mib, 'P3_ok': unload_mib <= CRITERIA['after_unload_max_mib']}
    return {'threads': threads, 'cpu_count': os.cpu_count(), 'providers': providers,
            'seconds': {'verify_sha256': round(verify_seconds, 2), 'load': round(load_seconds, 2),
                        'eight_queries': round(query_seconds, 2)},
            'commit_increase_over_a_mib': increase, 'peak_commit_increase_over_a_mib': peak,
            'working_set_mib': {name: round(m['working_set']/MIB) for name, m in stages.items()},
            'throughput': throughput, 'criteria': CRITERIA, 'verdict': verdict}


def table(result: dict) -> str:
    rows = [f"threads={result['threads']} cpus={result['cpu_count']} providers={result['providers']}",
            f"seconds: {result['seconds']}", '',
            f"{'stage':26}{'commit +MiB':>13}{'peak +MiB':>12}{'working set MiB':>17}"]
    for name, value in result['commit_increase_over_a_mib'].items():
        rows.append(f"{name:26}{value:>13}{result['peak_commit_increase_over_a_mib'][name]:>12}"
                    f"{result['working_set_mib'][name]:>17}")
    rows.append('')
    rows += [f'throughput {label}: {item}' for label, item in result['throughput'].items()]
    rows.append(f"verdict: {result['verdict']}")
    return '\n'.join(rows)


def main(argv=None) -> int:
    import _real_model
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, default=None, help='data home with the model (default: test helper)')
    parser.add_argument('--threads', type=int, default=None, help='intra_op_num_threads (default: ORT default)')
    parser.add_argument('--out', type=Path, default=None)
    args = parser.parse_args(argv)
    home = args.home or _real_model.real_model_home()
    if home is None:
        print(_real_model.SKIP_REASON, file=sys.stderr)
        return 3
    result = measure(home, args.threads)
    out = args.out or home/'measure'/f"model-threads{args.threads or 'default'}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    print(table(result))
    print(f'wrote {out}')
    return 0 if all(v for k, v in result['verdict'].items() if k.endswith('_ok')) else 1


@pytest.mark.probe
def test_probe_model_resources(tmp_path):
    """``pytest tests/probes/measure_model.py -m probe``: P1-P3 in a fresh process against the criteria."""
    run = subprocess.run([sys.executable, str(HERE), '--out', str(tmp_path/'model.json')], capture_output=True,
                         text=True, encoding='utf-8', timeout=900)
    if run.returncode == 3:
        pytest.skip(run.stderr.strip())
    print(run.stdout)
    assert run.returncode == 0, run.stdout + run.stderr


if __name__ == '__main__':
    sys.exit(main())

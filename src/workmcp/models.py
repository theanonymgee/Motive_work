"""Local embedding model manifest, verification and the explicit download (design §6 #3, §7).

This is the only package module allowed to use the network, and only from the explicit
download path (``download_file`` -> ``fetch`` -> ``main``). Network modules are imported
inside ``download_file`` so importing the manifest (status, embedder, index) loads no
network code. The server never calls these functions.

Every file is pinned to a repository commit, a byte size and a SHA-256; a file is usable
only after its hash matches. Files are stored flat under ``<home>/models/<name>/<revision>/``
so ONNX external data resolves; downloads go to ``<home>/tmp/*.partial`` and are moved in
only after size and hash match.

M2: ``import_from`` copies the pinned files from a folder already on the PC (an earlier
download or an offline bundle) with the same ``.partial`` -> SHA-256 -> ``os.replace`` rule; it
calls no network function. ``workmcp fetch-model NAME --from FOLDER`` and ``workmcp verify-model``
reach this module only through ``cli_fetch_model``/``cli_verify_model``. The download path stays a
skeleton that is neither run nor tested.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

from . import paths

BASE_URL = 'https://huggingface.co'
CHUNK_BYTES = 1 << 20
DEFAULT_MODEL = 'bge-m3'

# Sizes and SHA-256 pinned in Motive_vari (tag kakao-p1-complete-20261022) from the Hugging
# Face tree API at the pinned commit (LFS oid, 2026-10-19).
MANIFEST: dict[str, dict] = {
    'bge-m3': {
        'repo': 'BAAI/bge-m3', 'revision': '5617a9f61b028005a4858fdac845db406aefb181',
        'kind': 'embedding', 'license': 'MIT', 'source': 'huggingface', 'precision': 'fp32', 'pooling': 'cls',
        'query_prefix': '', 'passage_prefix': '', 'dims': 1024, 'max_tokens': 512,
        'model_file': 'model.onnx', 'tokenizer_file': 'tokenizer.json',
        'files': [
            {'path': 'onnx/model.onnx', 'size': 724923,
             'sha256': 'f84251230831afb359ab26d9fd37d5936d4d9bb5d1d5410e66442f630f24435b'},
            {'path': 'onnx/model.onnx_data', 'size': 2266820608,
             'sha256': '1eebfb28493f67bba03ce0ef64bfdc7fc5a3bd9d7493f818bb1d78cd798416b4'},
            {'path': 'onnx/Constant_7_attr__value', 'size': 65552,
             'sha256': 'cdf16f72c5d07b36484056e601ed9687f78477e5d85cee85a34f2406b7fb5906'},
            {'path': 'onnx/tokenizer.json', 'size': 17082821,
             'sha256': '6710678b12670bc442b99edc952c4d996ae309a7020c1fa0096dd245c2faf790'},
        ],
    },
}


class ModelFileError(RuntimeError):
    pass


def entry(name: str) -> dict:
    if name not in MANIFEST:
        raise KeyError(f'unknown model {name!r}; known: {", ".join(MANIFEST)}')
    return {'name': name, **MANIFEST[name]}


def model_dir(name: str, home: Path) -> Path:
    return paths.models_dir(home)/name/entry(name)['revision']


def local_name(spec: dict) -> str:
    return spec['path'].rsplit('/', 1)[-1]


def file_path(name: str, filename: str, home: Path) -> Path:
    return model_dir(name, home)/filename


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while block := handle.read(CHUNK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def identity(name: str) -> dict:
    """Canonical model identity; its JSON hash is the vector index model key."""
    item = entry(name)
    model = next(f for f in item['files'] if local_name(f) == item['model_file'])
    return {'name': name, 'repo': item['repo'], 'revision': item['revision'], 'file_sha256': model['sha256'],
            'backend': 'onnxruntime-cpu', 'precision': item['precision'], 'pooling': item['pooling'],
            'query_prefix': item['query_prefix'], 'passage_prefix': item['passage_prefix'],
            'dims': item['dims'], 'max_tokens': item['max_tokens']}


def quick_status(name: str, home: Path) -> dict:
    """Presence and sizes only (no hashing); the full SHA-256 check runs in ``verify``."""
    item = entry(name)
    missing, wrong = [], []
    for spec in item['files']:
        path = file_path(name, local_name(spec), home)
        if not path.is_file():
            missing.append(local_name(spec))
        elif path.stat().st_size != spec['size']:
            wrong.append(local_name(spec))
    available = not missing and not wrong
    if available:
        message = 'files present with pinned sizes (SHA-256 checked on load)'
    elif missing:
        message = f'missing: {", ".join(missing)}'
    else:
        message = f'size mismatch: {", ".join(wrong)}'
    return {'name': name, 'available': available, 'real_model': available, 'message': message}


def verify(name: str, home: Path) -> dict[str, str]:
    """Return ``{filename: ok|missing|size_mismatch|sha256_mismatch}``."""
    report = {}
    for spec in entry(name)['files']:
        path = file_path(name, local_name(spec), home)
        if not path.is_file():
            report[local_name(spec)] = 'missing'
        elif path.stat().st_size != spec['size']:
            report[local_name(spec)] = 'size_mismatch'
        else:
            report[local_name(spec)] = 'ok' if sha256_file(path) == spec['sha256'] else 'sha256_mismatch'
    return report


def available(name: str, home: Path) -> bool:
    """Files present with pinned sizes (``quick_status``; no hashing)."""
    return quick_status(name, home)['available']


def import_from(name: str, source: Path, home: Path, *, log=print) -> dict[str, str]:
    """Copy the manifest files of ``name`` from the absolute folder ``source`` into the data home.

    Per file (design M2 §3.3): read ``source/<file name>`` (flat) or ``source/<manifest path>``
    (repository layout); ``missing_in_source`` / ``size_mismatch_in_source`` without copying;
    ``already_verified`` when the target already matches; else copy in 1 MiB blocks to
    ``<home>/tmp/<name>-<file>.partial`` hashing the same blocks, then ``os.replace`` on a SHA-256
    match (``ok``) or delete the partial (``sha256_mismatch_in_source``). ``source`` is only read.
    Returns ``{file name: status}``. Calls no network function.
    """
    raise NotImplementedError('models.import_from: M2 stage 1')


def cli_fetch_model(args, home: Path) -> int:
    """``workmcp fetch-model NAME --from FOLDER`` body (the CLI checks ``--from`` and the data home):
    ``import_from`` and one line per file; exit 0 when every file is ``ok``/``already_verified``."""
    raise NotImplementedError('models.cli_fetch_model: M2 stage 1')


def cli_verify_model(args, home: Path) -> int:
    """``workmcp verify-model [NAME]`` body: one ``file: status`` line per file; 0 when all ``ok``."""
    report = verify(args.name, home)
    for filename, status in report.items():
        print(f'{args.name}/{filename}: {status}')
    return 0 if all(value == 'ok' for value in report.values()) else 1


def require_verified(name: str, home: Path) -> Path:
    bad = {k: v for k, v in verify(name, home).items() if v != 'ok'}
    if bad:
        raise ModelFileError(f'{name}: model files not verified: {bad}')
    return model_dir(name, home)


def download_file(url: str, target: Path, partial: Path, size: int, sha256: str, *, log=print,
                  reconnects: int = 20) -> None:
    """Range-resumable download to ``partial``; atomic move to ``target`` only after size+SHA-256 match.

    Uses ``urllib.request`` (standard proxy variables such as ``HTTPS_PROXY`` apply).
    """
    import urllib.error
    import urllib.request

    target.parent.mkdir(parents=True, exist_ok=True)
    partial.parent.mkdir(parents=True, exist_ok=True)
    if partial.exists() and partial.stat().st_size > size:
        partial.unlink()
    for _ in range(reconnects + 1):
        have = partial.stat().st_size if partial.exists() else 0
        if have >= size:
            break
        request = urllib.request.Request(url, headers={'Range': f'bytes={have}-'} if have else {})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status == 200 and have:
                    log(f'  server ignored Range; restarting {target.name}')
                    have = 0
                elif response.status not in (200, 206):
                    raise ModelFileError(f'{target.name}: HTTP {response.status}')
                done, step = have, max(size//20, 1)
                with open(partial, 'ab' if have else 'wb') as handle:
                    while block := response.read(CHUNK_BYTES):
                        handle.write(block)
                        if (done + len(block))//step != done//step:
                            log(f'  {target.name}: {100*(done+len(block))//size}%')
                        done += len(block)
        except urllib.error.HTTPError as exc:
            raise ModelFileError(f'{target.name}: HTTP {exc.code}') from None
        except (urllib.error.URLError, OSError) as exc:
            log(f'  {target.name}: connection dropped ({type(exc).__name__}); resuming')
    actual_size = partial.stat().st_size if partial.exists() else 0
    if actual_size != size:
        if actual_size > size:
            partial.unlink()
        raise ModelFileError(f'{target.name}: size {actual_size} != {size} (partial kept only if shorter)')
    if sha256_file(partial) != sha256:
        partial.unlink()
        raise ModelFileError(f'{target.name}: SHA-256 mismatch; partial deleted')
    os.replace(partial, target)


def fetch(name: str, home: Path, *, attempts: int = 2, log=print) -> dict[str, str]:
    item = entry(name)
    for spec in item['files']:
        target = file_path(name, local_name(spec), home)
        if target.is_file() and target.stat().st_size == spec['size'] and sha256_file(target) == spec['sha256']:
            log(f'  {target.name}: already verified')
            continue
        url = f"{BASE_URL}/{item['repo']}/resolve/{item['revision']}/{spec['path']}"
        partial = home/'tmp'/f'{name}-{target.name}.partial'
        for attempt in range(1, attempts + 1):
            try:
                log(f'fetch {name}/{target.name} ({spec["size"]:,} bytes), attempt {attempt}')
                download_file(url, target, partial, spec['size'], spec['sha256'], log=log)
                break
            except ModelFileError as exc:
                log(f'  failed: {exc}')
                if attempt == attempts:
                    raise
    return verify(name, home)


def main(argv=None) -> int:
    """``python -m workmcp.models fetch|verify|list [name]`` — the only command that downloads."""
    parser = argparse.ArgumentParser(prog='python -m workmcp.models')
    parser.add_argument('command', choices=['fetch', 'verify', 'list'])
    parser.add_argument('name', nargs='?', default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    if args.command == 'list':
        for name, item in MANIFEST.items():
            total = sum(f['size'] for f in item['files'])
            print(f"{name:12} {item['repo']}@{item['revision'][:12]} {item['precision']} {item['license']} "
                  f"{total/1e9:.2f}GB")
        return 0
    try:
        home = paths.ensure_home(paths.resolve_home())
    except paths.DataPathError as exc:
        print(exc, file=sys.stderr)
        return 2
    report = fetch(args.name, home) if args.command == 'fetch' else verify(args.name, home)
    for filename, status in report.items():
        print(f'{filename}: {status}')
    return 0 if all(v == 'ok' for v in report.values()) else 1


if __name__ == '__main__':
    sys.exit(main())

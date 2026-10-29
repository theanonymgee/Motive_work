"""Offline install bundle for the collector PC (design M6 §3): build it on the development PC, check it on the
company PC. Standard library only; it never imports ``workmcp`` (``tests/test_tools_dir.py``).

    python -I tools/make_bundle.py make --out <folder outside the repository> [--commit HEAD] [--model-from DIR]
                                        [--zip] [--allow-dirty] [--dry-run]
    python -I verify_bundle.py <bundle folder>          (the same file, copied into the bundle)

``make`` needs the development PC's own platform (Windows x64, Python 3.12, with pip: ``uv venv --seed`` or
``python -m ensurepip``). It downloads the wheels pinned in ``requirements.lock.txt`` (``pip download
--only-binary=:all: --require-hashes``), builds the ``workmcp`` wheel from ``git archive`` of the commit (never from
the working tree), and writes into ``<out>/workmcp-bundle-<version>-<yyyymmdd>-<commit8>/``:

    wheels/  requirements.lock.txt  src.zip  verify_bundle.py  INSTALL.txt  SHA256SUMS

``--model-from DIR`` also copies the four model files (SHA-256 checked against the constants below, which a test
keeps equal to ``workmcp.models.MANIFEST``) into ``<out>/workmcp-model-bge-m3-<revision8>/`` with its own
``SHA256SUMS``. The Python installer is not part of a bundle (python.org's signed per-user installer; guide).
Nothing is installed, no setting is changed, no token is read. ``--out`` inside the repository is refused.

``verify`` recomputes every line of ``SHA256SUMS`` (``ok N mismatch M missing K unlisted U``) and fails on any
mismatch, missing file or file that ``SHA256SUMS`` does not list (``INSTALL.txt`` and ``SHA256SUMS`` itself excepted);
a listed path may not leave the bundle folder. Exit codes: 0 done, 1 a step failed or verification failed,
4 arguments or preconditions.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUMS = 'SHA256SUMS'
INSTALL = 'INSTALL.txt'
VERIFY_NAME = 'verify_bundle.py'
LOCK = 'requirements.lock.txt'
UNLISTED_OK = frozenset({SUMS, INSTALL})
BLOCK = 1 << 20
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 4

MODEL_NAME = 'bge-m3'
MODEL_REVISION = '5617a9f61b028005a4858fdac845db406aefb181'
# local file name -> (size, SHA-256); equal to workmcp.models.MANIFEST['bge-m3']['files'] (tests/test_make_bundle.py)
MODEL_FILES = {
    'model.onnx': (724923, 'f84251230831afb359ab26d9fd37d5936d4d9bb5d1d5410e66442f630f24435b'),
    'model.onnx_data': (2266820608, '1eebfb28493f67bba03ce0ef64bfdc7fc5a3bd9d7493f818bb1d78cd798416b4'),
    'Constant_7_attr__value': (65552, 'cdf16f72c5d07b36484056e601ed9687f78477e5d85cee85a34f2406b7fb5906'),
    'tokenizer.json': (17082821, '6710678b12670bc442b99edc952c4d996ae309a7020c1fa0096dd245c2faf790'),
}


class Problem(Exception):
    """A step ends here: one line for stderr and the exit code."""

    def __init__(self, message: str, exit_code: int = EXIT_USAGE):
        super().__init__(message)
        self.exit_code = exit_code


# ---- hashes and SHA256SUMS ---------------------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while block := handle.read(BLOCK):
            digest.update(block)
    return digest.hexdigest()


def bundle_files(folder: Path) -> list[str]:
    """Relative POSIX paths of every file below ``folder`` that ``SHA256SUMS`` has to list."""
    found = [p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()]
    return sorted(name for name in found if name not in UNLISTED_OK)


def write_sums(folder: Path) -> str:
    """Write ``SHA256SUMS`` (``sha256sum`` format, two spaces) for every listed file; return its own SHA-256."""
    lines = [f'{sha256_file(folder/name)}  {name}\n' for name in bundle_files(folder)]
    (folder/SUMS).write_text(''.join(lines), encoding='utf-8', newline='\n')
    return sha256_file(folder/SUMS)


def parse_sums(text: str) -> list[tuple[str, str]]:
    """``(sha256, relative path)`` per line. A path that is absolute, has ``..`` or a drive is refused."""
    entries = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        match = re.fullmatch(r'([0-9a-fA-F]{64}) [ *](.+)', line)
        if not match:
            raise Problem(f'{SUMS} line {number}: not "<sha256>  <path>"', EXIT_FAILED)
        name = match.group(2)
        parts = name.replace('\\', '/').split('/')
        if name.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:', name) or '..' in parts or '' in parts:
            raise Problem(f'{SUMS} line {number}: path leaves the bundle folder: {name}', EXIT_FAILED)
        entries.append((match.group(1).lower(), name.replace('\\', '/')))
    return entries


@dataclass
class Verification:
    ok: int = 0
    mismatch: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unlisted: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.mismatch or self.missing or self.unlisted)

    def summary(self) -> str:
        return f'ok {self.ok} mismatch {len(self.mismatch)} missing {len(self.missing)} unlisted {len(self.unlisted)}'


def verify_folder(folder: Path) -> Verification:
    sums = folder/SUMS
    if not sums.is_file():
        raise Problem(f'no {SUMS} in {folder}', EXIT_FAILED)
    result = Verification()
    listed = set()
    for expected, name in parse_sums(sums.read_text(encoding='utf-8')):
        listed.add(name)
        path = folder/name
        if not path.is_file():
            result.missing.append(name)
        elif sha256_file(path) != expected:
            result.mismatch.append(name)
        else:
            result.ok += 1
    result.unlisted = [name for name in bundle_files(folder) if name not in listed]
    return result


# ---- the lock file -----------------------------------------------------------------------------------------------

@dataclass
class Pin:
    name: str
    version: str
    marker: str | None
    hashes: set[str] = field(default_factory=set)


def normalize(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def parse_lock(text: str) -> dict[str, Pin]:
    """``name==version [; marker] \\`` lines with their ``--hash=sha256:`` lines, keyed by normalized name."""
    pins: dict[str, Pin] = {}
    current = None
    for line in text.splitlines():
        match = re.match(r'^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)\s*(?:;\s*(.*?))?\s*\\?\s*$', line)
        if match:
            current = Pin(normalize(match.group(1)), match.group(2), match.group(3) or None)
            pins[current.name] = current
        elif current is not None:
            current.hashes.update(h.lower() for h in re.findall(r'--hash=sha256:([0-9a-fA-F]{64})', line))
    return pins


def wheel_pin(filename: str) -> tuple[str, str]:
    """``(normalized name, version)`` from a wheel file name ``name-version-tags.whl``."""
    parts = filename[:-4].split('-') if filename.endswith('.whl') else []
    if len(parts) < 5:
        raise Problem(f'not a wheel file name: {filename}', EXIT_FAILED)
    return normalize(parts[0]), parts[1]


def check_wheels(wheels: Path, pins: dict[str, Pin]) -> list[str]:
    """Every downloaded wheel is a pinned version with a hash from the lock; every pin without an environment
    marker has its wheel. Returns the names of marker pins that have no wheel here (other platforms)."""
    seen = set()
    for path in sorted(wheels.glob('*.whl')):
        name, version = wheel_pin(path.name)
        pin = pins.get(name)
        if pin is None or pin.version != version:
            raise Problem(f'wheel not in the lock: {path.name}', EXIT_FAILED)
        if sha256_file(path) not in pin.hashes:
            raise Problem(f'wheel hash not in the lock: {path.name}', EXIT_FAILED)
        seen.add(name)
    missing = [pin.name for pin in pins.values() if pin.name not in seen]
    required = [name for name in missing if pins[name].marker is None]
    if required:
        raise Problem(f'no wheel for: {", ".join(required)}', EXIT_FAILED)
    return missing


# ---- preconditions -----------------------------------------------------------------------------------------------

def is_inside(path: Path, folder: Path) -> bool:
    try:
        return Path(os.path.realpath(path)).is_relative_to(Path(os.path.realpath(folder)))
    except ValueError:
        return False


def out_problem(out: Path, repo: Path = REPO) -> str | None:
    if is_inside(out, repo):
        return f'--out is inside the repository ({repo}); choose a folder outside it'
    return None


def host_problem() -> str | None:
    """The wheels are downloaded for this interpreter, so it has to be the target platform."""
    if sys.platform != 'win32' or platform.machine().upper() != 'AMD64' or sys.version_info[:2] != (3, 12):
        return (f'this interpreter is {sys.platform} {platform.machine()} Python {sys.version_info.major}.'
                f'{sys.version_info.minor}; the bundle is for Windows AMD64 Python 3.12 - run it there')
    return None


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    return env


def pip_problem() -> str | None:
    result = subprocess.run([sys.executable, '-I', '-m', 'pip', '--version'], capture_output=True, text=True,
                            env=child_env())
    if result.returncode:
        return ('this Python has no pip; use one that has it, e.g. "uv venv --seed <folder>" or '
                '"python -m ensurepip", then run this tool with that python')
    return None


def git(*args: str) -> str:
    exe = shutil.which('git')
    if exe is None:
        raise Problem('git was not found on PATH')
    result = subprocess.run([exe, *args], cwd=REPO, capture_output=True, text=True)
    if result.returncode:
        raise Problem(f'git {args[0]} failed: {result.stderr.strip()}', EXIT_FAILED)
    return result.stdout


def resolve_commit(rev: str) -> str:
    return git('rev-parse', '--verify', rev + '^{commit}').strip()


def dirty_problem() -> str | None:
    if git('status', '--porcelain').strip():
        return 'the working tree has uncommitted changes (the bundle is built from a commit); commit, or --allow-dirty'
    return None


# ---- building ----------------------------------------------------------------------------------------------------

def run_step(label: str, command: list[str]) -> None:
    print(f'{label}: {" ".join(command[:6])} ...')
    if subprocess.run(command, env=child_env()).returncode:
        raise Problem(f'{label} failed', EXIT_FAILED)


def export_commit(commit: str, folder: Path) -> Path:
    """``git archive`` of the commit as ``<folder>/src.zip`` (LF files stay LF); the wheel is built from its extraction."""
    archive = folder/'src.zip'
    git('-c', 'core.autocrlf=false', 'archive', '--format=zip', commit, '-o', str(archive))
    return archive


def pip_download(lock: Path, wheels: Path) -> None:
    run_step('pip download', [sys.executable, '-I', '-m', 'pip', 'download', '--only-binary=:all:', '--require-hashes',
                              '--no-input', '--disable-pip-version-check', '-r', str(lock), '-d', str(wheels)])


def build_wheel(source: Path, wheels: Path) -> None:
    run_step('pip wheel', [sys.executable, '-I', '-m', 'pip', 'wheel', '--no-deps', '--no-input',
                           '--disable-pip-version-check', '-w', str(wheels), str(source)])


def copy_verified(source: Path, target: Path, size: int, sha256: str) -> None:
    """Copy in 1 MiB blocks into ``target.partial`` while hashing; replace only when size and SHA-256 match."""
    partial = target.with_name(target.name + '.partial')
    digest, total = hashlib.sha256(), 0
    with open(source, 'rb') as reader, open(partial, 'wb') as writer:
        while block := reader.read(BLOCK):
            digest.update(block)
            total += len(block)
            writer.write(block)
    if total != size or digest.hexdigest() != sha256:
        partial.unlink()
        raise Problem(f'model file {source.name}: size or SHA-256 differs from the manifest', EXIT_FAILED)
    os.replace(partial, target)


def build_model(model_from: Path, out: Path) -> Path:
    target = out/f'workmcp-model-{MODEL_NAME}-{MODEL_REVISION[:8]}'
    if target.exists():
        raise Problem(f'{target} exists already; remove it or choose another --out')
    sources = {}
    for name in MODEL_FILES:
        found = [p for p in (model_from/name, model_from/'onnx'/name) if p.is_file()]
        if not found:
            raise Problem(f'--model-from {model_from}: {name} not found (flat or onnx/ layout)')
        sources[name] = found[0]
    target.mkdir(parents=True)
    try:
        for name, (size, sha256) in MODEL_FILES.items():
            print(f'model: copy and check {name} ({size:,} bytes)')
            copy_verified(sources[name], target/name, size, sha256)
    except Problem:
        shutil.rmtree(target)                        # no half-copied model folder is left to be mistaken for a bundle
        raise
    write_sums(target)
    return target


def install_text(version: str, commit: str, built: str, sums_sha256: str, wheels: int) -> str:
    return '\n'.join([
        f'workmcp offline bundle {version}  commit {commit}  built {built}  wheels {wheels}',
        '',
        'No admin rights needed. Python 3.12 (python.org per-user installer) is not part of this bundle.',
        'Run in PowerShell, in this folder. Use a venv OUTSIDE it, e.g. C:\\Users\\<USER>\\workmcp\\.venv',
        '',
        '1. python -I verify_bundle.py .                      (last line must read: ok N mismatch 0 missing 0 unlisted 0)',
        f'2. certutil -hashfile SHA256SUMS SHA256               (must equal {sums_sha256})',
        '3. py -3.12 -m venv C:\\Users\\<USER>\\workmcp\\.venv',
        '4. C:\\Users\\<USER>\\workmcp\\.venv\\Scripts\\python.exe -I -m pip install --no-index --find-links wheels '
        '--require-hashes -r requirements.lock.txt',
        f'5. C:\\Users\\<USER>\\workmcp\\.venv\\Scripts\\python.exe -I -m pip install --no-index --find-links wheels '
        f'--no-deps workmcp=={version}',
        '6. C:\\Users\\<USER>\\workmcp\\.venv\\Scripts\\python.exe -I -m pip check',
        '7. C:\\Users\\<USER>\\workmcp\\.venv\\Scripts\\python.exe -I -m workmcp setup --home C:\\Users\\<USER>\\Motive_work '
        '--model-from <model bundle folder> --push-url https://<VPS_HOST>/workmcp',
        '',
        'Never turn pip hash checking off. src.zip is for the test diagnosis only; do not use its folder as the data home.',
        'Full guide: doc/guides/2026-10-29_회사PC-설치안내서_v1.md (in src.zip).',
        '',
    ])


def zip_folder(folder: Path, target: Path, *, compress: bool) -> None:
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(target, 'w', mode, allowZip64=True) as archive:
        for path in sorted(folder.rglob('*')):
            if path.is_file():
                archive.write(path, (Path(folder.name)/path.relative_to(folder)).as_posix())


def megabytes(path: Path) -> str:
    return f'{sum(p.stat().st_size for p in ([path] if path.is_file() else path.rglob("*")) if p.is_file())/1e6:.1f} MB'


def preconditions(args) -> tuple[Path, str]:
    """Check everything that needs no download; return the output folder and the full commit hash."""
    out = Path(args.out).expanduser()
    if not out.is_absolute():
        raise Problem('--out must be an absolute path')
    if problem := out_problem(out):
        raise Problem(problem)
    if args.model_from and not Path(args.model_from).is_dir():
        raise Problem(f'--model-from is not a folder: {args.model_from}')
    commit = resolve_commit(args.commit)
    if not args.allow_dirty and (problem := dirty_problem()):
        raise Problem(problem)
    if problem := host_problem():
        raise Problem(problem)
    if problem := pip_problem():
        raise Problem(problem)
    return out, commit


def make(args) -> int:
    out, commit = preconditions(args)
    pins = parse_lock(git('show', f'{commit}:{LOCK}'))
    version_text = git('show', f'{commit}:pyproject.toml')
    version = (re.search(r'(?m)^version\s*=\s*"([^"]+)"', version_text) or [None, '0'])[1]
    name = f'workmcp-bundle-{version}-{datetime.now():%Y%m%d}-{commit[:8]}'
    folder = out/name
    if args.dry_run:
        print(f'dry run: nothing is downloaded or written. checks passed: out outside the repository, commit {commit[:8]} '
              f'resolved, host Windows AMD64 Python 3.12, pip present.')
        print(f'would write: {folder}  (lock pins {len(pins)}, pip download --only-binary=:all: --require-hashes, '
              f'pip wheel --no-deps from git archive {commit[:8]}, src.zip, {VERIFY_NAME}, {INSTALL}, {SUMS})')
        if args.model_from:
            print(f'would write: {out/f"workmcp-model-{MODEL_NAME}-{MODEL_REVISION[:8]}"}  (4 files, '
                  f'{sum(s for s, _ in MODEL_FILES.values()):,} bytes, SHA-256 checked while copying)')
        return EXIT_OK
    if folder.exists():
        raise Problem(f'{folder} exists already; remove it or choose another --out')
    wheels = folder/'wheels'
    wheels.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix='workmcp-bundle-') as temp:
        source = Path(temp)/'src'
        archive = export_commit(commit, folder)
        with zipfile.ZipFile(archive) as opened:
            opened.extractall(source)
        shutil.copyfile(source/LOCK, folder/LOCK)
        pip_download(folder/LOCK, wheels)
        absent = check_wheels(wheels, pins)             # the lock's wheels only; the project wheel is not in the lock
        build_wheel(source, wheels)
    shutil.copyfile(Path(__file__).resolve(), folder/VERIFY_NAME)
    count = len(list(wheels.glob('*.whl')))
    built = datetime.now().strftime('%Y-%m-%d')
    sums_sha256 = write_sums(folder)
    (folder/INSTALL).write_text(install_text(version, commit[:8], built, sums_sha256, count), encoding='utf-8',
                                newline='\n')
    result = verify_folder(folder)
    if not result.clean:
        raise Problem(f'the bundle did not verify: {result.summary()}', EXIT_FAILED)
    print(f'bundle: wheels {count} (lock pins {len(pins)}; no wheel here for marker pins: {", ".join(absent) or "none"}), '
          f'src.zip {megabytes(folder/"src.zip")} -> {folder} {megabytes(folder)}')
    print(f'{SUMS} sha256 {sums_sha256}  (compare on the company PC with: certutil -hashfile {SUMS} SHA256)')
    if args.zip:
        target = out/f'{name}.zip'
        zip_folder(folder, target, compress=True)
        print(f'zip: {target} {megabytes(target)}')
    if args.model_from:
        model = build_model(Path(args.model_from), out)
        print(f'model: 4 files -> {model} {megabytes(model)}')
    return EXIT_OK


def verify(args) -> int:
    result = verify_folder(Path(args.folder).expanduser())
    for label, names in (('mismatch', result.mismatch), ('missing', result.missing), ('unlisted', result.unlisted)):
        for name in names:
            print(f'{label}: {name}')
    print(result.summary())
    return EXIT_OK if result.clean else EXIT_FAILED


def parser() -> argparse.ArgumentParser:
    main_parser = argparse.ArgumentParser(prog='make_bundle.py', description='offline install bundle (design M6 §3)')
    sub = main_parser.add_subparsers(dest='command', required=True)
    make_parser = sub.add_parser('make', help='build the bundle on the development PC (needs network for pip)')
    make_parser.add_argument('--out', required=True, metavar='FOLDER', help='absolute folder outside the repository')
    make_parser.add_argument('--commit', default='HEAD', metavar='REV', help='commit to bundle (default HEAD)')
    make_parser.add_argument('--model-from', metavar='DIR', help='also copy the 4 model files from DIR (checked)')
    make_parser.add_argument('--zip', action='store_true', help='also write <bundle>.zip next to the folder')
    make_parser.add_argument('--allow-dirty', action='store_true', help='build although the working tree is dirty')
    make_parser.add_argument('--dry-run', action='store_true', help='run the checks and show what would be written')
    verify_parser = sub.add_parser('verify', help='check every line of SHA256SUMS (and unlisted files)')
    verify_parser.add_argument('folder', metavar='BUNDLE_FOLDER', nargs='?', default='.')
    return main_parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if Path(sys.argv[0]).name == VERIFY_NAME and (not argv or argv[0] not in ('make', 'verify', '-h', '--help')):
        argv = ['verify', *argv]                      # python -I verify_bundle.py .
    args = parser().parse_args(argv)
    try:
        return make(args) if args.command == 'make' else verify(args)
    except Problem as exc:
        print(f'error: {exc}', file=sys.stderr)
        return exc.exit_code


if __name__ == '__main__':
    sys.exit(main())

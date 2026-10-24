"""Make the synthetic data home for the M3 client connection check (design M3 §11.4 step 0). A manual
script, not a test module: its name does not match ``test_*.py``, so the default pytest run never
collects it.

    uv run python tests/probes/make_m3_home.py [--home DIR] [--model-from FOLDER] [--allow-appdata]

- ``--home``: the data home (default ``%USERPROFILE%\\Motive_work_m3test``). It must follow the data home
  rules and lie outside ``AppData`` (app virtualization, design M3 §10), unless ``--allow-appdata``.
- The synthetic documents (``fixtures.bundle`` and ``fixtures.corpus``) are written to the sibling folder
  ``<home>_src`` (a source root may not lie inside the data home, ``config.validate_root``); an existing
  folder is left as it is, so a second run scans unchanged files.
- Then, in this process, with ``WORKMCP_HOME`` set to the home: ``workmcp scan --root <src>``, optionally
  ``workmcp fetch-model bge-m3 --from FOLDER`` (a folder on this PC that already holds the model, e.g. the
  app-virtualized home ``%LOCALAPPDATA%\\Packages\\<app>\\LocalCache\\Local\\Motive_work\\models\\bge-m3\\<rev>``),
  ``workmcp index`` (lexical only without the model) and ``workmcp doctor``; the doctor output is also
  saved to ``<home>/logs/m3-doctor.txt`` for the evidence document.

Synthetic data only; nothing leaves the PC; the script starts no process and opens no network.
"""
import argparse
import contextlib
import io
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                  # tests/ (fixtures)
sys.path.insert(0, str(HERE.parents[2]/'src'))

from fixtures import bundle, corpus  # noqa: E402
from workmcp import cli, models, paths  # noqa: E402

DEFAULT_NAME = 'Motive_work_m3test'


def default_home() -> str:
    profile = os.environ.get('USERPROFILE') or str(Path.home())
    return str(Path(profile)/DEFAULT_NAME)


def run(*argv: str) -> tuple[int, str]:
    """``workmcp <argv>`` in this process; returns the exit code and what it printed (also shown)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(argv))
    text = out.getvalue()
    print(f'$ workmcp {" ".join(argv)}  -> exit {code}')
    print(text, end='' if text.endswith('\n') or not text else '\n')
    return code, text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--home', default=default_home(), help='data home (default: %(default)s)')
    parser.add_argument('--model-from', metavar='FOLDER', help='copy the bge-m3 files from this folder first')
    parser.add_argument('--allow-appdata', action='store_true', help='accept a home under AppData (not advised)')
    args = parser.parse_args(argv)

    try:
        home = paths.validate_home(args.home, os.environ, paths.find_repo_root())
    except paths.DataPathError as exc:
        print(exc, file=sys.stderr)
        return 2
    probe = paths.virtualization_probe(home, os.environ)
    if probe['under_appdata'] and not args.allow_appdata:
        print(f'refused: {home} is under AppData; app virtualization may give each client another home '
              f'(design M3 §10). Choose a home outside AppData or pass --allow-appdata.', file=sys.stderr)
        return 2
    paths.ensure_home(home)
    src = home.parent/f'{home.name}_src'
    if src.exists():
        print(f'sources: {src} exists, left as it is')
    else:
        bundle.write_bundle(src/'bundle')
        corpus.write(corpus.build(), src/'corpus')
        print(f'sources: synthetic bundle and corpus written to {src}')

    os.environ[paths.HOME_ENV] = str(home)
    code, _ = run('scan', '--root', str(src))
    if code != 0:
        return code
    if args.model_from:
        code, _ = run('fetch-model', models.DEFAULT_MODEL, '--from', args.model_from)
        if code != 0:
            return code
    code, _ = run('index')
    if code != 0:
        return code
    code, doctor = run('doctor')
    (home/'logs'/'m3-doctor.txt').write_text(doctor, encoding='utf-8')
    print(f'\nWORKMCP_HOME for the client configs (.mcp.json env, .codex/config.toml [mcp_servers.workmcp.env]): '
          f'{home}')
    return code


if __name__ == '__main__':
    sys.exit(main())

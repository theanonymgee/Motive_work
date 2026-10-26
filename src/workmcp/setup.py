"""``workmcp setup``: data home, ``config.toml``, model, client registration lines, ``doctor``, optional
``scan``/``index``, and ``--upgrade`` (design M5 §2.3, §12). Idempotent: a second run changes nothing.

Stage 0 fixes the contract only: ``Options`` (one field per CLI argument), ``check_options`` (argument
rules, no file system), the result shape (``RESULT_KEYS``, ``STEPS``, ``STEP_KEYS``, ``STEP_DETAIL_KEYS``)
and the exit codes. The step bodies (``apply``, ``plan`` and the helpers below them) come in stage 1.

Boundaries (design M5 §2.3, §9.3): this module starts no process (the ``claude mcp add`` / ``codex mcp
add`` lines are printed for the user to run), never writes the clients' global files (it only reads
key names there), writes only under the data home and, with ``--scope project``, under ``--project``,
and reads no source file. ``download_model`` is the one caller of ``models.fetch`` outside ``models``
(``tests/test_network_boundary.py`` allows exactly this); only ``cli.py`` may import this module.
"""
from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import models

EXIT_OK, EXIT_FAILED, EXIT_HOME, EXIT_LOCKED, EXIT_USAGE = 0, 1, 2, 3, 4
MODEL_CHOICES = ('download', 'none')
SCOPES = ('user', 'project')
CLIENTS = ('claude', 'codex')
SERVER_NAME = 'workmcp'  # the MCP server name in both clients

# apply()/plan() result: {'exit_code': int, 'dry_run': bool, 'home': str | None, 'steps': {step: {...}}}.
RESULT_KEYS = ('exit_code', 'dry_run', 'home', 'steps')
STEPS = ('home', 'config', 'model', 'clients', 'doctor', 'scan', 'index', 'upgrade')
# Every step: status, whether files were (or, in a dry run, would be) written, the human lines it
# printed (paths, counts, statuses only; never document text), and step-specific detail.
STEP_KEYS = ('status', 'changed', 'lines', 'detail')
STEP_STATUSES = ('done', 'planned', 'skipped', 'refused', 'failed')
STEP_DETAIL_KEYS = {
    'home': ('path', 'created', 'under_appdata'),
    'config': ('file', 'written', 'sources', 'missing_sources', 'model'),
    'model': ('name', 'choice', 'report'),                    # report: {file: status} of import_from/fetch/verify
    'clients': ('scope', 'clients', 'commands', 'files', 'registered'),  # registered: {client: True|False|None}
    'doctor': ('exit_code',),
    'scan': ('exit_code',),
    'index': ('exit_code',),
    'upgrade': ('backup', 'schema_from', 'schema_to'),
}


@dataclass(frozen=True)
class Options:
    """One field per ``workmcp setup`` argument (design M5 §2.3 + §12)."""
    home: str | None = None                 # --home PATH (required; checked by check_options)
    sources: tuple[str, ...] = ()           # --source PATH, repeatable
    model: str | None = None                # --model download|none
    model_from: str | None = None           # --model-from DIR (an earlier download or an offline bundle)
    clients: tuple[str, ...] = CLIENTS      # --clients claude,codex
    scope: str = 'user'                     # --scope user|project
    project: str | None = None              # --project DIR (with --scope project)
    scan: bool = False                      # --scan
    index: bool = False                     # --index
    upgrade: bool = False                   # --upgrade: backup -> migrate -> doctor; other steps skipped
    yes: bool = False                       # --yes: overwrite a differing project file, accept an AppData home


def options_from_args(args) -> Options:
    """``Options`` from the ``argparse`` namespace of ``workmcp setup``."""
    return Options(home=args.home, sources=tuple(args.source or ()), model=args.model, model_from=args.model_from,
                   clients=tuple(part.strip() for part in args.clients.split(',')), scope=args.scope,
                   project=args.project, scan=args.scan, index=args.index, upgrade=args.upgrade, yes=args.yes)


def check_options(options: Options) -> str | None:
    """The first argument rule ``options`` breaks (one line for stderr, exit 4), else None. No file access."""
    if not options.home:
        return r'--home PATH is required (for example %USERPROFILE%\Motive_work)'
    if not options.upgrade:
        if options.model is not None and options.model_from is not None:
            return '--model and --model-from exclude each other'
        if options.model is None and options.model_from is None:
            return '--model download|none or --model-from DIR is required'
        if options.model is not None and options.model not in MODEL_CHOICES:
            return f'--model must be one of {", ".join(MODEL_CHOICES)}'
        if options.model_from is not None and not Path(options.model_from).is_absolute():
            return f'--model-from must be an absolute folder: {options.model_from}'
    if options.scope not in SCOPES:
        return f'--scope must be one of {", ".join(SCOPES)}'
    if options.scope == 'project' and not options.project:
        return '--scope project needs --project DIR'
    if options.scope == 'user' and options.project:
        return '--project DIR needs --scope project'
    if options.project and not Path(options.project).is_absolute():
        return f'--project must be an absolute folder: {options.project}'
    if not options.clients or len(set(options.clients)) != len(options.clients) \
            or not set(options.clients) <= set(CLIENTS):
        return f'--clients must be a comma-separated subset of {",".join(CLIENTS)}'
    return None


def apply(options: Options, *, env: Mapping[str, str] | None = None, python: str | None = None,
          log=print) -> dict:
    """Run the steps in ``STEPS`` order and return the result (``RESULT_KEYS``, ``dry_run`` False).

    ``env`` defaults to ``os.environ`` (home rules, AppData check, the clients' global file locations);
    ``python`` to ``sys.executable`` (the interpreter the client command lines name, with ``-I``).
    ``log`` gets each human line as it is printed. (Not named ``run``: the network boundary test reserves
    ``run(...)`` for ``run(transport="stdio")``.) Stage 1.
    """
    raise NotImplementedError('workmcp setup: not implemented yet (M5 stage 1)')


def plan(options: Options, *, env: Mapping[str, str] | None = None, python: str | None = None) -> dict:
    """Dry run: the same result shape as ``apply`` with ``dry_run`` True and status ``planned`` where a step
    would write; creates, changes and deletes nothing (not even the data home). Stage 1."""
    raise NotImplementedError('workmcp setup dry run: not implemented yet (M5 stage 1)')


def config_template(sources: tuple[str, ...], model: str) -> str:
    """Text of a new ``config.toml`` (``[[sources]]`` per root, ``[index] model``); never edits a file. Stage 1."""
    raise NotImplementedError


def client_commands(home: Path, python: str, clients: tuple[str, ...]) -> list[str]:
    """User-scope registration lines (``claude mcp add --scope user ...``, ``codex mcp add ...``) naming
    ``python -I -m workmcp serve`` and ``WORKMCP_HOME``; printed, never run. Stage 1."""
    raise NotImplementedError


def project_files(home: Path, python: str, project: Path, clients: tuple[str, ...]) -> dict[Path, str]:
    """``{path: text}`` of ``<project>/.mcp.json`` and ``<project>/.codex/config.toml`` (``--scope project``). Stage 1."""
    raise NotImplementedError


def registered(env: Mapping[str, str], clients: tuple[str, ...]) -> dict[str, bool | None]:
    """Whether the clients' global files already name ``SERVER_NAME`` (read only; None when unreadable). Stage 1."""
    raise NotImplementedError


def backup_store(home: Path) -> Path | None:
    """``--upgrade``: checkpoint and copy ``work.sqlite3`` to ``db/backup-...-v<schema>.sqlite3`` in blocks. Stage 1."""
    raise NotImplementedError


def download_model(name: str, home: Path, *, log=print) -> dict[str, str]:
    """``--model download``: the pinned files from Hugging Face (``models.fetch``), then ``{file: status}``."""
    return models.fetch(name, home, log=log)


def main(args) -> int:
    """``workmcp setup`` body: arguments (exit 4) -> ``apply`` -> its exit code."""
    options = options_from_args(args)
    if error := check_options(options):
        print(error, file=sys.stderr)
        return EXIT_USAGE
    try:
        return apply(options)['exit_code']
    except NotImplementedError as exc:
        print(exc, file=sys.stderr)
        return EXIT_FAILED

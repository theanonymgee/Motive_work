"""``workmcp setup``: data home, ``config.toml``, model, client registration lines, ``doctor``, optional
``scan``/``index``, and ``--upgrade`` (design M6 §2, which takes precedence over design M5 §2.3, §12).
Idempotent and non-interactive: every decision is an argument, the one confirmation is ``--yes``, and a
second run changes nothing.

Steps (``STEPS``): ``home`` (rules of ``paths.validate_home``; an AppData home only with ``--yes``) ->
``config`` (a new ``config.toml`` from ``config_template`` only when there is none; an existing file is never
edited, the lines to add are printed) -> ``model`` (``--model download`` through ``models.fetch``, the one
network step; ``--model-from DIR`` through ``models.import_from``, no network; ``--model none``) ->
``clients`` (registration command lines printed, or with ``--scope project`` the project files written) ->
``doctor`` -> ``scan``/``index`` (only when asked). ``--upgrade`` runs ``home`` -> ``upgrade`` (scan, index and
push locks held; ``backup_store``; ``store.open_home`` migrates) -> ``doctor`` and skips the rest.
``--dry-run`` (``plan``) checks the same rules and reports ``planned`` where a run would write; it creates,
changes and deletes nothing. Every argument and source folder is checked before anything is created.

Boundaries (design M6 §2.1, §2.4, M5 §9.3): this module starts no process (the ``claude mcp add`` / ``codex
mcp add`` lines are printed for the user to run), never writes the clients' global files (``registered``
only reads whether ``workmcp`` is a key there), writes only under the data home and, with ``--scope
project``, the two files under ``--project``, and reads no source file. It never reads, makes or prints a
token: only whether ``WORKMCP_PUSH_TOKEN`` is set and whether ``<home>/push.token`` exists. ``download_model``
is the one caller of ``models.fetch`` outside ``models`` (``tests/test_network_boundary.py`` allows exactly
this); only ``cli.py`` may import this module, and ``cli`` hands in its ``doctor``/``scan``/``index`` bodies
(``commands``), so this module never imports ``cli``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config, models, paths, push, store, sync

EXIT_OK, EXIT_FAILED, EXIT_HOME, EXIT_LOCKED, EXIT_USAGE = 0, 1, 2, 3, 4
MODEL_CHOICES = ('download', 'none')
SCOPES = ('user', 'project')
CLIENTS = ('claude', 'codex')
NO_CLIENTS = 'none'          # --clients none: no registration step (design M6 §2.2)
SERVER_NAME = 'workmcp'      # the MCP server name in both clients
MCP_TOKEN_ENV = 'WORKMCP_MCP_TOKEN'
MCP_TOKEN_PLACEHOLDER = '<MCP_TOKEN>'   # printed in place of the token, which setup never sees
SERVE_ARGS = ('-I', '-m', 'workmcp', 'serve')

# apply()/plan() result: {'exit_code': int, 'dry_run': bool, 'home': str | None, 'steps': {step: {...}}}.
RESULT_KEYS = ('exit_code', 'dry_run', 'home', 'steps')
STEPS = ('home', 'config', 'model', 'clients', 'doctor', 'scan', 'index', 'upgrade')
# Every step: status, whether files were (or, in a dry run, would be) written, the human lines it
# printed (paths, counts, statuses only; never document text or a token), and step-specific detail.
# doctor/scan/index print their own output; their ``lines`` hold setup's header line only.
STEP_KEYS = ('status', 'changed', 'lines', 'detail')
STEP_STATUSES = ('done', 'planned', 'skipped', 'refused', 'failed')
STEP_DETAIL_KEYS = {
    'home': ('path', 'created', 'under_appdata'),
    # push_token: 'environment' | 'file' | 'missing' | None (no push url): where it is, never its value
    'config': ('file', 'written', 'sources', 'missing_sources', 'model', 'push_url', 'push_token'),
    'model': ('name', 'choice', 'report'),                    # report: {file: status} of import_from/fetch/verify
    'clients': ('scope', 'clients', 'commands', 'files', 'registered'),  # registered: {client: True|False|None}
    'doctor': ('exit_code',),
    'scan': ('exit_code',),
    'index': ('exit_code',),
    'upgrade': ('backup', 'schema_from', 'schema_to'),
}

Command = Callable[[Path], int]   # cli's doctor/scan/index body run on the given data home


@dataclass(frozen=True)
class Options:
    """One field per ``workmcp setup`` argument (design M5 §2.3 + §12, M6 §2.2)."""
    home: str | None = None                 # --home PATH (required; checked by check_options)
    sources: tuple[str, ...] = ()           # --source PATH, repeatable
    model: str | None = None                # --model download|none
    model_from: str | None = None           # --model-from DIR (an earlier download or an offline bundle)
    clients: tuple[str, ...] = CLIENTS      # --clients claude,codex | --clients none
    scope: str = 'user'                     # --scope user|project
    project: str | None = None              # --project DIR (with --scope project)
    scan: bool = False                      # --scan
    index: bool = False                     # --index
    upgrade: bool = False                   # --upgrade: backup -> migrate -> doctor; other steps skipped
    yes: bool = False                       # --yes: overwrite a differing project file, accept an AppData home
    push_url: str | None = None             # --push-url https://<host>/workmcp (collector role)
    dry_run: bool = False                   # --dry-run: plan() instead of apply()


def options_from_args(args) -> Options:
    """``Options`` from the ``argparse`` namespace of ``workmcp setup``."""
    return Options(home=args.home, sources=tuple(args.source or ()), model=args.model, model_from=args.model_from,
                   clients=tuple(part.strip() for part in args.clients.split(',')), scope=args.scope,
                   project=args.project, scan=args.scan, index=args.index, upgrade=args.upgrade, yes=args.yes,
                   push_url=args.push_url, dry_run=args.dry_run)


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
    if options.clients != (NO_CLIENTS,) and (
            not options.clients or len(set(options.clients)) != len(options.clients)
            or not set(options.clients) <= set(CLIENTS)):
        return f'--clients must be a comma-separated subset of {",".join(CLIENTS)}, or {NO_CLIENTS}'
    if options.push_url is not None:
        if options.push_url.lower().startswith('http://'):
            return '--push-url must start with https:// (an http:// test server is set by hand in config.toml)'
        if problem := sync.url_problem(options.push_url):
            return f'--push-url: {problem}'
    return None


def apply(options: Options, *, env: Mapping[str, str] | None = None, python: str | None = None,
          log=print, commands: Mapping[str, Command] | None = None, opener=None) -> dict:
    """Run the steps and return the result (``RESULT_KEYS``, ``dry_run`` False).

    ``env`` defaults to ``os.environ`` (home rules, AppData check, push token presence, the clients' global
    file locations); ``python`` to ``sys.executable`` (the interpreter the client command lines name, with
    ``-I``). ``log`` gets each human line as it is printed. ``commands`` maps ``doctor``/``scan``/``index`` to
    cli's bodies (a missing one is ``skipped``). ``opener`` goes to ``models.fetch`` (tests hand in fake
    responses). (Not named ``run``: the network boundary test reserves ``run(...)`` for transports.)
    """
    return _Setup(options, env, python, log, commands, opener, dry_run=False).result()


def plan(options: Options, *, env: Mapping[str, str] | None = None, python: str | None = None, log=print,
         commands: Mapping[str, Command] | None = None) -> dict:
    """Dry run: the same checks and result shape as ``apply`` with ``dry_run`` True and status ``planned``
    where a step would write; creates, changes and deletes nothing (not even the data home), runs no
    command and reaches no network."""
    return _Setup(options, env, python, log, commands, None, dry_run=True).result()


# --- templates and command lines (strings only) ---------------------------------------------------------

def toml_string(text: str) -> str:
    """A TOML string for ``text``: a literal string (backslashes as they are) unless it holds ``'`` or a
    control character, then a basic string with ``\\``, ``"`` and control characters escaped."""
    controls = any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in text)
    if "'" not in text and not controls:
        return f"'{text}'"
    escaped = text.replace('\\', '\\\\').replace('"', '\\"')
    return '"' + ''.join(f'\\u{ord(ch):04x}' if ord(ch) < 0x20 or ord(ch) == 0x7f else ch for ch in escaped) + '"'


def config_template(sources: tuple[str, ...], model: str, push_url: str | None = None) -> str:
    """Text of a new ``config.toml`` (``[[sources]]`` per root, ``[index] model``, ``[push] url`` when given);
    never edits a file. The one TOML text this package writes (design M5 §2.3: no general TOML writer)."""
    lines = ['# workmcp settings, written once by "workmcp setup"; setup never changes this file again.',
             '# Edit by hand (Notepad). Secrets never go here: the push token lives in push.token or',
             f'# the environment variable {paths.PUSH_TOKEN_ENV}.', '']
    for source in sources:
        lines += ['[[sources]]', f'path = {toml_string(source)}', '']
    if not sources:
        lines += ["# [[sources]]", "# path = 'C:\\folder\\to\\collect'", '']
    lines += ['[index]', f'model = {toml_string(model)}', '']
    if push_url:
        lines += ['[push]', f'url = {toml_string(push_url)}', '']
    return '\n'.join(lines)


def mcp_url(push_url: str) -> str:
    """The operating server's MCP endpoint under its base URL (``<prefix>/mcp``, config ``[server] http_path``)."""
    return f"{push_url.rstrip('/')}/mcp"


def _quoted(text: str) -> str:
    """One shell argument: quoted when it holds a space or a shell-special character."""
    return f'"{text}"' if any(ch.isspace() or ch in '&()^;' for ch in text) else text


def client_commands(home: Path, python: str, clients: tuple[str, ...], url: str | None = None) -> list[str]:
    """User-scope registration lines, printed and never run. ``url`` None: the local stdio server
    (``python -I -m workmcp serve`` with ``WORKMCP_HOME``); else the remote MCP endpoint (collector role):
    Claude Code with a ``<MCP_TOKEN>`` placeholder header, Codex with ``--bearer-token-env-var``."""
    lines = []
    if url is None:
        serve = ' '.join([_quoted(python), *SERVE_ARGS])
        variable = _quoted(f'{paths.HOME_ENV}={home}')
        if 'claude' in clients:
            lines.append(f'claude mcp add --scope user --transport stdio -e {variable} {SERVER_NAME} -- {serve}')
        if 'codex' in clients:
            lines.append(f'codex mcp add {SERVER_NAME} --env {variable} -- {serve}')
        return lines
    if 'claude' in clients:
        lines.append(f'claude mcp add --transport http --scope user {SERVER_NAME} {url} '
                     f'--header "Authorization: Bearer {MCP_TOKEN_PLACEHOLDER}"')
    if 'codex' in clients:
        lines.append(f'codex mcp add {SERVER_NAME} --url {url} --bearer-token-env-var {MCP_TOKEN_ENV}')
    return lines


def project_files(home: Path, python: str, project: Path, clients: tuple[str, ...],
                  url: str | None = None) -> dict[Path, str]:
    """``{path: text}`` of ``<project>/.mcp.json`` (claude) and ``<project>/.codex/config.toml`` (codex) for
    ``--scope project``: the ``doc/examples`` templates with this PC's values. ``url`` None: local stdio;
    else the remote server with the token taken from ``WORKMCP_MCP_TOKEN`` (never written into a file)."""
    files = {}
    if 'claude' in clients:
        if url is None:
            server = {'command': python, 'args': list(SERVE_ARGS), 'env': {paths.HOME_ENV: str(home)},
                      'timeout': 120000}
        else:
            server = {'type': 'http', 'url': url, 'headers': {'Authorization': f'Bearer ${{{MCP_TOKEN_ENV}}}'}}
        files[project/'.mcp.json'] = json.dumps({'mcpServers': {SERVER_NAME: server}}, indent=2) + '\n'
    if 'codex' in clients:
        if url is None:
            text = (f'[mcp_servers.{SERVER_NAME}]\ncommand = {toml_string(python)}\n'
                    f'args = [{", ".join(json.dumps(arg) for arg in SERVE_ARGS)}]\ntool_timeout_sec = 120\n\n'
                    f'[mcp_servers.{SERVER_NAME}.env]\n{paths.HOME_ENV} = {toml_string(str(home))}\n')
        else:
            text = (f'[mcp_servers.{SERVER_NAME}]\nurl = {toml_string(url)}\n'
                    f'bearer_token_env_var = "{MCP_TOKEN_ENV}"\nstartup_timeout_sec = 30\ntool_timeout_sec = 120\n')
        files[project/'.codex'/'config.toml'] = text
    return files


def client_files(env: Mapping[str, str]) -> dict[str, Path | None]:
    """Where each client keeps its user-scope settings: ``~/.claude.json``, ``$CODEX_HOME`` or
    ``~/.codex`` + ``config.toml`` (None when the user folder is unknown)."""
    base = env.get('USERPROFILE') or env.get('HOME')
    codex_home = env.get('CODEX_HOME') or (str(Path(base)/'.codex') if base else None)
    return {'claude': Path(base)/'.claude.json' if base else None,
            'codex': Path(codex_home)/'config.toml' if codex_home else None}


def _server_names(file: Path, text: str) -> list[str] | None:
    """The MCP server names a client settings text defines (key names only; values are dropped)."""
    try:
        data = json.loads(text) if file.suffix == '.json' else tomllib.loads(text)
    except ValueError:  # json.JSONDecodeError and tomllib.TOMLDecodeError
        return None
    servers = data.get('mcpServers' if file.suffix == '.json' else 'mcp_servers') if isinstance(data, dict) else None
    return sorted(servers) if isinstance(servers, dict) else []


def registered(env: Mapping[str, str], clients: tuple[str, ...]) -> dict[str, bool | None]:
    """Whether the clients' global files already name ``SERVER_NAME`` (read only; None when the file is
    missing or unreadable). Only key names are looked at; headers, tokens and paths are dropped unread."""
    files, result = client_files(env), {}
    for client in clients:
        file = files.get(client)
        try:
            names = _server_names(file, file.read_text(encoding='utf-8-sig')) if file is not None else None
        except (OSError, ValueError):
            names = None
        result[client] = None if names is None else SERVER_NAME in names
    return result


# --- store backup ----------------------------------------------------------------------------------------

def schema_version(db_file: Path) -> int | None:
    """``MAX(version)`` of ``schema_migrations`` read through a read-only connection; None when unreadable."""
    try:
        db = sqlite3.connect(f'{Path(db_file).as_uri()}?mode=ro', uri=True)
        try:
            return db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
        finally:
            db.close()
    except sqlite3.Error:
        return None


def backup_store(home: Path, *, at: datetime | None = None) -> Path | None:
    """``--upgrade``: copy ``work.sqlite3`` to ``db/backup-YYYYMMDD-HHMMSS-v<schema>.sqlite3`` with SQLite's
    online backup (a consistent snapshot, WAL content included); None when there is no store. An existing
    backup of the same second is never overwritten; a failed copy leaves no file."""
    source = paths.db_path(home)
    if not source.is_file():
        return None
    at = at or datetime.now()
    target = source.parent/f'backup-{at:%Y%m%d-%H%M%S}-v{schema_version(source)}.sqlite3'
    if target.exists():
        raise FileExistsError(f'backup already exists: {target}')
    reader = sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True)
    try:
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        reader.close()
    return target


def download_model(name: str, home: Path, *, log=print, opener=None) -> dict[str, str]:
    """``--model download``: the pinned files from Hugging Face (``models.fetch``), then ``{file: status}``.
    The only network step of ``setup``; ``opener`` lets a test hand in fake responses."""
    return models.fetch(name, home, log=log, opener=opener)


# --- the steps -------------------------------------------------------------------------------------------

class _Refused(Exception):
    """A step refused the run before anything was written: ``code`` is the exit code."""

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


class _Setup:
    """One ``apply``/``plan`` run: the steps in order, each recorded in ``self.steps``."""

    def __init__(self, options: Options, env, python, log, commands, opener, *, dry_run: bool):
        self.options, self.dry_run, self.log, self.opener = options, dry_run, log, opener
        self.env = os.environ if env is None else env
        self.python = python or sys.executable
        self.commands = commands or {}
        self.home: Path | None = None
        self.repo = paths.find_repo_root()
        self.steps = {step: {'status': 'skipped', 'changed': False, 'lines': [],
                             'detail': dict.fromkeys(STEP_DETAIL_KEYS[step])} for step in STEPS}

    def record(self, step: str, status: str, lines=(), *, changed: bool = False, **detail) -> None:
        if unknown := set(detail) - set(STEP_DETAIL_KEYS[step]):
            raise KeyError(f'unknown {step} detail {sorted(unknown)}')
        lines = list(lines)
        for line in lines:
            self.log(line)
        self.steps[step] = {'status': status, 'changed': changed, 'lines': lines,
                            'detail': {key: detail.get(key) for key in STEP_DETAIL_KEYS[step]}}

    def result(self) -> dict:
        self.log('workmcp setup' + (' (dry run: nothing is written)' if self.dry_run else ''))
        try:
            if error := check_options(self.options):
                self.log(error)
                raise _Refused(EXIT_USAGE)
            self.check_home()
            if self.options.upgrade:
                self.prepare_home()
                self.upgrade()
                self.doctor()
            else:
                specs, existing = self.check_inputs()
                self.prepare_home()
                push_url = self.config(specs, existing)
                self.model()
                self.clients(push_url)
                self.doctor()
                self.run_command('scan', self.options.scan)
                self.run_command('index', self.options.index)
            code = EXIT_FAILED if any(s['status'] == 'failed' for s in self.steps.values()) else EXIT_OK
            self.closing(code)
        except _Refused as refused:
            code = refused.code
            self.log(f'setup stopped (exit {code}); nothing was written by the refused step')
        return {'exit_code': code, 'dry_run': self.dry_run, 'home': None if self.home is None else str(self.home),
                'steps': self.steps}

    def closing(self, code: int) -> None:
        verb = 'dry run' if self.dry_run else 'done'
        if self.options.upgrade:
            self.log(f'setup {verb} (exit {code})')
            return
        collector = self.steps['config']['detail']['push_url']
        following = ('write the push token, then: workmcp scan -> workmcp index -> workmcp push --dry-run -> '
                     'workmcp push' if collector else 'workmcp scan -> workmcp index, then register the clients')
        self.log(f'setup {verb} (exit {code}). next: {following}')

    # home ------------------------------------------------------------------------------------------------
    def check_home(self) -> None:
        options = self.options
        try:
            home = paths.validate_home(options.home, self.env, self.repo)
        except paths.DataPathError as exc:
            self.record('home', 'refused', [f'data home refused: {exc}'], path=options.home)
            raise _Refused(EXIT_HOME) from None
        appdata = paths.under_appdata(home, self.env)
        if appdata and not options.yes:
            self.record('home', 'refused', [
                f'data home refused: {home} is under AppData, where a packaged client app (Claude, Codex) may '
                rf'redirect writes into its own folder; choose a folder such as %USERPROFILE%\Motive_work, '
                f'or pass --yes to accept it'], path=str(home), under_appdata=True)
            raise _Refused(EXIT_HOME)
        self.home = home
        self.steps['home']['detail'].update(path=str(home), under_appdata=appdata)

    def prepare_home(self) -> None:
        home = self.home
        missing = not all((home/name).is_dir() for name in paths.SUBDIRS)
        lines = []
        if self.steps['home']['detail']['under_appdata']:
            lines.append(f'warning: data home {home} is under AppData (accepted with --yes); every client and '
                         f'shell must set {paths.HOME_ENV} to this same path')
        if self.dry_run:
            state = 'would be created' if missing else 'exists'
            status = 'planned' if missing else 'done'
        else:
            paths.ensure_home(home)
            state, status = ('created' if missing else 'exists'), 'done'
        lines.insert(0, f'data home: {home} (rules ok; {state})')
        self.record('home', status, lines, changed=missing, path=str(home), created=missing and not self.dry_run,
                    under_appdata=self.steps['home']['detail']['under_appdata'])

    # inputs, checked before anything is written ----------------------------------------------------------
    def check_inputs(self):
        options, home = self.options, self.home
        existing = None
        file = config.config_file(home)
        if file.is_file():
            try:
                existing = config.load(home)
            except config.ConfigError as exc:
                self.record('config', 'refused', [f'config: {exc}; fix the file by hand (setup never edits it)'],
                            file=str(file))
                raise _Refused(EXIT_USAGE) from None
        checks = config.check_roots([config.SourceEntry(raw) for raw in options.sources], home, self.repo, self.env)
        if errors := [check.error for check in checks if check.error]:
            self.record('config', 'refused', [f'source refused: {error}' for error in errors], file=str(file))
            raise _Refused(EXIT_USAGE)
        if options.model_from is not None and not Path(options.model_from).is_dir():
            self.record('model', 'refused', [f'--model-from is not an existing folder: {options.model_from}'],
                        name=models.DEFAULT_MODEL, choice='from')
            raise _Refused(EXIT_USAGE)
        if options.scope == 'project' and options.clients != (NO_CLIENTS,) and not Path(options.project).is_dir():
            self.record('clients', 'refused', [f'--project is not an existing folder: {options.project}'],
                        scope='project')
            raise _Refused(EXIT_USAGE)
        return [check.spec for check in checks], existing

    # config ----------------------------------------------------------------------------------------------
    def config(self, specs, existing) -> str | None:
        options, home = self.options, self.home
        file = config.config_file(home)
        model = config.NO_MODEL if options.model == 'none' else models.DEFAULT_MODEL
        warnings = [f'  {warning}' for spec in specs for warning in spec.warnings]
        if existing is None:
            sources = [str(spec.path) for spec in specs]
            text = config_template(tuple(sources), model, options.push_url)
            config.parse_config(text, file)  # the template always reads back (raises if it ever did not)
            push_url = options.push_url
            summary = (f'sources {len(sources)}, model {model}, push url {"set" if push_url else "not set"}')
            if self.dry_run:
                lines, status = [f'config: would write {file} ({summary})'], 'planned'
            else:
                with open(file, 'x', encoding='utf-8', newline='\n') as handle:
                    handle.write(text)
                lines, status = [f'config: written {file} ({summary})'], 'done'
            if not sources:
                lines.append("  no [[sources]] yet: add one (path = 'C:\\folder') to it before workmcp scan")
            lines += warnings
            missing, written, model_in_file = [], True, model
        else:
            # abspath, not resolve: an existing network entry is compared without touching the network
            keys = {os.path.normcase(os.path.abspath(paths._strip_long_prefix(entry.raw)))
                    for entry in existing.sources}
            missing = [str(spec.path) for spec in specs if spec.key not in keys]
            add = [line for source in missing for line in ('[[sources]]', f'path = {toml_string(source)}')]
            push_url = existing.push.url
            notes = []
            if options.push_url and existing.push.url is None:
                add += ['[push]', f'url = {toml_string(options.push_url)}']
                push_url = options.push_url
            elif options.push_url and existing.push.url.rstrip('/') != options.push_url.rstrip('/'):
                notes.append(f'  note: [push] url there is {existing.push.url}, not --push-url {options.push_url}; '
                             f'edit the file to change it')
            if existing.index.model != model:
                notes.append(f'  note: [index] model there is "{existing.index.model}", not "{model}"; '
                             f'edit the file to change it')
            sources = [entry.raw for entry in existing.sources]
            lines = [f'config: exists, not changed ({file})']
            if add:
                lines.append('  add these lines to it yourself (Notepad):')
                lines += [f'    {line}' for line in add]
            lines += notes + warnings
            status, written, model_in_file = 'done', False, existing.index.model
        token = None
        if push_url:
            token, line = self.push_token_line(home)
            lines.append(line)
        self.record('config', status, lines, changed=written, file=str(file), written=written and not self.dry_run,
                    sources=sources, missing_sources=missing, model=model_in_file, push_url=push_url,
                    push_token=token)
        return push_url

    def push_token_line(self, home: Path) -> tuple[str, str]:
        """Where the push token is (``environment``, ``file``, ``missing``) and its line; the value is never read."""
        file = home/paths.PUSH_TOKEN_FILE
        if self.env.get(paths.PUSH_TOKEN_ENV):
            return 'environment', f'push token: environment variable {paths.PUSH_TOKEN_ENV} is set'
        if file.is_file():
            return 'file', f'push token: file {file} present'
        return 'missing', (f'push token: missing - write the one line printed once by "workmcp token issue --kind '
                           f'push" on the server into {file} (Notepad), or set {paths.PUSH_TOKEN_ENV}; setup never '
                           f'reads, makes or prints it')

    # model -----------------------------------------------------------------------------------------------
    def model(self) -> None:
        options, home = self.options, self.home
        if options.model == 'none':
            self.record('model', 'done', ['model: none - lexical search only (later: setup --model download or '
                                          '--model-from DIR)'], name=config.NO_MODEL, choice='none', report={})
            return
        name = models.DEFAULT_MODEL
        choice = 'download' if options.model == 'download' else 'from'
        files = models.entry(name)['files']
        total = sum(spec['size'] for spec in files)
        size = f'{len(files)} files, {total:,} bytes'
        if self.dry_run:
            status = models.quick_status(name, home)
            if status['available']:
                self.record('model', 'done', [f'model {name}: present ({status["message"]})'], name=name,
                            choice=choice, report={})
                return
            where = (f'download from {models.entry(name)["base_url"]} (pinned revision '
                     f'{models.entry(name)["revision"][:8]})' if choice == 'download' else f'copy from {options.model_from}')
            self.record('model', 'planned', [f'model {name}: would {where} ({size}, SHA-256 checked)'],
                        changed=True, name=name, choice=choice, report={})
            return
        if choice == 'from':
            report = models.import_from(name, Path(options.model_from), home, log=self.log)
            changed = any(value == 'ok' for value in report.values())
        else:
            before = models.verify(name, home)
            if all(value == 'ok' for value in before.values()):
                report = dict.fromkeys(before, 'already_verified')
            else:
                try:
                    report = download_model(name, home, log=self.log, opener=self.opener)
                except (models.ModelFileError, OSError) as exc:
                    self.log(f'  download stopped: {type(exc).__name__}')
                    report = models.verify(name, home)
                report = {key: ('already_verified' if before[key] == 'ok' and value == 'ok' else value)
                          for key, value in report.items()}
            changed = any(value == 'ok' for value in report.values())
        if all(value in models.IMPORT_OK for value in report.values()):
            state = 'ok' if changed else 'already in place'
            self.record('model', 'done', [f'model {name}: {state} ({size}, SHA-256 verified)'], changed=changed,
                        name=name, choice=choice, report=report)
        else:
            bad = ', '.join(f'{key}: {value}' for key, value in report.items() if value not in models.IMPORT_OK)
            self.record('model', 'failed', [f'model {name}: failed ({bad})'], changed=changed, name=name,
                        choice=choice, report=report)

    # clients ---------------------------------------------------------------------------------------------
    def clients(self, push_url: str | None) -> None:
        options, home = self.options, self.home
        clients = () if options.clients == (NO_CLIENTS,) else options.clients
        if not clients:
            self.record('clients', 'skipped', ['clients: none (no registration lines)'], scope=options.scope,
                        clients=[], commands=[], files={}, registered={})
            return
        url = mcp_url(push_url) if push_url else None
        role = 'collector role: the remote server' if url else 'local stdio server'
        found = registered(self.env, clients)
        places = client_files(self.env)
        known = [f'  {places[client]}: ' + {True: f"'{SERVER_NAME}' is already registered (remove it first to "
                                                  f"replace it, or keep it)",
                                            False: f"no '{SERVER_NAME}' entry yet",
                                            None: 'not found or unreadable'}[found[client]] for client in clients]
        if options.scope == 'user':
            commands = client_commands(home, self.python, clients, url)
            lines = [f'clients ({role}, user scope) - run these yourself; setup runs nothing:',
                     *self.command_lines(clients, commands, url), *known]
            self.record('clients', 'done', lines, scope='user', clients=list(clients), commands=commands, files={},
                        registered=found)
            return
        lines, states, changed = [f'clients ({role}, project scope {options.project}):'], {}, False
        for path, text in project_files(home, self.python, Path(options.project), clients, url).items():
            state = self.project_file(path, text)
            states[str(path)] = state
            changed = changed or state in ('written', 'overwritten', 'would write', 'would overwrite')
            lines.append(f'  {path}: {state}')
        if 'codex' in clients:
            lines.append('  Codex reads a project .codex/config.toml only in a trusted project (approve the folder '
                         'once in Codex)')
        if url:
            lines.append(f'  the token comes from the user environment variable {MCP_TOKEN_ENV} (no admin: Windows '
                         f'settings > "Edit environment variables for your account")')
        lines += known
        status = 'planned' if self.dry_run and changed else 'done'
        self.record('clients', status, lines, changed=changed, scope='project', clients=list(clients),
                    commands=[], files=states, registered=found)

    def command_lines(self, clients, commands, url) -> list[str]:
        by_client = dict(zip(clients, commands))
        lines = []
        if url is None:
            for client in clients:
                lines.append(f'  {by_client[client]}')
            return lines + ['  # check: claude mcp list / codex mcp list']
        if 'claude' in by_client:
            lines += [f'  # Claude Code: {MCP_TOKEN_PLACEHOLDER} is the mcp token the server printed once',
                      f'  {by_client["claude"]}',
                      '  #   the token lands in your shell history: clear it afterwards, e.g. PowerShell: '
                      'Remove-Item (Get-PSReadLineOption).HistorySavePath',
                      f'  #   without the token on a command line: --scope project writes .mcp.json with '
                      f'"Bearer ${{{MCP_TOKEN_ENV}}}"']
        if 'codex' in by_client:
            lines += [f'  # Codex: the token stays in the environment variable {MCP_TOKEN_ENV}, never on the '
                      f'command line',
                      f'  {by_client["codex"]}',
                      rf'  #   then add to [mcp_servers.{SERVER_NAME}] in %USERPROFILE%\.codex\config.toml: '
                      'startup_timeout_sec = 30 / tool_timeout_sec = 120']
        return lines + [f'  # {MCP_TOKEN_ENV} (user environment, no admin): Windows settings > "Edit environment '
                        f'variables for your account" > New',
                        '  # check: claude mcp list / codex mcp list, then call the status tool in each client']

    def project_file(self, path: Path, text: str) -> str:
        """Write ``path`` when missing (or differing with ``--yes``); returns what happened."""
        try:
            current = path.read_text(encoding='utf-8-sig') if path.is_file() else None
        except (OSError, ValueError):
            current = ''  # unreadable: treated as differing
        if current == text:
            return 'unchanged'
        if current is not None and not self.options.yes:
            names = _server_names(path, current)
            servers = 'unreadable' if names is None else ', '.join(names) or 'none'
            return f'exists with other content (servers: {servers}); not changed - pass --yes to overwrite'
        verb = 'overwrite' if current is not None else 'write'
        if self.dry_run:
            return f'would {verb}'
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + '.partial')
        with open(partial, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
        os.replace(partial, path)
        return 'overwritten' if current is not None else 'written'

    # doctor, scan, index ---------------------------------------------------------------------------------
    def doctor(self) -> None:
        self.run_command('doctor', True)

    def run_command(self, step: str, wanted: bool) -> None:
        if not wanted:
            return
        command = self.commands.get(step)
        if self.dry_run:
            self.record(step, 'planned' if step != 'doctor' else 'skipped',
                        [f'{step}: would run after setup' if step != 'doctor' else 'doctor: runs in a real run'])
            return
        if command is None:
            self.record(step, 'skipped', [f'{step}: not available here'])
            return
        self.log(f'--- {step} ---')
        code = command(self.home)
        self.record(step, 'done' if code == 0 else 'failed', [] if code == 0 else [f'{step}: exit {code}'],
                    exit_code=code)
        self.steps[step]['lines'].insert(0, f'--- {step} ---')

    # upgrade ---------------------------------------------------------------------------------------------
    def upgrade(self) -> None:
        home = self.home
        db_file = paths.db_path(home)
        locks = ((store.LOCK_NAME, store.STALE_LOCK_AGE), (store.INDEX_LOCK_NAME, store.STALE_LOCK_AGE),
                 (push.PUSH_LOCK, push.PUSH_LOCK_STALE))
        if not db_file.is_file():
            self.record('upgrade', 'skipped', [f'upgrade: no store yet ({db_file}); nothing to back up or migrate'])
            return
        schema_from = schema_version(db_file)
        if self.dry_run:
            held = [name for name, _ in locks if store.lock_path(home, name).exists()]
            lines = [f'upgrade: would back up {db_file} to db\\backup-<time>-v{schema_from}.sqlite3, then migrate '
                     f'v{schema_from} -> v{store.SCHEMA_VERSION}']
            if held:
                lines.append(f'  lock present: {", ".join(held)} (a real run exits 3 unless the lock is stale)')
            self.record('upgrade', 'planned', lines, changed=True, schema_from=schema_from,
                        schema_to=store.SCHEMA_VERSION)
            return
        held, lines = [], []
        nonce = store.new_lock_nonce()          # release only the locks this run took (review M5 remote v3 V3-5)
        try:
            for name, stale in locks:
                taken = store.acquire_lock(home, name, stale_after=stale, nonce=nonce)
                held.append(taken[0])
                if taken[1]:
                    lines.append(taken[1])
            backup = backup_store(home)
            lines.append(f'backup: {backup} ({backup.stat().st_size / 2**20:.1f} MB)')
            store.open_home(home).close()
        except store.Locked as exc:
            self.record('upgrade', 'refused', [*lines, f'upgrade refused: {exc}'], schema_from=schema_from)
            raise _Refused(EXIT_LOCKED) from None
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc):
                raise
            self.record('upgrade', 'refused', [*lines, 'upgrade refused: the store is locked by another process '
                                               '(stop the server first)'], schema_from=schema_from)
            raise _Refused(EXIT_LOCKED) from None
        except RuntimeError as exc:
            if not store.schema_changed(exc):
                raise
            self.record('upgrade', 'refused', [*lines, f'upgrade refused: {store.DEV_SCHEMA_HINT.format(db=db_file)}'],
                        schema_from=schema_from)
            raise _Refused(EXIT_HOME) from None
        except OSError as exc:
            self.record('upgrade', 'failed', [*lines, f'upgrade: backup failed ({type(exc).__name__})'],
                        schema_from=schema_from)
            return
        finally:
            for path in held:
                store.release_lock(path, nonce)
        schema_to = schema_version(db_file)
        lines.append(f'schema: v{schema_from} -> v{schema_to}'
                     + (' (no migration needed)' if schema_from == schema_to else ' (migrated)'))
        self.record('upgrade', 'done', lines, changed=True, backup=str(backup), schema_from=schema_from,
                    schema_to=schema_to)


def main(args, commands: Mapping[str, Command] | None = None) -> int:
    """``workmcp setup`` body: arguments (exit 4) -> ``plan`` (``--dry-run``) or ``apply`` -> its exit code.
    ``commands``: cli's ``doctor``/``scan``/``index`` bodies for a data home (cli passes them in)."""
    options = options_from_args(args)
    if error := check_options(options):
        print(error, file=sys.stderr)
        return EXIT_USAGE
    steps = plan if options.dry_run else apply
    return steps(options, commands=commands)['exit_code']

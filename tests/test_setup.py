"""``workmcp setup`` (design M6 §2, over M5 §2.3, §12): arguments, argument rules, result shape and exit codes,
then the steps on temporary homes with synthetic source folders and a tiny fake model (``--model-from`` and a
fake ``opener`` for ``--model download``). No network (sockets and ``urlopen`` fail the test), no process, no
real data home, no real client file: every run gets an injected environment whose user folder is ``tmp_path``.
"""
import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

import pytest

from workmcp import cli, config, models, paths, setup, store

ROOT = Path(__file__).resolve().parents[1]
ABS = str(Path(__file__).resolve().parent)  # any absolute folder (the argument rules never touch it)
PY = r'C:\Tools\venv\Scripts\python.exe'    # the interpreter the client lines name (synthetic)
URL = 'https://example.invalid/workmcp'
MCP = 'https://example.invalid/workmcp/mcp'
FAKE = 'FAKE-TOKEN-'                         # prefix of every fake secret; must never be printed
CONTENT = {'tiny.onnx': b'A' * 3000, 'tiny.onnx_data': bytes(range(256)) * 40, 'tiny.tokenizer.json': b'{"t": 1}'}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Any socket connection, name lookup, ``urlopen`` or process start fails the test."""
    def refuse(*args, **kwargs):
        raise AssertionError('setup tests must stay offline and start no process')
    monkeypatch.setattr(socket.socket, 'connect', refuse)
    monkeypatch.setattr(socket, 'getaddrinfo', refuse)
    monkeypatch.setattr(socket, 'create_connection', refuse)
    monkeypatch.setattr(urllib.request, 'urlopen', refuse)
    monkeypatch.setattr(subprocess, 'Popen', refuse)


@pytest.fixture
def user(tmp_path):
    folder = tmp_path/'user'
    folder.mkdir()
    return folder


@pytest.fixture
def env(user):
    """The environment setup sees: only a user folder (no AppData variables, no tokens)."""
    return {'USERPROFILE': str(user)}


@pytest.fixture
def home(tmp_path):
    return tmp_path/'Motive_work'


@pytest.fixture
def docs(tmp_path):
    folder = tmp_path/'docs'
    folder.mkdir()
    (folder/'note.txt').write_text('Synthetic setup note: the quarterly widget review moved to Friday.\n',
                                   encoding='utf-8')
    return folder


@pytest.fixture
def tiny_model(monkeypatch):
    """The default model name with a tiny synthetic manifest (``tests/test_models.py`` pattern)."""
    spec = {**models.MANIFEST['bge-m3'], 'repo': 'test/none', 'revision': 'rev1', 'base_url': 'https://example.invalid',
            'model_file': 'tiny.onnx', 'tokenizer_file': 'tiny.tokenizer.json',
            'files': [{'path': f'onnx/{n}', 'size': len(c), 'sha256': sha(c)} for n, c in CONTENT.items()]}
    monkeypatch.setitem(models.MANIFEST, 'bge-m3', spec)
    return spec


def bundle(folder: Path, broken: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in CONTENT.items():
        (folder/name).write_bytes(bytes(len(data)) if name == broken else data)  # same size, wrong bytes
    return folder


def snapshot(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): (sha(p.read_bytes()), p.stat().st_mtime_ns)
            for p in sorted(folder.rglob('*')) if p.is_file()}


def run(home, env, *, dry=False, python=PY, opener=None, **changes):
    """``apply`` (or ``plan``) with cli's real doctor/scan/index; returns ``(result, lines)``."""
    options = setup.Options(home=str(home), **{'model': 'none', 'clients': ('none',), **changes})
    lines = []
    if dry:
        result = setup.plan(options, env=env, python=python, log=lines.append, commands=cli.setup_commands())
    else:
        result = setup.apply(options, env=env, python=python, log=lines.append, commands=cli.setup_commands(),
                             opener=opener)
    assert tuple(result) == setup.RESULT_KEYS and tuple(result['steps']) == setup.STEPS
    for step, record in result['steps'].items():
        assert tuple(record) == setup.STEP_KEYS and record['status'] in setup.STEP_STATUSES
        assert tuple(record['detail']) == setup.STEP_DETAIL_KEYS[step]
    return result, lines


def statuses(result) -> dict:
    return {step: record['status'] for step, record in result['steps'].items()}


# --- contract (stage 0, extended in M6) -------------------------------------------------------------------

def parse(*argv):
    """The ``setup`` namespace exactly as ``cli.main`` builds it (``setup.main`` stubbed to capture it)."""
    seen = {}

    def capture(args, commands=None):
        seen['args'], seen['commands'] = args, commands
        return 0

    real, setup.main = setup.main, capture
    try:
        assert cli.main(['setup', *argv]) == 0
    finally:
        setup.main = real
    assert set(seen['commands']) == {'doctor', 'scan', 'index'}  # cli hands its bodies in (design M6 §2.6-9)
    return seen['args']


def test_cli_setup_arguments_map_to_options():
    options = setup.options_from_args(parse('--home', ABS, '--source', 'C:\\a', '--source', 'C:\\b', '--model',
                                            'download', '--clients', 'codex', '--scope', 'project', '--project', ABS,
                                            '--scan', '--index', '--yes', '--push-url', URL, '--dry-run'))
    assert options == setup.Options(home=ABS, sources=('C:\\a', 'C:\\b'), model='download', clients=('codex',),
                                    scope='project', project=ABS, scan=True, index=True, yes=True, push_url=URL,
                                    dry_run=True)
    defaults = setup.options_from_args(parse())
    assert defaults == setup.Options()
    assert defaults.clients == setup.CLIENTS and defaults.scope == 'user' and not defaults.upgrade
    assert defaults.push_url is None and defaults.dry_run is False
    assert setup.options_from_args(parse('--home', ABS, '--model-from', ABS)).model_from == ABS
    assert setup.options_from_args(parse('--home', ABS, '--upgrade')).upgrade is True
    assert setup.options_from_args(parse('--home', ABS, '--clients', 'none')).clients == ('none',)


@pytest.mark.parametrize('changes, message', [
    ({'home': None}, '--home PATH is required'),
    ({'model': None}, '--model download|none or --model-from DIR is required'),
    ({'model_from': ABS}, 'exclude each other'),
    ({'model': 'latest'}, '--model must be one of download, none'),
    ({'model': None, 'model_from': 'relative\\bundle'}, '--model-from must be an absolute folder'),
    ({'scope': 'global'}, '--scope must be one of user, project'),
    ({'scope': 'project'}, '--scope project needs --project DIR'),
    ({'project': ABS}, '--project DIR needs --scope project'),
    ({'scope': 'project', 'project': 'proj'}, '--project must be an absolute folder'),
    ({'clients': ()}, '--clients must be'),
    ({'clients': ('claude', 'claude')}, '--clients must be'),
    ({'clients': ('claude', 'cursor')}, '--clients must be'),
    ({'clients': ('none', 'claude')}, '--clients must be'),   # none only on its own
    ({'push_url': 'http://example.invalid/workmcp'}, '--push-url must start with https://'),
    ({'push_url': 'http://127.0.0.1:8765/workmcp'}, '--push-url must start with https://'),
    ({'push_url': 'https://user@example.invalid/x'}, '--push-url:'),
    ({'push_url': 'ftp://example.invalid'}, '--push-url:'),
])
def test_check_options_names_the_broken_rule(changes, message):
    base = {'home': ABS, 'model': 'none'}
    error = setup.check_options(setup.Options(**{**base, **changes}))
    assert error is not None and message in error


@pytest.mark.parametrize('options', [
    setup.Options(home=ABS, model='none'),
    setup.Options(home=ABS, model='download', sources=('C:\\x',), scan=True, index=True),
    setup.Options(home=ABS, model_from=ABS, scope='project', project=ABS, clients=('claude',)),
    setup.Options(home=ABS, upgrade=True),  # --upgrade needs no model choice (its other steps are skipped)
    setup.Options(home=ABS, model='none', clients=('none',), push_url=URL, dry_run=True),
])
def test_check_options_accepts_valid_combinations(options):
    assert setup.check_options(options) is None


def test_result_shape_and_exit_codes_are_fixed():
    assert setup.RESULT_KEYS == ('exit_code', 'dry_run', 'home', 'steps')
    assert setup.STEPS == ('home', 'config', 'model', 'clients', 'doctor', 'scan', 'index', 'upgrade')
    assert setup.STEP_KEYS == ('status', 'changed', 'lines', 'detail')
    assert set(setup.STEP_DETAIL_KEYS) == set(setup.STEPS)
    assert (setup.EXIT_OK, setup.EXIT_FAILED, setup.EXIT_HOME, setup.EXIT_LOCKED, setup.EXIT_USAGE) == (0, 1, 2, 3, 4)
    assert setup.MODEL_CHOICES == ('download', 'none') and setup.SCOPES == ('user', 'project')
    assert setup.CLIENTS == ('claude', 'codex') and setup.NO_CLIENTS == 'none'


def test_cli_setup_exits_4_on_arguments_without_creating_anything(tmp_path, capsys):
    home = tmp_path/'Motive_work'
    assert cli.main(['setup', '--model', 'none']) == 4
    assert '--home PATH is required' in capsys.readouterr().err
    assert cli.main(['setup', '--home', str(home), '--model', 'none', '--model-from', str(tmp_path)]) == 4
    assert 'exclude each other' in capsys.readouterr().err
    assert cli.main(['setup', '--home', str(home), '--model', 'latest']) == 4
    assert cli.main(['setup', '--home', str(home), '--model', 'none', '--push-url', 'http://example.invalid/w']) == 4
    assert '--push-url must start with https://' in capsys.readouterr().err
    assert not home.exists()


# --- idempotence and the full run ---------------------------------------------------------------------------

def test_apply_twice_creates_everything_once_and_the_second_run_changes_nothing(home, env, docs, tiny_model,
                                                                               tmp_path, capsys):
    options = dict(model=None, model_from=str(bundle(tmp_path/'bundle')), sources=(str(docs),), push_url=URL,
                   clients=setup.CLIENTS)
    first, lines = run(home, env, **options)
    assert first['exit_code'] == 0 and first['dry_run'] is False and first['home'] == str(home)
    assert statuses(first) == {'home': 'done', 'config': 'done', 'model': 'done', 'clients': 'done',
                               'doctor': 'done', 'scan': 'skipped', 'index': 'skipped', 'upgrade': 'skipped'}
    steps = first['steps']
    assert steps['home']['detail'] == {'path': str(home), 'created': True, 'under_appdata': False}
    assert steps['config']['changed'] and steps['config']['detail']['written'] is True
    assert steps['config']['detail']['sources'] == [str(docs.resolve())]
    assert steps['config']['detail']['push_url'] == URL and steps['config']['detail']['push_token'] == 'missing'
    assert steps['model']['changed'] and steps['model']['detail']['report'] == dict.fromkeys(CONTENT, 'ok')
    assert steps['doctor']['detail'] == {'exit_code': 0}
    assert all((home/name).is_dir() for name in paths.SUBDIRS)
    cfg = config.load(home)
    assert [entry.raw for entry in cfg.sources] == [str(docs.resolve())]
    assert cfg.index.model == 'bge-m3' and cfg.push.url == URL
    assert models.verify('bge-m3', home) == dict.fromkeys(CONTENT, 'ok')
    assert f'data home: {home} (rules ok; created)' in lines
    assert any(line.startswith('config: written') and 'push url set' in line for line in lines)
    assert any('push token: missing' in line for line in lines)
    assert lines[-1].startswith('setup done (exit 0). next: set WORKMCP_HOME (above), write the push token, then: '
                                'workmcp scan')
    out = capsys.readouterr().out
    assert 'role: collector' in out and f'[push] url {URL}' in out  # doctor ran on the setup home

    before = snapshot(home)
    second, lines = run(home, env, **options)
    assert second['exit_code'] == 0
    assert not any(record['changed'] for record in second['steps'].values())
    assert second['steps']['home']['detail']['created'] is False
    assert second['steps']['config']['detail']['written'] is False
    assert second['steps']['model']['detail']['report'] == dict.fromkeys(CONTENT, 'already_verified')
    assert snapshot(home) == before  # same files, same bytes, same times
    assert f'data home: {home} (rules ok; exists)' in lines
    assert any(line.startswith('config: exists, not changed') for line in lines)
    assert not any('add these lines' in line for line in lines)
    assert any('already in place' in line for line in lines)


# --- review M6 v1 M6-2 / M6-18: a data home other than the default needs WORKMCP_HOME; the next line --------------

def home_lines(result) -> list[str]:
    return result['steps']['home']['lines']


def test_a_home_other_than_the_default_says_how_to_set_workmcp_home(tmp_path, env, docs, capsys):
    home = tmp_path/"D drive's Motive_work"           # a quote inside: doubled in the PowerShell string
    result, lines = run(home, env, sources=(str(docs),), push_url=URL)
    text = '\n'.join(home_lines(result))
    assert result['exit_code'] == 0
    assert f'WORKMCP_HOME: this data home is not the default ({env["USERPROFILE"]}\\Motive_work)' in text
    quoted = str(home).replace("'", "''")
    assert f"[Environment]::SetEnvironmentVariable('WORKMCP_HOME', '{quoted}', 'User')" in text
    assert 'Edit environment variables for your account' in text and 'no admin' in text
    assert lines[-1] == ('setup done (exit 0). next: set WORKMCP_HOME (above), write the push token, then: '
                         'workmcp scan -> workmcp index -> workmcp push --dry-run -> workmcp push')
    assert 'from setup --home' in capsys.readouterr().out          # doctor names where its home came from


def test_the_default_home_or_a_matching_variable_needs_no_workmcp_home_line(user, env, docs):
    default = user/'Motive_work'
    result, lines = run(default, env, sources=(str(docs),), push_url=URL)
    assert not any('WORKMCP_HOME' in line for line in home_lines(result)), home_lines(result)
    assert lines[-1].startswith('setup done (exit 0). next: write the push token, then: workmcp scan')
    other = user.parent/'elsewhere'
    result, lines = run(other, {**env, 'WORKMCP_HOME': str(other)}, sources=(str(docs),), push_url=URL)
    assert home_lines(result)[1:] == ['WORKMCP_HOME: set to this data home']
    assert 'set WORKMCP_HOME' not in lines[-1]
    result, lines = run(other, {**env, 'WORKMCP_HOME': str(default)}, sources=(str(docs),), push_url=URL)
    assert any('set to another folder' in line for line in home_lines(result))
    assert lines[-1].startswith('setup done (exit 0). next: set WORKMCP_HOME (above), write the push token')


def test_the_next_line_leaves_out_a_push_token_that_is_there(user, env, docs):
    """Review M6 v1 M6-18: ``write the push token`` only while it is missing (the value is never read)."""
    default = user/'Motive_work'
    result, lines = run(default, {**env, paths.PUSH_TOKEN_ENV: FAKE + 'present'}, sources=(str(docs),), push_url=URL)
    assert result['steps']['config']['detail']['push_token'] == 'environment'
    assert lines[-1] == ('setup done (exit 0). next: workmcp scan -> workmcp index -> workmcp push --dry-run -> '
                         'workmcp push')
    local = user.parent/'local-home'                                             # local role (no push url)
    result, lines = run(local, {**env, 'WORKMCP_HOME': str(local)}, sources=(str(docs),))
    assert lines[-1] == 'setup done (exit 0). next: workmcp scan -> workmcp index, then register the clients'


def test_doctor_names_the_source_of_its_data_home():
    assert cli.home_source(True, {}) == 'from setup --home'
    assert cli.home_source(False, {'WORKMCP_HOME': 'D:\\Motive_work'}) == 'from WORKMCP_HOME'
    assert cli.home_source(False, {}) == 'default, WORKMCP_HOME not set'


def test_scan_and_index_run_on_the_setup_home_when_asked(home, env, docs, capsys):
    result, _ = run(home, env, sources=(str(docs),), scan=True, index=True)
    assert result['exit_code'] == 0
    assert result['steps']['scan']['status'] == 'done' and result['steps']['scan']['detail'] == {'exit_code': 0}
    assert result['steps']['index']['status'] == 'done' and result['steps']['index']['detail'] == {'exit_code': 0}
    assert paths.db_path(home).is_file()
    out = capsys.readouterr().out
    assert 'scan #1' in out and 'model none' in out  # model none: chunks and FTS only
    db = store.open_read(home)
    try:
        assert db.execute("SELECT COUNT(*) FROM documents WHERE status='active'").fetchone()[0] == 1
    finally:
        db.close()


def test_scan_without_sources_is_a_failed_step_and_exit_1(home, env, capsys):
    result, lines = run(home, env, scan=True)
    assert result['exit_code'] == 1 and result['steps']['scan']['status'] == 'failed'
    assert result['steps']['scan']['detail'] == {'exit_code': 4}
    assert any("no [[sources]] yet" in line for line in lines)


# --- refusals: nothing is created ---------------------------------------------------------------------------

def test_home_inside_the_repository_is_refused_with_exit_2(env):
    home = ROOT/'setup-test-home-must-not-exist'
    result, lines = run(home, env)
    assert result['exit_code'] == 2 and result['steps']['home']['status'] == 'refused'
    assert any('home_inside_repo' in line for line in lines)
    assert not home.exists()


def test_appdata_home_is_refused_without_yes_and_accepted_with_a_warning(tmp_path, env):
    local = tmp_path/'AppData'/'Local'
    appdata_env = {**env, 'LOCALAPPDATA': str(local)}
    home = local/'Motive_work'
    result, lines = run(home, appdata_env)
    assert result['exit_code'] == 2 and result['steps']['home']['status'] == 'refused'
    assert result['steps']['home']['detail']['under_appdata'] is True
    assert any('under AppData' in line and '--yes' in line for line in lines)
    assert not home.exists() and not local.exists()
    result, lines = run(home, appdata_env, yes=True)
    assert result['exit_code'] == 0 and home.is_dir()
    assert result['steps']['home']['detail']['under_appdata'] is True
    assert any(line.startswith('warning: data home') and 'under AppData' in line for line in lines)


def test_missing_source_is_refused_with_exit_4_before_anything_is_created(home, env, tmp_path):
    result, lines = run(home, env, sources=(str(tmp_path/'nowhere'),))
    assert result['exit_code'] == 4 and result['steps']['config']['status'] == 'refused'
    assert any('source_root_missing' in line for line in lines)
    assert result['steps']['home']['status'] == 'skipped'
    assert not home.exists()


def test_source_inside_the_home_is_refused(home, env):
    inner = home/'docs'
    inner.mkdir(parents=True)
    result, lines = run(home, env, sources=(str(inner),))
    assert result['exit_code'] == 4 and any('source_root_inside_home' in line for line in lines)
    assert sorted(p.name for p in home.iterdir()) == ['docs']  # the sub-folders were not created


def test_missing_model_folder_and_project_folder_are_refused_with_exit_4(home, env, tmp_path):
    result, lines = run(home, env, model=None, model_from=str(tmp_path/'no-bundle'))
    assert result['exit_code'] == 4 and result['steps']['model']['status'] == 'refused'
    result, lines = run(home, env, clients=('claude',), scope='project', project=str(tmp_path/'no-project'))
    assert result['exit_code'] == 4 and result['steps']['clients']['status'] == 'refused'
    assert not home.exists()


def test_a_broken_existing_config_is_refused_and_left_alone(home, env):
    home.mkdir()
    (home/'config.toml').write_text('[index]\nmodel = "nope"\n', encoding='utf-8')
    before = snapshot(home)
    result, lines = run(home, env)
    assert result['exit_code'] == 4 and result['steps']['config']['status'] == 'refused'
    assert snapshot(home) == before


# --- config.toml --------------------------------------------------------------------------------------------

@pytest.mark.parametrize('sources', [
    ('C:\\Users\\x\\문서\\자료',),
    ("C:\\Users\\x\\it's here", 'D:\\a b\\c'),
    ('C:\\odd"name\\x', 'C:\\tab\there'),   # never a Windows folder, but the template must still read back
])
def test_config_template_reads_back_exactly(sources, tmp_path):
    for model, url in (('bge-m3', URL), ('none', None)):
        text = setup.config_template(sources, model, url)
        cfg = config.parse_config(text, tmp_path/'config.toml')
        assert [entry.raw for entry in cfg.sources] == list(sources)
        assert cfg.index.model == model and cfg.push.url == url
    assert setup.toml_string('C:\\a\\b') == "'C:\\a\\b'"  # literal string: backslashes as written


def test_a_source_folder_with_a_quote_in_its_name_round_trips(home, env, tmp_path):
    folder = tmp_path/"it's docs"
    folder.mkdir()
    result, _ = run(home, env, sources=(str(folder),))
    assert result['exit_code'] == 0
    assert [entry.raw for entry in config.load(home).sources] == [str(folder.resolve())]


def test_an_existing_config_is_never_changed_and_the_missing_lines_are_printed(home, env, docs, tmp_path):
    other = tmp_path/'other'
    other.mkdir()
    home.mkdir()
    text = f"# mine\n[[sources]]\npath = '{docs.resolve()}'\n\n[index]\nmodel = \"bge-m3\"\n"
    (home/'config.toml').write_bytes(text.encode('utf-8'))
    result, lines = run(home, env, sources=(str(docs), str(other)), push_url=URL)
    assert result['exit_code'] == 0
    assert (home/'config.toml').read_bytes() == text.encode('utf-8')
    detail = result['steps']['config']['detail']
    assert result['steps']['config']['changed'] is False and detail['written'] is False
    assert detail['missing_sources'] == [str(other.resolve())] and detail['push_url'] == URL
    assert '  add these lines to it yourself (Notepad):' in lines
    assert '    [[sources]]' in lines and f"    path = '{other.resolve()}'" in lines
    assert '    [push]' in lines and f"    url = '{URL}'" in lines
    assert any('[index] model there is "bge-m3", not "none"' in line for line in lines)  # --model none: a note
    assert detail['model'] == 'bge-m3'


# --- model ----------------------------------------------------------------------------------------------------

def test_model_from_a_damaged_bundle_fails_with_exit_1_and_the_other_steps_still_run(home, env, tiny_model, tmp_path):
    damaged = bundle(tmp_path/'bundle', broken='tiny.onnx_data')
    result, lines = run(home, env, model=None, model_from=str(damaged), clients=('codex',))
    assert result['exit_code'] == 1 and result['steps']['model']['status'] == 'failed'
    assert result['steps']['model']['detail']['report']['tiny.onnx_data'] == 'sha256_mismatch_in_source'
    assert any(line.startswith('model bge-m3: failed (tiny.onnx_data: sha256_mismatch_in_source)') for line in lines)
    assert result['steps']['clients']['status'] == 'done' and result['steps']['doctor']['status'] == 'done'
    assert not models.file_path('bge-m3', 'tiny.onnx_data', home).exists()


class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body, self.headers = status, body, {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        block, self.body = self.body[:n], self.body[n:]
        return block


def test_model_download_goes_through_models_fetch_with_the_pinned_urls_only(home, env, tiny_model):
    seen = []

    def opener(request, timeout):
        seen.append(request.full_url)
        return FakeResponse(200, CONTENT[request.full_url.rsplit('/', 1)[-1]])

    result, lines = run(home, env, model='download', opener=opener)
    assert result['exit_code'] == 0 and result['steps']['model']['detail']['report'] == dict.fromkeys(CONTENT, 'ok')
    assert seen == [f'https://example.invalid/test/none/resolve/rev1/onnx/{name}' for name in CONTENT]
    assert any(line.startswith('fetch bge-m3/tiny.onnx ') for line in lines)
    seen.clear()
    result, _ = run(home, env, model='download', opener=opener)
    assert seen == [] and result['steps']['model']['changed'] is False
    assert result['steps']['model']['detail']['report'] == dict.fromkeys(CONTENT, 'already_verified')


def test_a_failing_download_is_a_failed_step_and_exit_1(home, env, tiny_model):
    result, lines = run(home, env, model='download', opener=lambda request, timeout: FakeResponse(503, b''))
    assert result['exit_code'] == 1 and result['steps']['model']['status'] == 'failed'
    assert set(result['steps']['model']['detail']['report'].values()) == {'missing'}
    assert 'download stopped: ModelFileError' in '\n'.join(lines)
    assert not list((home/'tmp').glob('*.partial'))


def test_model_none_writes_index_model_none_and_fetches_nothing(home, env):
    result, lines = run(home, env)
    assert result['steps']['model']['detail'] == {'name': 'none', 'choice': 'none', 'report': {}}
    assert config.load(home).index.model == 'none'
    assert any(line.startswith('model: none - lexical search only') for line in lines)


# --- secrets ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('where', ['environment', 'file'])
def test_no_token_value_is_read_or_printed(home, env, user, docs, monkeypatch, capsys, where):
    home.mkdir()
    secrets = {'push_env': FAKE + 'push-env-1', 'push_file': FAKE + 'push-file-2', 'mcp': FAKE + 'mcp-3',
               'claude': FAKE + 'claude-4'}
    (home/paths.PUSH_TOKEN_FILE).write_text(secrets['push_file'] + '\n', encoding='utf-8')
    (user/'.claude.json').write_text(json.dumps({'mcpServers': {'workmcp': {
        'type': 'http', 'url': MCP, 'headers': {'Authorization': f"Bearer {secrets['claude']}"}}}}), encoding='utf-8')
    token_env = {**env, setup.MCP_TOKEN_ENV: secrets['mcp']}
    monkeypatch.delenv(paths.PUSH_TOKEN_ENV, raising=False)
    if where == 'environment':
        token_env[paths.PUSH_TOKEN_ENV] = secrets['push_env']
        monkeypatch.setenv(paths.PUSH_TOKEN_ENV, secrets['push_env'])  # doctor reads os.environ (presence only)
    monkeypatch.setenv(setup.MCP_TOKEN_ENV, secrets['mcp'])
    reads = []
    real_read = paths.read_token
    monkeypatch.setattr(paths, 'read_token', lambda *a, **k: reads.append(a) or real_read(*a, **k))
    result, lines = run(home, token_env, sources=(str(docs),), push_url=URL, clients=setup.CLIENTS)
    assert result['exit_code'] == 0 and reads == []
    assert result['steps']['config']['detail']['push_token'] == where
    assert result['steps']['clients']['detail']['registered'] == {'claude': True, 'codex': None}
    captured = capsys.readouterr()
    dumped = json.dumps(result, ensure_ascii=False) + '\n'.join(lines) + captured.out + captured.err
    assert FAKE not in dumped
    assert (home/paths.PUSH_TOKEN_FILE).read_text(encoding='utf-8') == secrets['push_file'] + '\n'


# --- clients ----------------------------------------------------------------------------------------------------

def test_collector_role_prints_the_remote_registration_lines(home, env, docs):
    result, lines = run(home, env, sources=(str(docs),), push_url=URL + '/', clients=setup.CLIENTS)
    commands = result['steps']['clients']['detail']['commands']
    assert commands == [
        f'claude mcp add --transport http --scope user workmcp {MCP} '
        '--header "Authorization: Bearer $env:WORKMCP_MCP_TOKEN"',
        f'codex mcp add workmcp --url {MCP} --bearer-token-env-var WORKMCP_MCP_TOKEN']
    assert all(f'  {command}' in lines for command in commands)
    text = '\n'.join(lines)
    assert 'collector role' in text and 'run these yourself' in text and 'startup_timeout_sec = 30' in text
    assert result['steps']['clients']['changed'] is False
    # review M6 v1 M6-3: the variable comes first, the history keeps its name only, no history file is removed,
    # and the line says where Claude Code keeps the token
    assert 'Edit environment variables for your account' in text and 'keeps only the variable name' in text
    assert '.claude.json' in text and 'Remove-Item' not in text and 'HistorySavePath' not in text
    assert text.index('Edit environment variables') < text.index('claude mcp add')


def test_local_role_prints_stdio_lines_with_python_isolated_and_the_home(home, env):
    python = r'C:\Program Files\Py\python.exe'
    result, lines = run(home, env, clients=('claude', 'codex'), python=python)
    commands = result['steps']['clients']['detail']['commands']
    assert commands == [
        f'claude mcp add --scope user --transport stdio -e WORKMCP_HOME={home} workmcp -- "{python}" -I -m workmcp serve',
        f'codex mcp add workmcp --env WORKMCP_HOME={home} -- "{python}" -I -m workmcp serve']
    assert 'local stdio server' in '\n'.join(lines)


def test_clients_none_prints_no_registration_line(home, env):
    result, lines = run(home, env)
    assert result['steps']['clients']['status'] == 'skipped' and result['steps']['clients']['detail']['commands'] == []
    assert not any('mcp add' in line for line in lines)


def test_registered_reads_key_names_only_and_never_writes(tmp_path, user, env):
    assert setup.registered(env, setup.CLIENTS) == {'claude': None, 'codex': None}  # no files
    (user/'.claude.json').write_text(json.dumps({'mcpServers': {'other': {}}, 'projects': {}}), encoding='utf-8')
    (user/'.codex').mkdir()
    (user/'.codex'/'config.toml').write_text('[mcp_servers.workmcp]\nurl = "https://example.invalid/x"\n',
                                             encoding='utf-8')
    before = snapshot(user)
    assert setup.registered(env, setup.CLIENTS) == {'claude': False, 'codex': True}
    (user/'.claude.json').write_text('{not json', encoding='utf-8')
    assert setup.registered(env, ('claude',)) == {'claude': None}
    codex_home = tmp_path/'codexhome'
    codex_home.mkdir()
    (codex_home/'config.toml').write_text('model = "x"\n', encoding='utf-8')
    assert setup.registered({**env, 'CODEX_HOME': str(codex_home)}, ('codex',)) == {'codex': False}
    assert setup.registered({}, setup.CLIENTS) == {'claude': None, 'codex': None}
    assert snapshot(user)['.codex/config.toml'] == before['.codex/config.toml']


def test_global_client_files_stay_untouched_by_a_run(home, env, user):
    (user/'.claude.json').write_text(json.dumps({'mcpServers': {'workmcp': {'type': 'http', 'url': MCP}}}),
                                     encoding='utf-8')
    before = snapshot(user)
    result, lines = run(home, env, clients=setup.CLIENTS)
    assert result['steps']['clients']['detail']['registered'] == {'claude': True, 'codex': None}
    assert any("'workmcp' is already registered" in line for line in lines)
    assert snapshot(user) == before


@pytest.mark.parametrize('push_url', [None, URL])
def test_project_scope_writes_the_two_files_once_and_respects_local_edits(home, env, tmp_path, push_url):
    project = tmp_path/'project'
    project.mkdir()
    options = dict(clients=setup.CLIENTS, scope='project', project=str(project), push_url=push_url)
    result, lines = run(home, env, **options)
    mcp_json, codex_toml = project/'.mcp.json', project/'.codex'/'config.toml'
    assert result['exit_code'] == 0 and result['steps']['clients']['changed'] is True
    assert result['steps']['clients']['detail']['files'] == {str(mcp_json): 'written', str(codex_toml): 'written'}
    claude = json.loads(mcp_json.read_text(encoding='utf-8'))['mcpServers']['workmcp']
    codex = tomllib.loads(codex_toml.read_text(encoding='utf-8'))['mcp_servers']['workmcp']
    if push_url:
        assert claude == {'type': 'http', 'url': MCP, 'headers': {'Authorization': 'Bearer ${WORKMCP_MCP_TOKEN}'}}
        assert codex == {'url': MCP, 'bearer_token_env_var': 'WORKMCP_MCP_TOKEN', 'startup_timeout_sec': 30,
                         'tool_timeout_sec': 120}
    else:
        assert claude['command'] == PY and claude['args'] == ['-I', '-m', 'workmcp', 'serve']
        assert claude['env'] == {'WORKMCP_HOME': str(home)}
        assert codex['command'] == PY and codex['env'] == {'WORKMCP_HOME': str(home)}

    before = snapshot(project)
    result, _ = run(home, env, **options)
    assert result['steps']['clients']['changed'] is False and snapshot(project) == before
    assert set(result['steps']['clients']['detail']['files'].values()) == {'unchanged'}

    mcp_json.write_text('{"mcpServers": {"mine": {}}}', encoding='utf-8')
    result, lines = run(home, env, **options)
    assert mcp_json.read_text(encoding='utf-8') == '{"mcpServers": {"mine": {}}}'
    assert 'servers: mine' in result['steps']['clients']['detail']['files'][str(mcp_json)]
    assert any('pass --yes to overwrite' in line for line in lines)
    result, _ = run(home, env, yes=True, **options)
    assert result['steps']['clients']['detail']['files'][str(mcp_json)] == 'overwritten'
    assert json.loads(mcp_json.read_text(encoding='utf-8'))['mcpServers']['workmcp'] == claude
    assert not list(project.rglob('*.partial'))


def test_a_project_folder_inside_a_source_root_is_refused(home, env, docs, tmp_path):
    """Review M6 v1 M6-17: ``--scope project`` never writes its files into a folder the collector reads (a new
    ``--source`` or one already in ``config.toml``); exit 4, nothing written."""
    inner = docs/'project'
    inner.mkdir()
    options = dict(clients=setup.CLIENTS, scope='project', project=str(inner))
    result, lines = run(home, env, sources=(str(docs),), **options)
    assert result['exit_code'] == 4 and result['steps']['clients']['status'] == 'refused'
    assert any('lies inside the source root' in line for line in lines)
    assert not home.exists() and list(inner.iterdir()) == []
    assert run(home, env, sources=(str(docs),))[0]['exit_code'] == 0                 # config.toml names docs now
    result, _ = run(home, env, **options)
    assert result['exit_code'] == 4 and list(inner.iterdir()) == []
    outside = tmp_path/'project'
    outside.mkdir()
    assert run(home, env, **{**options, 'project': str(outside)})[0]['exit_code'] == 0


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows junctions only')
def test_an_existing_source_written_through_a_junction_is_not_asked_again(home, env, docs, tmp_path):
    """Review M6 v1 M6-16: a ``[[sources]]`` entry that names the folder through a junction and a ``--source``
    that names it directly are the same root: no "add these lines"."""
    import _winapi
    junction = tmp_path/'docs-link'
    _winapi.CreateJunction(str(docs), str(junction))
    try:
        assert run(home, env, sources=(str(junction),))[0]['exit_code'] == 0
        config.config_file(home).write_text(f"[[sources]]\npath = '{junction}'\n\n[index]\nmodel = 'none'\n",
                                            encoding='utf-8')
        result, lines = run(home, env, sources=(str(docs),))
        assert result['exit_code'] == 0 and result['steps']['config']['detail']['missing_sources'] == []
        assert not any('add these lines' in line for line in lines)
        result, lines = run(tmp_path/'other-home', env, sources=(str(junction),))
        assert any(f'is written as the folder it resolves to: {docs.resolve()}' in line for line in lines)
    finally:
        junction.rmdir()


def test_client_commands_and_project_files_are_strings_only(tmp_path):
    home = tmp_path/'h'
    assert setup.client_commands(home, PY, ('codex',), MCP) == [
        f'codex mcp add workmcp --url {MCP} --bearer-token-env-var WORKMCP_MCP_TOKEN']
    files = setup.project_files(home, PY, tmp_path/'p', ('claude',), None)
    assert list(files) == [tmp_path/'p'/'.mcp.json'] and not (tmp_path/'p').exists()


# --- dry run --------------------------------------------------------------------------------------------------

def test_dry_run_creates_nothing_and_marks_planned_steps(home, env, docs, tiny_model, tmp_path):
    project = tmp_path/'project'
    project.mkdir()
    result, lines = run(home, env, dry=True, model='download', sources=(str(docs),), push_url=URL, scan=True,
                        clients=setup.CLIENTS, scope='project', project=str(project))
    assert result['exit_code'] == 0 and result['dry_run'] is True
    assert statuses(result) == {'home': 'planned', 'config': 'planned', 'model': 'planned', 'clients': 'planned',
                                'doctor': 'skipped', 'scan': 'planned', 'index': 'skipped', 'upgrade': 'skipped'}
    assert not home.exists() and list(project.iterdir()) == []
    assert lines[0] == 'workmcp setup (dry run: nothing is written)'
    assert any(line.startswith('config: would write') for line in lines)
    assert any('would download from https://example.invalid' in line for line in lines)


def test_dry_run_on_a_finished_home_reports_nothing_to_do(home, env, docs):
    run(home, env, sources=(str(docs),))
    before = snapshot(home)
    result, lines = run(home, env, dry=True, sources=(str(docs),))
    assert result['exit_code'] == 0 and not any(record['changed'] for record in result['steps'].values())
    assert snapshot(home) == before


def test_cli_setup_runs_and_dry_runs_end_to_end(tmp_path, docs, monkeypatch, capsys):
    monkeypatch.setenv('USERPROFILE', str(tmp_path/'user'))
    for name in ('LOCALAPPDATA', 'APPDATA', 'HOME', 'CODEX_HOME', paths.PUSH_TOKEN_ENV, *paths.SYNC_ENV):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path/'cli-home'
    argv = ['setup', '--home', str(home), '--model', 'none', '--source', str(docs), '--push-url', URL,
            '--clients', 'none']
    assert cli.main([*argv, '--dry-run']) == 0
    assert not home.exists() and 'config: would write' in capsys.readouterr().out
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert 'config: written' in out and '--- doctor ---' in out and 'role: collector' in out
    assert cli.main(argv) == 0
    assert 'config: exists, not changed' in capsys.readouterr().out


# --- upgrade ----------------------------------------------------------------------------------------------------

def make_store(home: Path, monkeypatch=None, version: int | None = None) -> Path:
    paths.ensure_home(home)
    if version is None:
        store.open_home(home).close()
    else:
        with monkeypatch.context() as patch:
            patch.setattr(store, 'MIGRATIONS', store.MIGRATIONS[:version])
            store.open_home(home).close()
    return paths.db_path(home)


def backups(home: Path) -> list[Path]:
    return sorted((home/'db').glob('backup-*.sqlite3'))


def test_upgrade_without_a_store_skips_and_runs_doctor(home, env):
    result, lines = run(home, env, upgrade=True, model=None)
    assert result['exit_code'] == 0 and statuses(result)['upgrade'] == 'skipped'
    assert statuses(result)['doctor'] == 'done'
    assert all(statuses(result)[step] == 'skipped' for step in ('config', 'model', 'clients', 'scan', 'index'))
    assert not (home/'config.toml').exists()


def test_upgrade_refuses_with_exit_3_while_a_lock_is_held_then_backs_up(home, env):
    make_store(home)
    lock = store.acquire_lock(home, store.INDEX_LOCK_NAME)[0]
    result, lines = run(home, env, upgrade=True, model=None)
    assert result['exit_code'] == 3 and result['steps']['upgrade']['status'] == 'refused'
    assert backups(home) == [] and lock.exists()
    assert not (home/store.LOCK_NAME).exists()  # the lock taken before the refusal was released
    store.release_lock(lock)

    result, lines = run(home, env, upgrade=True, model=None)
    assert result['exit_code'] == 0
    detail = result['steps']['upgrade']['detail']
    assert detail['schema_from'] == detail['schema_to'] == store.SCHEMA_VERSION
    assert [Path(detail['backup'])] == backups(home)
    assert f'-v{store.SCHEMA_VERSION}.sqlite3' in detail['backup']
    assert f'schema: v{store.SCHEMA_VERSION} -> v{store.SCHEMA_VERSION} (no migration needed)' in lines
    db = sqlite3.connect(detail['backup'])
    try:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == store.SCHEMA_VERSION
    finally:
        db.close()
    assert not any((home/name).exists() for name in (store.LOCK_NAME, store.INDEX_LOCK_NAME, 'push.lock'))


def test_upgrade_releases_only_its_own_locks(home, env, monkeypatch):
    """Review M5 remote v3 V3-5: a lock another run took over meanwhile (another nonce) stays."""
    make_store(home)
    backup_store = setup.backup_store
    foreign = {'pid': 1, 'started_at': '2026-10-29T09:00:00+09:00', 'heartbeat_at': '2026-10-29T09:00:00+09:00',
               'nonce': 'f' * 16}

    def taken_over_during_backup(target):
        (home/store.LOCK_NAME).write_text(json.dumps(foreign), encoding='utf-8')
        return backup_store(target)
    monkeypatch.setattr(setup, 'backup_store', taken_over_during_backup)
    result, lines = run(home, env, upgrade=True, model=None)
    assert result['exit_code'] == 0 and result['steps']['upgrade']['status'] == 'done'
    assert json.loads((home/store.LOCK_NAME).read_text(encoding='utf-8')) == foreign
    assert not any((home/name).exists() for name in (store.INDEX_LOCK_NAME, 'push.lock'))


def test_upgrade_migrates_an_older_store_after_the_backup(home, env, monkeypatch):
    make_store(home, monkeypatch, version=store.SCHEMA_VERSION - 1)
    old = store.SCHEMA_VERSION - 1
    assert setup.schema_version(paths.db_path(home)) == old
    result, lines = run(home, env, upgrade=True, model=None)
    assert result['exit_code'] == 0
    assert f'schema: v{old} -> v{store.SCHEMA_VERSION} (migrated)' in lines
    assert backups(home)[0].name.endswith(f'-v{old}.sqlite3')
    assert setup.schema_version(backups(home)[0]) == old
    assert setup.schema_version(paths.db_path(home)) == store.SCHEMA_VERSION


def test_upgrade_of_a_store_with_a_changed_schema_checksum_exits_2(home, env):
    db_file = make_store(home)
    db = sqlite3.connect(db_file)
    db.execute("UPDATE schema_migrations SET checksum='changed' WHERE version=1")
    db.commit()
    db.close()
    result, lines = run(home, env, upgrade=True, model=None)
    assert result['exit_code'] == 2 and result['steps']['upgrade']['status'] == 'refused'
    assert any('development schema change' in line for line in lines)


def test_upgrade_dry_run_backs_up_nothing(home, env):
    make_store(home)
    result, lines = run(home, env, dry=True, upgrade=True, model=None)
    assert result['exit_code'] == 0 and result['steps']['upgrade']['status'] == 'planned'
    assert backups(home) == []

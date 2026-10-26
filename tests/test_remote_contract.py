"""M5 remote stage 0 contract (design M5 remote §1.2, §4.1, §4.6, §11.1): ``[push]``/``[server]`` settings, the new
CLI commands and their argument exits, the push token file rule, the token helpers, and the stage-0 shells of
``remote``/``push``/``receive``. Stage 1 replaces the "not implemented" exits with the real bodies (``test_remote.py``,
``test_push.py``, ``test_receive.py``). No network: nothing here binds or connects."""
import argparse
from pathlib import Path

import pytest

from workmcp import cli, config, paths, push, receive, remote, server, store, sync

PUSH_URL = 'https://srv.example.invalid/workmcp'


def parse(text: str) -> config.Config:
    return config.parse_config(text, Path('config.toml'))


def test_defaults_of_the_new_settings():
    cfg = parse('')
    assert cfg.push == config.PushSettings(url=None)
    s = cfg.server
    assert (s.http, s.http_path, s.allowed_hosts, s.push_max_mb, s.model_preload) == (
        '127.0.0.1:8765', '/workmcp', (), 8, False)


def test_push_and_server_tables_are_read():
    cfg = parse(f"[push]\nurl = '{PUSH_URL}'\n[server]\nhttp = '[::1]:9000'\nhttp_path = '/wm'\n"
                "allowed_hosts = ['srv.example.invalid']\npush_max_mb = 16\nmodel_preload = true\n")
    assert cfg.push.url == PUSH_URL
    assert (cfg.server.http, cfg.server.http_path, cfg.server.allowed_hosts, cfg.server.push_max_mb,
            cfg.server.model_preload) == ('[::1]:9000', '/wm', ('srv.example.invalid',), 16, True)
    assert parse("[push]\nurl = 'http://127.0.0.1:48765/workmcp'\n").push.url.startswith('http://')  # test server


@pytest.mark.parametrize('text, message', [
    ("[push]\nurl = 'http://srv.example.invalid/workmcp'\n", 'loopback'),
    ("[push]\nurl = 'srv.example.invalid'\n", 'https://'),
    ("[push]\nurl = 3\n", 'string'),
    ("[push]\ntoken = 'x'\n", r'unknown \[push\] key'),
    ("[server]\nhttp = '0.0.0.0:8765'\n", 'loopback'),
    ("[server]\nhttp = 'localhost:8765'\n", 'loopback'),
    ("[server]\nhttp = '127.0.0.1:0'\n", 'port'),
    ("[server]\nhttp = '127.0.0.1'\n", 'PORT'),
    ("[server]\nhttp_path = 'workmcp'\n", 'http_path'),
    ("[server]\nhttp_path = '/workmcp/'\n", 'http_path'),
    ("[server]\nallowed_hosts = ['https://srv.example.invalid']\n", 'not URLs'),
    ("[server]\npush_max_mb = 65\n", 'push_max_mb'),
    ("[server]\nmodel_preload = 'yes'\n", 'model_preload'),
    ("[remote]\nx = 1\n", 'unknown table'),
])
def test_bad_new_settings_are_refused(text, message):
    with pytest.raises(config.ConfigError, match=message):
        parse(text)


@pytest.mark.parametrize('text, expected', [('127.0.0.1:8765', ('127.0.0.1', 8765)), ('[::1]:1', ('::1', 1)),
                                            ('::1:8765', ('::1', 8765))])
def test_parse_http_accepts_loopback_only(text, expected):
    assert config.parse_http(text) == expected


@pytest.mark.parametrize('text', ['0.0.0.0:80', ':8765', '[::]:8765', '127.0.0.2:8765', '127.0.0.1:65536', None, '8765'])
def test_parse_http_refuses(text):
    with pytest.raises(config.ConfigError):
        config.parse_http(text)


# ---- CLI arguments and exits -------------------------------------------------------------------------------

@pytest.fixture
def home(workmcp_home):
    return paths.ensure_home(paths.resolve_home())


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def write_config(home, text):
    config.config_file(home).write_text(text, encoding='utf-8')


@pytest.mark.parametrize('argv, code, message', [
    (['serve', '--http', '0.0.0.0:8765'], 4, 'loopback'),
    (['serve', '--http', 'localhost:8765'], 4, 'loopback'),
    (['serve', '--http', '127.0.0.1:8765'], 1, 'not implemented'),   # stage 0
    (['serve', '--http'], 1, 'not implemented'),                     # [server] http
    (['token', 'issue', '--kind', 'other', '--name', 'x'], 4, '--kind'),
    (['token', 'issue', '--kind', 'mcp', '--name', 'two words'], 4, '--name'),
    (['token', 'issue', '--kind', 'mcp', '--name', 'a', '--collector', 'b'], 4, '--collector'),
    (['token', 'revoke', '0'], 4, 'ID'),
    (['token', 'issue', '--kind', 'push', '--name', 'company-pc'], 1, 'not implemented'),
    (['token', 'list'], 1, 'not implemented'),
    (['token', 'rotate', '3'], 1, 'not implemented'),
    (['collectors'], 1, 'not implemented'),
    (['collectors', 'retire', 'company-pc'], 1, 'not implemented'),
])
def test_server_commands_check_their_arguments_first(home, capsys, argv, code, message):
    got, out, err = run(capsys, *argv)
    assert (got, out) == (code, '') and message in err, err


def test_serve_http_refuses_a_bad_data_home(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv('WORKMCP_HOME', 'relative-home')
    assert run(capsys, 'serve', '--http', '127.0.0.1:8765')[0] == 2


def test_push_exits(home, capsys, monkeypatch):
    monkeypatch.delenv(paths.PUSH_TOKEN_ENV, raising=False)
    code, _, err = run(capsys, 'push')
    assert code == 4 and '[push] url is not set' in err
    write_config(home, "[push]\nurl = 'http://127.0.0.1:48765/workmcp'\n")
    code, _, err = run(capsys, 'push')
    assert code == 4 and '--allow-insecure-loopback' in err
    code, _, err = run(capsys, 'push', '--allow-insecure-loopback')
    assert code == 4 and 'push token file not found' in err and paths.PUSH_TOKEN_ENV in err
    assert run(capsys, 'push', '--allow-insecure-loopback', '--max-mb', '0')[0] == 4
    assert run(capsys, 'push', '--allow-insecure-loopback', '--limit-docs', '0')[0] == 4
    write_config(home, f"[push]\nurl = '{PUSH_URL}'\n")
    (home/paths.PUSH_TOKEN_FILE).write_text('synthetic-token\n', encoding='utf-8')
    code, out, err = run(capsys, 'push', '--dry-run')
    assert (code, out) == (1, '') and 'not implemented' in err and 'synthetic-token' not in err


def test_verify_stored_is_stage_1_and_refuses_root(home, capsys, tmp_path):
    store.open_home(home).close()
    assert run(capsys, 'verify', '--stored', '--root', str(tmp_path))[0] == 4
    code, _, err = run(capsys, 'verify', '--stored')
    assert code == 1 and 'not implemented' in err


def test_new_commands_have_the_design_arguments():
    """Parser signatures of design §4.6 (and push --allow-insecure-loopback of §6.3)."""
    seen = {}
    original = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        result = original(self, args, namespace)
        seen[tuple(args)] = vars(result)
        raise SystemExit(0)

    cases = {('serve', '--http', '127.0.0.1:1'): {'http': '127.0.0.1:1'},
             ('push', '--max-mb', '5', '--limit-docs', '2', '--token-file', 'x', '--dry-run', '--require-vectors',
              '--allow-insecure-loopback'): {'max_mb': 5, 'limit_docs': 2, 'token_file': 'x', 'dry_run': True,
                                             'require_vectors': True, 'allow_insecure_loopback': True},
             ('token', 'issue', '--kind', 'push', '--name', 'n', '--collector', 'c'): {
                 'token_command': 'issue', 'kind': 'push', 'name': 'n', 'collector': 'c'},
             ('token', 'rotate', '7'): {'token_command': 'rotate', 'id': 7},
             ('collectors', 'retire', 'n'): {'collectors_command': 'retire', 'name': 'n'},
             ('collectors',): {'collectors_command': None},
             ('verify', '--stored', '--doc', '3'): {'stored': True, 'doc': [3]}}
    argparse.ArgumentParser.parse_args = capture
    try:
        for argv in cases:
            with pytest.raises(SystemExit):
                cli.main(list(argv))
    finally:
        argparse.ArgumentParser.parse_args = original
    for argv, expected in cases.items():
        assert {key: seen[argv][key] for key in expected} == expected, argv


# ---- push token file ---------------------------------------------------------------------------------------

def test_read_token_stays_inside_the_data_home(tmp_path):
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    with pytest.raises(paths.TokenFileError, match='not found'):
        paths.read_token(home)
    (home/'push.token').write_text('\ufeffsynthetic-token\r\n', encoding='utf-8')
    assert paths.read_token(home) == 'synthetic-token'
    (home/'other.token').write_text('t2', encoding='utf-8')
    assert paths.read_token(home, 'other.token') == 't2' == paths.read_token(home, home/'other.token')
    outside = tmp_path/'outside.token'
    outside.write_text('t3', encoding='utf-8')
    for path in (outside, home/'..'/'outside.token', home):
        with pytest.raises(paths.TokenFileError, match='inside the data home'):
            paths.read_token(home, path)
    for text in ('', 'two tokens', '\n'):
        (home/'push.token').write_text(text, encoding='utf-8')
        with pytest.raises(paths.TokenFileError, match='one token'):
            paths.read_token(home)


def test_push_token_prefers_the_environment(tmp_path):
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    (home/'push.token').write_text('from-file', encoding='utf-8')
    options = push.Options()
    assert push.push_token(home, {paths.PUSH_TOKEN_ENV: ' from-env '}, options) == 'from-env'
    assert push.push_token(home, {paths.PUSH_TOKEN_ENV: ''}, options) == 'from-file'


# ---- remote, push and receive shells ---------------------------------------------------------------------

def test_token_helpers_keep_only_a_hash():
    token, digest, prefix = remote.new_token()
    assert len(token) == 43 and prefix == token[:remote.TOKEN_PREFIX_CHARS] and sync.is_sha256(digest)
    assert remote.token_sha256(token) == digest and remote.verify_token_sha(digest, token)
    assert not remote.verify_token_sha(digest, token[:-1]) and remote.new_token()[0] != token


def test_routes_and_instructions():
    assert remote.routes(config.ServerSettings()) == {'mcp': '/workmcp/mcp', 'push': '/workmcp/push/',
                                                      'healthz': '/workmcp/healthz'}
    assert set(remote.routes(config.ServerSettings())) == set(remote.ROUTE_NAMES)
    assert 'delivered_sources' in remote.REMOTE_INSTRUCTIONS and '원본 파일은 회사 PC에만' in remote.REMOTE_INSTRUCTIONS
    assert remote.TOKEN_KINDS == ('mcp', 'push') and remote.REFUSAL_REASONS == ('missing', 'unknown', 'revoked',
                                                                                'wrong_kind')


def test_build_server_takes_the_remote_instructions(tmp_path):
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    store.open_home(home).close()
    assert server.build_server(home).instructions == server.INSTRUCTIONS
    built = server.build_server(home, instructions=remote.REMOTE_INSTRUCTIONS)
    assert built.instructions == remote.REMOTE_INSTRUCTIONS and built.workmcp_state.role == 'local'


@pytest.mark.parametrize('call', [
    lambda: remote.issue_token(None, 'mcp', 'n'), lambda: remote.list_tokens(None), lambda: remote.revoke_token(None, 1),
    lambda: remote.rotate_token(None, 1), lambda: remote.lookup_token(None, 't', 'mcp'),
    lambda: remote.retire_collector(None, 'n'), lambda: remote.auth_settings(config.ServerSettings()),
    lambda: remote.transport_security(config.ServerSettings()), lambda: remote.open_state(Path('.'), None),
    lambda: remote.build_remote(Path('.'), None, None),
    lambda: remote.main(Path('.'), '127.0.0.1', 1, config.Config(Path('c'), False, config.ScanSettings(), ())),
    lambda: push.execute(Path('.'), None, 't', push.Options()), lambda: push.report_lines({}),
    lambda: receive.apply(None, {}, {}, raw_bytes=0), lambda: receive.state(None, {}, model_key=None),
    lambda: receive.state_page(None, {}), lambda: receive.vectors_page(None, {}, model_key='k'),
])
def test_stage_0_bodies_are_not_implemented(call):
    with pytest.raises(NotImplementedError):
        call()


def test_push_contract_constants():
    assert push.REPORT_KEYS[0] == 'exit_code' and 'bytes_sent' in push.REPORT_KEYS and 'batches' in push.REPORT_KEYS
    assert (push.EXIT_OK, push.EXIT_FAILED, push.EXIT_HOME, push.EXIT_USAGE, push.EXIT_UNREACHABLE) == (0, 1, 2, 4, 6)
    assert push.check_options(push.Options()) is None
    assert receive.COLLECTOR_KEYS == ('id', 'name', 'retired_at')
    assert remote.EXIT_PORT_IN_USE == 5

"""The MCP adapter (design M3 §1, §6, §8): five async unstructured tools, the common ``handle`` path,
client names, error envelopes, the call log and the idle-release ticker. Synthetic data only; no
stdio here (``test_network_boundary.py`` runs the real ``serve``)."""
import inspect
import json
import logging
import sqlite3
from types import SimpleNamespace

import anyio
import pytest
from mcp.server.mcpserver import Context

from workmcp import config, embedder, paths, search, server, store, tools


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def home(tmp_path):
    return paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))


@pytest.fixture
def state(home):
    db = store.open_home(home)
    clock = Clock()
    engine = search.Engine(home, config.SearchSettings(model_idle_seconds=60),
                           embedder_factory=lambda: embedder.HashEmbedder(64), clock=clock)
    value = server.make_state(home, engine=engine, write_db=db)
    value.clock = clock
    yield value
    db.close()


def calls(state):
    return state.write_db.execute('SELECT client, client_version, tool, ok, error_code, pieces, chars FROM tool_calls '
                                  'ORDER BY id').fetchall()


def test_build_server_registers_five_async_unstructured_tools(state):
    built = server.build_server(state.home, state=state)
    listed = {tool.name: tool for tool in built._tool_manager.list_tools()}
    assert set(listed) == set(tools.TOOL_NAMES)
    for tool in listed.values():
        assert tool.is_async and inspect.iscoroutinefunction(tool.fn)
        assert tool.output_schema is None          # structured_output=False: one text content, our JSON
        assert tool.context_kwarg == 'ctx'         # the client name comes from the request Context
        assert 'ctx' not in tool.parameters.get('properties', {})
    assert built.instructions == server.INSTRUCTIONS
    assert built.workmcp_state is state


def test_server_call_returns_the_envelope_string_unchanged(state):
    built = server.build_server(state.home, state=state)
    result = anyio.run(built.call_tool, 'status', {})
    assert len(result.content) == 1 and not result.is_error
    text = result.content[0].text
    assert tools.dumps(json.loads(text)) == text  # compact JSON exactly as handle made it
    assert calls(state) == [('unknown', '', 'status', 1, None, 0, 0)]  # a Context outside a request


def test_handle_returns_compact_json_envelope_and_logs_the_call(state):
    text = server.handle(state, 'status', {}, ('claude-code', '2.1'))
    envelope = json.loads(text)
    assert envelope['ok'] and set(envelope['result']) == tools.STATUS_KEYS and envelope['delivered_sources'] == []
    assert text == tools.dumps(envelope) and ': ' not in text[:40]
    assert calls(state) == [('claude-code', '2.1', 'status', 1, None, 0, 0)]
    stage0 = json.loads(server.handle(state, 'list_documents', {'limit': 3}, ('codex', '')))
    assert stage0['error']['code'] == tools.NOT_IMPLEMENTED
    assert calls(state)[-1] == ('codex', '', 'list_documents', 0, 'not_implemented', 0, 0)
    status = json.loads(server.handle(state, 'status', {}, ('codex', '')))['result']
    assert status['calls'] == {'total': 2, 'by_client': {'claude-code': 1, 'codex': 1}}


def fake_context(name=None, version=None, *, params=True):
    info = SimpleNamespace(name=name, version=version)
    session = SimpleNamespace(client_params=SimpleNamespace(client_info=info) if params else None)
    return SimpleNamespace(session=session)


def test_client_name_comes_from_client_info_or_unknown():
    assert server.client_of(fake_context('claude-code', '2.1.299')) == ('claude-code', '2.1.299')
    assert server.client_of(fake_context(params=False)) == ('unknown', '')
    assert server.client_of(fake_context('', None)) == ('unknown', '')
    assert server.client_of(None) == ('unknown', '')
    assert server.client_of(Context()) == ('unknown', '')  # outside a request the SDK raises ValueError
    name, version = server.client_of(fake_context('co\x00dex\n' + 'x' * 100, 'v\x1b1'))
    assert name == 'codex' + 'x' * 59 and version == 'v1'


@pytest.mark.parametrize('raised, code, retryable', [
    (search.SearchError('empty_query', 'the query is empty'), 'empty_query', False),
    (FileNotFoundError('store not created yet: C:\\x'), 'store_missing', False),
    (sqlite3.OperationalError('database is locked'), 'store_busy', True),
    (sqlite3.OperationalError('no such table: x'), 'internal_error', False),
])
def test_known_errors_become_envelopes(state, monkeypatch, raised, code, retryable):
    def fail(state, **args):
        raise raised
    monkeypatch.setitem(server.TOOLS, 'status', fail)
    envelope = json.loads(server.handle(state, 'status', {}, ('c', '')))
    assert envelope == {'ok': False, 'error': {'code': code, 'message': envelope['error']['message'],
                                               'retryable': retryable}}
    assert calls(state) == [('c', '', 'status', 0, code, 0, 0)]


def test_unexpected_exception_becomes_internal_error_with_class_name_only(state, monkeypatch, caplog):
    def fail(state, **args):
        raise ValueError('C:\\secret\\path 합성 본문')
    monkeypatch.setitem(server.TOOLS, 'status', fail)
    with caplog.at_level(logging.ERROR, logger='workmcp.server'):
        envelope = json.loads(server.handle(state, 'status', {}, ('c', '')))
    assert envelope['error'] == {'code': 'internal_error', 'message': 'ValueError', 'retryable': False}
    assert any(record.exc_info for record in caplog.records)  # the traceback goes to stderr, not the client


def test_call_log_failure_adds_a_notice_but_the_response_goes_out(state, capsys):
    state.write_db.close()
    envelope = json.loads(server.handle(state, 'status', {}, ('c', '')))
    assert envelope['ok'] and envelope['notice'][-1].startswith('로컬 전달 기록(deliveries) 실패: ')
    assert 'call log failed for status' in capsys.readouterr().err
    state.write_db = store.open_home(state.home)


def test_handle_without_write_db_logs_nothing(home):
    quiet = server.make_state(home)
    store.open_home(home).close()
    assert json.loads(server.handle(quiet, 'status', {}, ('c', '')))['ok']


def warm(state):
    assert state.engine.embedder() is not None and state.engine.status()['loaded']


def test_tool_call_start_releases_an_idle_model(state, capsys):
    warm(state)
    state.clock.now += 30
    server.handle(state, 'status', {}, ('c', ''))
    assert state.engine.status()['loaded'] and state.unloads == 0
    state.clock.now += 61
    status = json.loads(server.handle(state, 'status', {}, ('c', '')))['result']
    assert not state.engine.status()['loaded'] and state.unloads == 1
    assert status['engine']['unloads'] == 1 and status['engine']['loaded'] is False
    assert 'unloaded model after idle' in capsys.readouterr().err


def test_lifespan_ticker_unloads_idle_model(state):
    warm(state)
    state.clock.now += 61
    state.tick = 0.01

    async def session():
        async with server._lifespan(state)(None) as context:
            assert context == {}
            for _ in range(200):
                if state.unloads:
                    break
                await anyio.sleep(0.01)

    anyio.run(session)
    assert state.unloads == 1 and not state.engine.status()['loaded']


def test_make_state_reads_the_server_settings(home):
    config.config_file(home).write_text('[server]\nresponse_max_chars = 8000\nunload_tick_seconds = 5\n',
                                        encoding='utf-8')
    made = server.make_state(home)
    assert made.budget == 8000 - tools.NOTICE_RESERVE and made.tick == 5.0
    assert server.make_state(paths.ensure_home(home.parent/'other')).budget == \
        tools.RESPONSE_MAX_CHARS - tools.NOTICE_RESERVE


@pytest.mark.parametrize('text, message', [
    ('[server]\nresponse_max_chars = 50000\n', 'response_max_chars must be an integer 4000-40000'),
    ('[server]\nresponse_max_chars = 3999\n', 'response_max_chars must be an integer 4000-40000'),
    ('[server]\nunload_tick_seconds = 0\n', 'unload_tick_seconds must be an integer 1-300'),
    ('[server]\nunload_tick_seconds = 1.5\n', 'unload_tick_seconds must be an integer 1-300'),
    ('[server]\nport = 8000\n', "unknown [server] key ['port']"),
])
def test_server_settings_are_checked(tmp_path, text, message):
    with pytest.raises(config.ConfigError, match=message.replace('[', r'\[').replace(']', r'\]')):
        config.parse_config(text, tmp_path/'config.toml')


def test_main_refuses_bad_settings_with_exit_4(workmcp_home, capsys):
    paths.ensure_home(workmcp_home)
    config.config_file(workmcp_home).write_text('[server]\nresponse_max_chars = 1\n', encoding='utf-8')
    assert server.main() == 4
    captured = capsys.readouterr()
    assert captured.out == '' and 'response_max_chars' in captured.err

"""The MCP adapter (design M3 §1, §6, §8): five async unstructured tools, the common ``handle`` path,
client names, error envelopes, the call log and the idle-release ticker. Synthetic data only; no
stdio here (``test_network_boundary.py`` runs the real ``serve``)."""
import inspect
import json
import logging
import sqlite3
import time
from types import SimpleNamespace

import anyio
import pytest
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from fixtures import LINES_KO
from test_collect import add_root, scan
from workmcp import config, embedder, index, locator, parsers, paths, search, server, snippet, store, tools


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
    listed = json.loads(server.handle(state, 'list_documents', {'limit': 3}, ('codex', '')))
    assert listed['ok'] and listed['result']['documents'] == [] and listed['delivered_sources'] == []
    assert calls(state)[-1] == ('codex', '', 'list_documents', 1, None, 0, 0)
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
    (locator.LocatorError('locator_text_mismatch'), 'locator_invalid', False),
    (snippet.NotFound('chunk 7 not found'), 'not_found', False),
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
    assert envelope['ok'] and envelope['notice'][-1] == server.LOG_DELAYED_NOTICE.format(1)
    assert 'call log delayed for status (1 queued)' in capsys.readouterr().err
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


def test_locator_errors_carry_the_fixed_code_only(state, monkeypatch):
    def fail(state, **args):
        raise locator.LocatorError('locator_text_mismatch')
    monkeypatch.setitem(server.TOOLS, 'get_snippet', fail)
    envelope = json.loads(server.handle(state, 'get_snippet', {}, ('c', '')))
    assert envelope['error'] == {'code': 'locator_invalid', 'message': 'locator_text_mismatch', 'retryable': False}


def indexed(state, tmp_path):
    """A few synthetic lines scanned and indexed with the state's ``HashEmbedder``."""
    folder = tmp_path/'src'
    folder.mkdir()
    (folder/'a.txt').write_text('\n'.join(LINES_KO), encoding='utf-8')
    scan(state.write_db, add_root(state.write_db, folder))
    index.run_index(state.write_db, state.home, embedder=embedder.HashEmbedder(64))


def test_first_semantic_call_reports_model_loaded_now(state, tmp_path):
    indexed(state, tmp_path)
    lexical = json.loads(server.handle(state, 'search', {'query': '견적', 'mode': 'lexical'}, ('c', '')))
    assert lexical['result']['meta']['engine'] == {'model_loaded_now': False, 'load_ms': 0}
    assert not state.engine.status()['loaded']   # lexical search never loads the model
    first = json.loads(server.handle(state, 'search', {'query': '견적', 'mode': 'semantic'}, ('c', '')))
    engine = first['result']['meta']['engine']
    assert engine['model_loaded_now'] is True and isinstance(engine['load_ms'], (int, float))
    assert server.MODEL_LOADED_NOTICE.format(engine['load_ms']) in first['notice']
    second = json.loads(server.handle(state, 'search', {'query': '견적', 'mode': 'hybrid'}, ('c', '')))
    assert second['result']['meta']['engine'] == {'model_loaded_now': False, 'load_ms': 0}
    assert not any(line.startswith('의미 검색 모델을') for line in second['notice'])
    state.clock.now += 61
    again = json.loads(server.handle(state, 'search', {'query': '견적', 'mode': 'semantic'}, ('c', '')))
    assert state.unloads == 1 and again['result']['meta']['engine']['model_loaded_now'] is True


def test_serve_start_reports_app_virtualization_on_stderr(workmcp_home, monkeypatch, capsys):
    monkeypatch.setattr(server.MCPServer, 'run', lambda self, transport: None)
    assert server.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ''
    [line] = [text for text in captured.err.splitlines() if 'app virtualization' in text]
    assert line.startswith('workmcp: ') and line.endswith(
        paths.virtualization_line(workmcp_home, paths.virtualization_probe(workmcp_home, __import__('os').environ)))


# ---- review M3-1: the call log never holds a call up -----------------------------------------------

def logged_calls(home) -> list[tuple]:
    db = store.open_read(home)
    try:
        return db.execute('SELECT tool, call_id FROM tool_calls ORDER BY id').fetchall()
    finally:
        db.close()


def test_a_running_scan_does_not_hold_tool_calls_up_and_the_log_catches_up(state, tmp_path, monkeypatch, capsys):
    """A scan in progress keeps its write transaction open (``collect.scan``); a tool call made then
    answers at once (well under a second), its log rows wait in memory and are written by the next call."""
    folder = tmp_path/'src'
    folder.mkdir()
    for n in range(3):
        (folder/f'{n}.txt').write_text('\n'.join(f'{n}번 {line}' for line in LINES_KO), encoding='utf-8')
    scanner = store.open_home(state.home)  # the scan's own connection, as another process would have
    root = add_root(scanner, folder)
    real_parse = parsers.parse
    during = []

    def parse_and_call(data, ext, limits):  # runs inside the scan's BEGIN IMMEDIATE ... COMMIT
        began = time.perf_counter()
        envelope = json.loads(server.handle(state, 'list_documents', {}, ('c', '')))
        during.append((time.perf_counter() - began, envelope))
        return real_parse(data, ext, limits)

    monkeypatch.setattr(parsers, 'parse', parse_and_call)
    try:
        scan(scanner, root)
    finally:
        scanner.close()
    monkeypatch.setattr(parsers, 'parse', real_parse)
    assert len(during) == 3
    for seconds, envelope in during:
        assert seconds < 1.0 and envelope['ok']
    assert [envelope['notice'][-1] for _, envelope in during] == \
        [server.LOG_DELAYED_NOTICE.format(n) for n in (1, 2, 3)]
    assert 'call log delayed for list_documents' in capsys.readouterr().err
    assert logged_calls(state.home) == [] and len(state.pending) == 3
    queued = [call['call_id'] for call, _ in state.pending]
    server.handle(state, 'status', {}, ('c', ''))  # the store is free again: the queue goes first
    assert state.pending == []
    assert logged_calls(state.home) == [*(('list_documents', call_id) for call_id in queued),
                                        ('status', logged_calls(state.home)[-1][1])]


def hold_write_lock(home):
    other = store.open_home(home)
    other.execute('BEGIN IMMEDIATE')
    return other


def test_ticker_writes_the_queue_and_close_reports_what_is_lost(state, capsys):
    other = hold_write_lock(state.home)
    began = time.perf_counter()
    hits = json.loads(server.handle(state, 'status', {}, ('c', '')))
    assert time.perf_counter() - began < 1.0 and hits['ok'] and len(state.pending) == 1
    other.execute('COMMIT')
    other.close()
    state.tick = 0.01

    async def session():
        async with server._lifespan(state)(None):
            for _ in range(200):
                if not state.pending:
                    break
                await anyio.sleep(0.01)

    anyio.run(session)
    assert state.pending == [] and [row[0] for row in logged_calls(state.home)] == ['status']
    other = hold_write_lock(state.home)
    try:
        server.handle(state, 'status', {}, ('c', ''))
        capsys.readouterr()
        assert server.close_log(state) == 1
        assert '1 call log entries were not written' in capsys.readouterr().err
    finally:
        other.execute('ROLLBACK')
        other.close()


def test_make_state_shortens_the_log_connection_busy_timeout(state):
    assert state.write_db.execute('PRAGMA busy_timeout').fetchone()[0] == server.LOG_BUSY_MS


# ---- review M3-3: every answer, errors too, fits the budget ----------------------------------------

def test_error_answers_fit_the_budget_and_messages_are_short(state, tmp_path):
    indexed(state, tmp_path)
    found = json.loads(server.handle(state, 'search', {'query': '견적', 'mode': 'lexical'}, ('c', '')))
    hit = found['result']['hits'][0]
    for unit in ({'kind': 'sheet_cell', 'sheet': 'S' * 60_000, 'cell': 'A1'},
                 {'kind': 'cell', 'row': 1, 'columns': list(range(1, 20_000))}):
        raw = server.handle(state, 'get_snippet', {'locator': {**hit['locator'], 'unit': unit}}, ('c', ''))
        envelope = json.loads(raw)
        assert len(raw) <= tools.RESPONSE_MAX_CHARS and envelope['ok'] is False
        assert envelope['error']['code'] == 'not_found' and len(envelope['error']['message']) <= tools.ERROR_MESSAGE_MAX
    assert len(tools.error('x', 'y' * 1000)['error']['message']) == tools.ERROR_MESSAGE_MAX


def test_the_final_length_check_catches_any_oversized_answer(state, monkeypatch):
    monkeypatch.setitem(server.TOOLS, 'status', lambda state, **args: {
        'ok': False, 'error': {'code': 'not_found', 'message': 'x' * 50_000, 'retryable': False}})
    raw = server.handle(state, 'status', {}, ('c', ''))
    assert json.loads(raw)['error']['code'] == 'response_too_large' and len(raw) < 200
    assert calls(state)[-1][3:5] == (0, 'response_too_large')


# ---- review L1: no silent integer or boolean conversion --------------------------------------------

@pytest.mark.parametrize('tool, arguments', [
    ('search', {'query': '견적', 'limit': True}),
    ('search', {'query': '견적', 'limit': 2.0}),
    ('search', {'query': '견적', 'collapse_by_document': 'yes'}),
    ('get_snippet', {'chunk_id': True}),
    ('get_snippet', {'chunk_id': 1, 'context_units': 1.0}),
    ('list_documents', {'offset': False}),
    ('recent_changes', {'limit': '5'}),
])
def test_integer_and_boolean_arguments_are_strict(state, tool, arguments):
    built = server.build_server(state.home, state=state)
    with pytest.raises(ToolError, match='validation error'):  # the SDK's argument error (isError on the wire)
        anyio.run(built.call_tool, tool, arguments)
    assert calls(state) == []  # our code never ran
    fine = anyio.run(built.call_tool, 'list_documents', {'limit': 3, 'offset': 0})
    assert not fine.is_error

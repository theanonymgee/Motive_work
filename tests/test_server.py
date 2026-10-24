"""The MCP adapter (design M3 §1, §6, §8, M4 §1): fifteen async unstructured tools, the common ``handle`` path,
client names, error envelopes, the call log and the idle-release ticker. Synthetic data only; no
stdio here (``test_network_boundary.py`` runs the real ``serve``)."""
import inspect
import json
import logging
import sqlite3
import sys
import time
from types import SimpleNamespace

import anyio
import pytest
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from fixtures import LINES_KO, SENTINEL
from test_collect import add_root, scan
from workmcp import config, embedder, index, knowledge, locator, parsers, paths, search, server, snippet, store, tools


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
    know = store.connect(paths.db_path(home))  # the knowledge write connection, as ``main`` opens it
    clock = Clock()
    engine = search.Engine(home, config.SearchSettings(model_idle_seconds=60),
                           embedder_factory=lambda: embedder.HashEmbedder(64), clock=clock)
    value = server.make_state(home, engine=engine, write_db=db, know_db=know)
    value.clock = clock
    yield value
    know.close()
    db.close()


def calls(state):
    return state.write_db.execute('SELECT client, client_version, tool, ok, error_code, pieces, chars FROM tool_calls '
                                  'ORDER BY id').fetchall()


def test_build_server_registers_fifteen_async_unstructured_tools(state):
    built = server.build_server(state.home, state=state)
    listed = {tool.name: tool for tool in built._tool_manager.list_tools()}
    assert set(listed) == set(tools.TOOL_NAMES) == set(server.TOOLS) and len(listed) == 15
    for tool in listed.values():
        assert tool.description  # a docstring, the client's only guide
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
    assert status['calls'] == {'total': 2, 'by_client': {'claude-code': 1, 'codex': 1}, 'pending': 0}


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
    (knowledge.KnowledgeError('store_busy', 'the store is busy; try again', retryable=True), 'store_busy', True),
    (knowledge.KnowledgeError('evidence_quote_not_found', 'sources[1]: no match in line 3'),
     'evidence_quote_not_found', False),
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


# ---- M4 stage 0: the knowledge write connection, recorded_by and call id (design M4 §7.2, §8.2) -----

def test_writers_get_the_knowledge_connection_recorded_by_and_the_logged_call_id(home, monkeypatch):
    write_db, know_db = store.open_home(home), store.open_home(home)
    seen = []

    def spy(db, **args):
        seen.append((db, args))
        return tools.ok({'assertion': None, 'conflicts': [], 'duplicates': []})
    try:
        state = server.make_state(home, engine=search.Engine(home), write_db=write_db, know_db=know_db)
        assert know_db.execute('PRAGMA busy_timeout').fetchone()[0] == server.KNOWLEDGE_BUSY_MS
        assert write_db.execute('PRAGMA busy_timeout').fetchone()[0] == server.LOG_BUSY_MS
        monkeypatch.setitem(server.TOOLS, 'record_task', server._writer('record_task', spy))
        envelope = json.loads(server.handle(state, 'record_task', {'statement': '합성', 'state': 'done', 'my_note': True},
                                            ('claude-code', '2.0')))
        assert envelope['ok'] is True
        [(db, args)] = seen
        call_id = write_db.execute("SELECT call_id FROM tool_calls WHERE tool='record_task'").fetchone()[0]
        assert db is know_db and args == {'recorded_by': 'claude-code/record_task', 'call_id': call_id,
                                          'statement': '합성', 'state': 'done', 'my_note': True}
    finally:
        write_db.close()
        know_db.close()


@pytest.mark.parametrize('tool, arguments, expected', [
    ('record_task', {'statement': '합성 할 일', 'state': 'done', 'my_note': True}, None),
    ('supersede', {'assertion_id': 1, 'reason': '합성 완료', 'state': 'done', 'my_note': True}, 'assertion_not_found'),
    ('list_knowledge', {'kind': 'todo', 'state': 'open'}, None),
])
def test_a_tool_argument_named_state_reaches_the_tool_function(state, tool, arguments, expected):
    """Through the SDK (``build_server``): an argument named ``state`` neither shadows the server state nor
    collides with ``TOOLS``' first parameter (a stage-0 shell answers not_implemented)."""
    built = server.build_server(state.home, state=state)
    result = anyio.run(built.call_tool, tool, arguments)
    envelope = json.loads(result.content[0].text)
    if not implemented(tool):
        expected = tools.NOT_IMPLEMENTED
    if expected is None:
        assert envelope['ok'] is True, envelope
        assert calls(state)[-1][2:5] == (tool, 1, None)
    else:
        assert envelope['error']['code'] == expected, envelope
        assert calls(state)[-1][2:5] == (tool, 0, expected)
    if tool == 'record_task' and expected is None:
        assert envelope['result']['assertion']['data']['state'] == 'done'


# ---- M4: the knowledge write path in the server (design M4 §6.1, §6.5, §8.2) -------------------------

def implemented(name: str) -> bool:
    """False while ``tools.<name>`` is still its stage-0 shell (design M4 §13.2: these tests turn on once
    the writers and readers land)."""
    return '_stage0(' not in inspect.getsource(getattr(tools, name))


needs_writers = pytest.mark.skipif(not all(map(implemented, tools.WRITE_TOOL_NAMES)),
                                   reason='knowledge writers are stage-0 shells (design M4 §13.2)')
KNOWLEDGE_TABLES = ('entities', 'assertions', 'evidence', 'links', 'assertion_events')


def counts(home) -> dict:
    db = store.open_read(home)
    try:
        return {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in KNOWLEDGE_TABLES}
    finally:
        db.close()


def logged(home) -> list[tuple]:
    db = store.open_read(home)
    try:
        return db.execute('SELECT tool, ok, error_code, call_id FROM tool_calls ORDER BY id').fetchall()
    finally:
        db.close()


def entity_insert(db, name):
    db.execute("INSERT INTO entities(kind, name, created_at, created_by) VALUES ('project', ?, ?, 'test')",
               (name, store.now()))


def locking_writer(db, *, recorded_by, call_id, kind, name, **args):
    """A stand-in writer with the knowledge write discipline (``store.begin_immediate`` ... ``store.commit``)."""
    store.begin_immediate(db)
    try:
        entity_insert(db, name)
        store.commit(db)
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    return tools.ok({'entity': {'id': 1}, 'created': True, 'added_aliases': [], 'alias_of': []})


def test_the_knowledge_connection_waits_for_the_lock_then_answers_store_busy_and_writes_nothing(state, monkeypatch):
    state.know_db.execute('PRAGMA busy_timeout=150')   # 5 s in the server; shortened here
    monkeypatch.setitem(server.TOOLS, 'record_entity', server._writer('record_entity', locking_writer))
    other = hold_write_lock(state.home)
    try:
        began = time.perf_counter()
        busy = json.loads(server.handle(state, 'record_entity', {'kind': 'project', 'name': '합성 P'}, ('c', '')))
        waited = time.perf_counter() - began
    finally:
        other.execute('ROLLBACK')
        other.close()
    assert busy == {'ok': False, 'error': {'code': 'store_busy', 'message': busy['error']['message'],
                                           'retryable': True}}
    assert 0.15 <= waited < 5            # it waited for the lock (plus the call log's own 200 ms), then gave up
    assert not state.know_db.in_transaction and counts(state.home)['entities'] == 0
    assert len(state.pending) == 1       # the call log waited too: queued, written by the next call
    done = json.loads(server.handle(state, 'record_entity', {'kind': 'project', 'name': '합성 P'}, ('c', '')))
    assert done['ok'] and counts(state.home)['entities'] == 1
    assert [row[:3] for row in logged(state.home)] == [('record_entity', 0, 'store_busy'), ('record_entity', 1, None)]


@pytest.mark.parametrize('failure', ['left_open', 'raised'])
def test_a_writer_that_leaves_its_transaction_open_is_rolled_back(state, monkeypatch, caplog, failure):
    def careless(db, *, recorded_by, call_id, kind, name, **args):
        store.begin_immediate(db)
        entity_insert(db, name)
        if failure == 'raised':
            raise RuntimeError('합성 본문')       # no rollback of its own
        return tools.ok({'entity': {'id': 1}, 'created': True, 'added_aliases': [], 'alias_of': []})
    monkeypatch.setitem(server.TOOLS, 'record_entity', server._writer('record_entity', careless))
    with caplog.at_level(logging.ERROR, logger='workmcp.server'):
        envelope = json.loads(server.handle(state, 'record_entity', {'kind': 'project', 'name': '합성 P'}, ('c', '')))
    assert envelope['error']['code'] == 'internal_error' and envelope['error']['retryable'] is False
    assert envelope['error']['message'] == ('TransactionLeftOpen' if failure == 'left_open' else 'RuntimeError')
    assert not state.know_db.in_transaction and counts(state.home)['entities'] == 0
    assert any('left a knowledge transaction open' in record.getMessage() for record in caplog.records)
    other = hold_write_lock(state.home)                # the lock is free again
    other.execute('ROLLBACK')
    other.close()
    assert logged(state.home)[-1][:3] == ('record_entity', 0, 'internal_error')


def test_a_server_without_a_knowledge_connection_writes_nothing(home):
    write_db = store.open_home(home)
    try:
        quiet = server.make_state(home, engine=search.Engine(home), write_db=write_db)
        envelope = json.loads(server.handle(quiet, 'record_entity', {'kind': 'project', 'name': '합성'}, ('c', '')))
    finally:
        write_db.close()
    assert envelope['error']['code'] == 'store_missing' and counts(home)['entities'] == 0


def test_a_stored_write_whose_answer_does_not_fit_names_the_stored_ids(state, monkeypatch):
    big = {'assertion': {'id': 7, 'statement': '합' * 50_000}, 'conflicts': [], 'duplicates': []}
    monkeypatch.setitem(server.TOOLS, 'record_fact', lambda st, **args: tools.ok(big))
    raw = server.handle(state, 'record_fact', {}, ('c', ''))
    envelope = json.loads(raw)
    assert envelope['error']['code'] == 'response_too_large' and envelope['error']['retryable'] is False
    message = envelope['error']['message']
    assert 'stored (id 7)' in message and 'do not repeat' in message and len(message) <= tools.ERROR_MESSAGE_MAX
    assert len(raw) <= tools.RESPONSE_MAX_CHARS and calls(state)[-1][2:5] == ('record_fact', 0, 'response_too_large')
    monkeypatch.setitem(server.TOOLS, 'get_entity', lambda st, **args: tools.ok(big))   # a reader: no such note
    message = json.loads(server.handle(state, 'get_entity', {}, ('c', '')))['error']['message']
    assert 'stored' not in message


@pytest.mark.parametrize('code', knowledge.ERROR_CODES)
def test_every_knowledge_error_code_becomes_its_envelope(state, monkeypatch, code):
    def fail(st, **args):
        raise knowledge.KnowledgeError(code, 'sources[2]: line 3, 12 characters' + ' x' * 200,
                                       retryable=code == 'store_busy')
    monkeypatch.setitem(server.TOOLS, 'record_fact', fail)
    envelope = json.loads(server.handle(state, 'record_fact', {}, ('c', '')))
    assert envelope['error']['code'] == code and envelope['error']['retryable'] is (code == 'store_busy')
    assert len(envelope['error']['message']) <= tools.ERROR_MESSAGE_MAX
    assert code in tools.ERROR_CODES


def test_store_busy_raised_by_a_writer_is_retryable(state, monkeypatch):
    def busy(st, **args):
        raise store.Busy('database is locked')
    monkeypatch.setitem(server.TOOLS, 'retract', busy)
    envelope = json.loads(server.handle(state, 'retract', {}, ('c', '')))
    assert envelope['error']['code'] == 'store_busy' and envelope['error']['retryable'] is True


# The real writers (design M4 §12 ⑤): turned on once they are implemented.

LONG_MARKS = tuple(f'합성긴줄{n:02d}' for n in range(1, 6))


def long_lines_home(state, tmp_path) -> list[dict]:
    """A synthetic file of five long lines (one unit each), scanned and indexed; returns one lexical hit
    per line (its locator names that line's unit)."""
    folder = tmp_path/'long'
    folder.mkdir()
    lines = [f'{mark} {SENTINEL} ' + ' '.join(f'{mark}-{i:03d} 가상 문장 {LINES_KO[i % len(LINES_KO)][-12:]}'
                                               for i in range(40)) for mark in LONG_MARKS]
    (folder/'long.txt').write_text('\n'.join(lines), encoding='utf-8')
    scan(state.write_db, add_root(state.write_db, folder))
    index.run_index(state.write_db, state.home, embedder=embedder.HashEmbedder(64))
    hits = []
    for mark in LONG_MARKS:
        found = json.loads(server.handle(state, 'search', {'query': f'{mark}-000', 'mode': 'lexical', 'limit': 1},
                                         ('c', '')))['result']['hits']
        hits.append(found[0])
    return hits


def unit_text(home, obj) -> str:
    from workmcp import binding
    db = store.open_read(home)
    try:
        unit_id = binding.validate_locator(db, obj)['unit_id']
        return db.execute('SELECT text FROM units WHERE id = ?', (unit_id,)).fetchone()[0]
    finally:
        db.close()


def new_entity(state, name='합성 과제') -> int:
    made = json.loads(server.handle(state, 'record_entity', {'kind': 'project', 'name': name}, ('c', '')))
    assert made['ok'], made
    return made['result']['entity']['id']


@needs_writers
def test_real_writers_wait_for_the_lock_then_answer_store_busy_and_write_nothing(state):
    entity = new_entity(state)
    before = counts(state.home)
    state.know_db.execute('PRAGMA busy_timeout=150')
    other = hold_write_lock(state.home)
    try:
        answers = [json.loads(server.handle(state, tool, args, ('c', ''))) for tool, args in (
            ('record_entity', {'kind': 'person', 'name': '합성 사람'}),
            ('record_fact', {'entity_id': entity, 'predicate': '상태', 'value': '합성 값', 'my_note': True}),
            ('record_task', {'statement': '합성 할 일', 'my_note': True}))]
    finally:
        other.execute('ROLLBACK')
        other.close()
    assert [(a['ok'], a.get('error', {}).get('code'), a.get('error', {}).get('retryable')) for a in answers] == \
        [(False, 'store_busy', True)] * 3
    assert counts(state.home) == before and not state.know_db.in_transaction
    server.handle(state, 'status', {}, ('c', ''))                 # the queued log rows go first
    assert [row[:3] for row in logged(state.home)][-4:-1] == [
        ('record_entity', 0, 'store_busy'), ('record_fact', 0, 'store_busy'), ('record_task', 0, 'store_busy')]


@needs_writers
def test_write_tool_errors_are_envelopes_and_write_nothing(state, tmp_path, capsys):
    hits = long_lines_home(state, tmp_path)
    entity = new_entity(state, f'합성 과제 {SENTINEL}')
    before = counts(state.home)
    fact = {'entity_id': entity, 'predicate': '상태', 'value': f'{SENTINEL} 값'}
    cases = [
        ('record_fact', {**fact}, 'evidence_required'),
        ('record_fact', {**fact, 'my_note': True, 'sources': [{'chunk_id': hits[0]['chunk_id'], 'quote': 'xx'}]},
         'evidence_required'),
        ('record_fact', {**fact, 'sources': [{'chunk_id': hits[0]['chunk_id'], 'quote': f'없는 문장 {SENTINEL}'}]},
         'evidence_quote_not_found'),
        ('record_fact', {**fact, 'sources': [{'chunk_id': 999_999, 'quote': f'{SENTINEL} xx'}]}, 'chunk_not_found'),
        ('record_fact', {**fact, 'entity_id': 999_999, 'my_note': True}, 'entity_not_found'),
        ('record_fact', {**fact, 'valid_from': '2026-13-01', 'my_note': True}, 'argument_invalid'),
        ('supersede', {'assertion_id': 999_999, 'reason': f'합성 {SENTINEL}', 'my_note': True}, 'assertion_not_found'),
        ('retract', {'assertion_id': 999_999, 'reason': f'합성 {SENTINEL}'}, 'assertion_not_found'),
    ]
    for tool, args, code in cases:
        envelope = json.loads(server.handle(state, tool, args, ('c', '')))
        assert envelope == {'ok': False, 'error': {'code': code, 'message': envelope['error']['message'],
                                                   'retryable': False}}, (tool, code, envelope)
        message = envelope['error']['message']
        assert SENTINEL not in message and len(message) <= tools.ERROR_MESSAGE_MAX
    assert counts(state.home) == before and not state.know_db.in_transaction
    db = store.open_read(state.home)
    try:
        rows = db.execute('SELECT tool, error_code, args_json FROM tool_calls ORDER BY id').fetchall()[-len(cases):]
    finally:
        db.close()
    assert [(tool, code) for tool, code, _ in rows] == [(tool, code) for tool, _, code in cases]
    assert not any(SENTINEL in args for _, _, args in rows) and SENTINEL not in capsys.readouterr().err


@needs_writers
def test_write_answers_fit_the_budget_and_deliveries_match_the_quotes_left(home, tmp_path):
    """Five 1,000-character quotes do not fit a 4,000-character budget: the answer drops quotes (design M4
    §6.5), every quote left is in delivered_sources and in deliveries, and the write keeps all five."""
    config.config_file(home).write_text('[server]\nresponse_max_chars = 4000\n', encoding='utf-8')
    write_db, know_db = store.open_home(home), store.connect(paths.db_path(home))
    try:
        engine = search.Engine(home, config.SearchSettings(), embedder_factory=lambda: embedder.HashEmbedder(64))
        state = server.make_state(home, engine=engine, write_db=write_db, know_db=know_db)
        hits = long_lines_home(state, tmp_path)
        entity = new_entity(state)
        sources = [{'locator': hit['locator'], 'quote': unit_text(home, hit['locator'])[:knowledge.QUOTE_MAX]}
                   for hit in hits]
        assert all(len(source['quote']) == knowledge.QUOTE_MAX for source in sources)
        raw = server.handle(state, 'record_fact', {'entity_id': entity, 'predicate': '근거', 'value': '합성 값',
                                                   'sources': sources}, ('c', ''))
    finally:
        know_db.close()
        write_db.close()
    envelope = json.loads(raw)
    assert envelope['ok'] and len(raw) <= 4000, envelope.get('error')
    evidence = envelope['result']['assertion']['evidence']
    left = [item for item in evidence if isinstance(item.get('quote'), str)]
    assert len(evidence) == 5 and len(left) < 5
    assert all(item['quote'] is None and item['quote_chars'] == knowledge.QUOTE_MAX for item in evidence
               if item not in left)
    assert envelope['delivered_sources'] == [locator.display(item['locator']) for item in left]
    db = store.open_read(home)
    try:
        delivered_rows = db.execute("SELECT d.chars FROM deliveries d JOIN tool_calls c ON c.call_id = d.call_id "
                                    "WHERE c.tool = 'record_fact' ORDER BY d.id").fetchall()
        stored = db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]
    finally:
        db.close()
    assert [row[0] for row in delivered_rows] == [len(item['quote']) for item in left] and stored == 5


class Serve:
    """``python -I -m workmcp serve`` (the client config's command) driven one request at a time (test code
    starts the process; the package starts none)."""

    def __init__(self, home, client: str):
        import os
        import subprocess
        import threading
        env = {**os.environ, 'WORKMCP_HOME': str(home)}
        self.proc = subprocess.Popen([sys.executable, '-I', '-m', 'workmcp', 'serve'], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
                                     encoding='utf-8')
        self.watchdog = threading.Timer(120, self.proc.kill)
        self.watchdog.start()
        self.send({**INITIALIZE, 'params': {**INITIALIZE['params'], 'clientInfo': {'name': client, 'version': '0'}}})
        assert self.reply(1)['result']['serverInfo']['name'] == 'workmcp'
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + '\n')
        self.proc.stdin.flush()

    def reply(self, number: int) -> dict:
        while True:
            line = self.proc.stdout.readline()
            assert line, self.proc.stderr.read()
            message = json.loads(line)
            if message.get('id') == number:
                return message

    def envelope(self, number: int) -> dict:
        return json.loads(self.reply(number)['result']['content'][0]['text'])

    def close(self) -> tuple[int, str]:
        try:
            self.proc.stdin.close()
            code = self.proc.wait(timeout=60)
            return code, self.proc.stderr.read()
        finally:
            self.watchdog.cancel()
            self.proc.kill()


@needs_writers
def test_two_servers_write_the_same_store(home):
    """Two serve processes (two clients) on one store, both writing while a third connection holds the
    write lock: once it is released their writes run one after the other (design M4 §8.2): the same entity
    is created once, and two facts with different values are both marked as conflicting."""
    from datetime import datetime
    store.open_home(home).close()
    servers = [Serve(home, 'proc-a'), Serve(home, 'proc-b')]
    released = []

    def race(number, name, *arguments):
        lock = hold_write_lock(home)
        try:
            for proc, args in zip(servers, arguments):
                proc.send({'jsonrpc': '2.0', 'id': number, 'method': 'tools/call',
                           'params': {'name': name, 'arguments': args}})
            time.sleep(1.5)                     # both wait on the lock (busy timeout 5 s)
        finally:
            released.append(datetime.now().astimezone().replace(microsecond=0))
            lock.execute('ROLLBACK')
            lock.close()
        return [proc.envelope(number) for proc in servers]

    try:
        made = race(2, 'record_entity', *[{'kind': 'project', 'name': '합성 동시 과제'}] * 2)
        assert all(e['ok'] for e in made), made
        assert sorted(e['result']['created'] for e in made) == [False, True]
        [entity] = {e['result']['entity']['id'] for e in made}
        facts = race(3, 'record_fact', *({'entity_id': entity, 'predicate': '상태', 'value': f'합성 값 {n}',
                                          'my_note': True} for n in (1, 2)))
        assert all(e['ok'] for e in facts), facts
        ids = [e['result']['assertion']['id'] for e in facts]
        marked = sorted([c['id'] for c in e['result']['conflicts']] for e in facts)
        assert marked in ([[], [ids[0]]], [[], [ids[1]]])   # the later write sees the earlier one
    finally:
        closed = [proc.close() for proc in servers]
    assert [code for code, _ in closed] == [0, 0], closed
    db = store.open_read(home)
    try:
        entities = db.execute('SELECT created_by, created_at FROM entities').fetchall()
        rows = db.execute('SELECT id, status, recorded_by, recorded_from FROM assertions ORDER BY id').fetchall()
        links = db.execute("SELECT from_id, to_id FROM links WHERE relation = 'conflicts' AND resolved_at IS NULL"
                           ).fetchall()
        clients = db.execute('SELECT client, tool, ok FROM tool_calls ORDER BY id').fetchall()
    finally:
        db.close()
    assert len(entities) == 1 and entities[0][0] in ('proc-a/record_entity', 'proc-b/record_entity')
    assert datetime.fromisoformat(entities[0][1]) >= released[0]          # written after the release
    assert [(row[0], row[1]) for row in rows] == [(min(ids), 'conflict'), (max(ids), 'conflict')]
    assert {row[2] for row in rows} == {'proc-a/record_fact', 'proc-b/record_fact'}
    assert all(datetime.fromisoformat(row[3]) >= released[1] for row in rows)
    assert sorted(map(sorted, links)) == [sorted(ids)]
    assert sorted(clients) == sorted((client, tool, 1) for client in ('proc-a', 'proc-b')
                                     for tool in ('record_entity', 'record_fact'))


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
    ('record_fact', {'entity_id': True, 'predicate': '담당', 'my_note': True}),
    ('record_fact', {'entity_id': 1, 'predicate': '담당', 'my_note': 'yes'}),
    ('record_task', {'statement': '합성', 'owner_entity_id': 1.0, 'my_note': True}),
    ('supersede', {'assertion_id': '1', 'reason': '합성', 'my_note': True}),
    ('retract', {'assertion_id': 1.0, 'reason': '합성'}),
    ('get_entity', {'entity_id': 1, 'include_closed': 1}),
    ('list_knowledge', {'offset': True}),
    ('get_history', {'assertion_id': False}),
])
def test_integer_and_boolean_arguments_are_strict(state, tool, arguments):
    built = server.build_server(state.home, state=state)
    with pytest.raises(ToolError, match='validation error'):  # the SDK's argument error (isError on the wire)
        anyio.run(built.call_tool, tool, arguments)
    assert calls(state) == []  # our code never ran
    fine = anyio.run(built.call_tool, 'list_documents', {'limit': 3, 'offset': 0})
    assert not fine.is_error


# ---- review M3 v2 N1: serve starts while a scan holds the write lock -------------------------------

def start_serve(home, *messages):
    """``python -I -m workmcp serve`` (the client config's command) with the messages on stdin; returns
    the process (test code starts it; the package starts no process)."""
    import os
    import subprocess
    env = {**os.environ, 'WORKMCP_HOME': str(home)}
    proc = subprocess.Popen([sys.executable, '-I', '-m', 'workmcp', 'serve'], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True, encoding='utf-8')
    proc.stdin.write(''.join(json.dumps(m) + '\n' for m in messages))
    proc.stdin.flush()
    return proc


INITIALIZE = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
              'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'n1', 'version': '0'}}}


def test_serve_starts_and_answers_while_another_process_holds_the_write_lock(home):
    import threading
    store.open_home(home).close()                       # an up-to-date store
    other = store.open_home(home)
    other.execute('BEGIN IMMEDIATE')                    # what a running scan holds
    began = time.perf_counter()
    proc = start_serve(home, INITIALIZE, {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                       {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'status', 'arguments': {}}})
    watchdog = threading.Timer(60, proc.kill)
    watchdog.start()
    try:
        replies = {}
        while len(replies) < 2:
            line = proc.stdout.readline()
            assert line, proc.stderr.read()
            message = json.loads(line)
            replies[message['id']] = (message, time.perf_counter() - began)
        assert replies[1][0]['result']['serverInfo']['name'] == 'workmcp'
        status = json.loads(replies[2][0]['result']['content'][0]['text'])
        assert status['ok'] and status['result']['schema_version'] == store.SCHEMA_VERSION
        assert status['result']['calls']['pending'] == 0   # its own call is queued after the answer
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
        stderr = proc.stderr.read()
    finally:
        watchdog.cancel()
        proc.kill()
        other.execute('ROLLBACK')
        other.close()
    assert replies[1][1] < 10  # interpreter start and imports included; no 5-second lock wait
    db = store.open_read(home)
    try:
        written = db.execute('SELECT COUNT(*) FROM tool_calls').fetchone()[0]
    finally:
        db.close()
    assert written == 0 and 'call log entries were not written' in stderr  # held to the end: lost, counted


def test_serve_that_needs_a_migration_under_a_held_lock_exits_with_one_line(home):
    import sqlite3 as sqlite
    path = paths.db_path(home)
    other = sqlite.connect(path, isolation_level=None)
    other.execute('PRAGMA journal_mode=WAL')
    other.execute('BEGIN IMMEDIATE')
    other.execute('CREATE TABLE placeholder(x)')        # a new store whose creator still holds it
    try:
        proc = start_serve(home, INITIALIZE)
        out, err = proc.communicate(timeout=60)
    finally:
        other.execute('ROLLBACK')
        other.close()
    assert proc.returncode == 3 and out == ''
    lines = [line for line in err.splitlines() if 'app virtualization' not in line]
    assert len(lines) == 1 and lines[0].startswith('workmcp: cannot open the store') and 'Traceback' not in err


def test_status_shows_calls_waiting_in_the_queue(state):
    other = store.open_home(state.home)
    other.execute('BEGIN IMMEDIATE')
    try:
        first = json.loads(server.handle(state, 'status', {}, ('c', '')))
        second = json.loads(server.handle(state, 'status', {}, ('c', '')))
    finally:
        other.execute('ROLLBACK')
        other.close()
    assert first['result']['calls']['pending'] == 0 and second['result']['calls']['pending'] == 1
    assert second['notice'][-1] == server.LOG_DELAYED_NOTICE.format(2) and '잃을 수 있음' in second['notice'][-1]

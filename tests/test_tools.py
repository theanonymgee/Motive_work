"""Tool functions (design M3 §2-§6): envelope helpers, delivered pieces, the size budget, the call-log
summary, the five tools through ``server.handle`` (the path ``serve`` takes, without stdio), binding
re-derivation of every delivered piece (§5, D-1..D-5), the size budget per tool (S-1..S-3) and the
long-lived engine across scans and index runs (E-2, with mutation checks). Synthetic text only."""
import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from fixtures import SENTINEL
from fixtures import bundle as synthetic_bundle
from fixtures import corpus as corpus_module
from test_collect import add_root, rewrite, scan
from workmcp import (binding, collect, config, embedder, index, knowledge, knowledge_query, locator, parsers, paths,
                     search, server, snippet, store, tools)

H = '0' * 64
DIMS = 64
CLIENT = ('tools-test', '1.0')


@pytest.fixture
def home(tmp_path):
    return paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))


def piece(n, text):
    unit = {'kind': 'line', 'line': n}
    return locator.make(1, 'C:\\synthetic\\a.txt', H, unit, text, [0, len(text)]), text


def search_envelope(count, size=100):
    hits = []
    for n in range(1, count + 1):
        obj, text = piece(n, f'합성 문장 {n} ' + '가' * size)
        hits.append({'rank': n, 'locator': obj, 'snippet': text, 'units': [obj]})
    return tools.ok({'hits': hits, 'meta': {}, 'notice': []})


def snippet_envelope():
    before, main, after = piece(1, '앞 문맥'), piece(2, '본문 조각'), piece(3, '뒤 문맥')
    as_piece = lambda pair: {'locator': pair[0], 'text': pair[1], 'truncated': False}  # noqa: E731
    return tools.ok({'doc': {}, 'version': {}, 'chunk': None, 'context_before': [as_piece(before)],
                     'pieces': [as_piece(main)], 'context_after': [as_piece(after)]}), (before, main, after)


def test_dumps_is_compact_and_keeps_korean():
    assert tools.dumps({'a': [1, 2], 'b': '합성'}) == '{"a":[1,2],"b":"합성"}'


def test_pieces_of_reads_hits_and_snippet_pieces_in_response_order():
    envelope = search_envelope(3)
    assert [text for _, text in tools.pieces_of(envelope)] == [h['snippet'] for h in envelope['result']['hits']]
    snip, (before, main, after) = snippet_envelope()
    assert tools.pieces_of(snip) == [before, main, after]
    assert tools.delivered(snip) == [locator.display(obj) for obj, _ in (before, main, after)]
    assert tools.pieces_of(tools.ok({'documents': [{'path': 'C:\\synthetic\\a.txt'}]})) == []
    assert tools.pieces_of(tools.error('empty_query', 'x')) == []


def drop_last_hit(envelope):
    hits = envelope['result']['hits']
    if not hits:
        return False
    hits.pop()
    return True


def test_fit_drops_whole_units_until_the_budget_and_recomputes_delivered_sources():
    envelope = search_envelope(10, size=300)
    full = len(tools.dumps(tools.fit(search_envelope(10, size=300), tools.shrink_none, 10**6)))
    fitted = tools.fit(envelope, drop_last_hit, full // 2)
    assert len(tools.dumps(fitted)) <= full // 2
    hits = fitted['result']['hits']
    assert 0 < len(hits) < 10
    assert fitted['delivered_sources'] == [locator.display(h['locator']) for h in hits]


def test_fit_answers_response_too_large_when_nothing_more_can_go():
    envelope = tools.fit(search_envelope(2), drop_last_hit, 10)
    assert envelope['ok'] is False and envelope['error']['code'] == 'response_too_large'
    assert tools.fit(search_envelope(1), tools.shrink_none, 50)['error']['code'] == 'response_too_large'
    error = tools.error('empty_query', 'x')
    assert tools.fit(error, drop_last_hit, 1) is error


def test_constants_match_the_design():
    assert (tools.RESPONSE_MAX_CHARS, tools.PIECE_MAX_CHARS, tools.CONTEXT_UNITS_MAX, tools.LIST_LIMIT_MAX) == \
        (40_000, 4_000, 5, 500)
    assert set(search.ERROR_CODES) < set(tools.ERROR_CODES) and tools.NOT_IMPLEMENTED not in tools.ERROR_CODES
    assert config.RESPONSE_MAX_CHARS_RANGE[1] == tools.RESPONSE_MAX_CHARS
    assert set(tools.SHRINK) == set(tools.TOOL_NAMES)


def test_summarize_args_keeps_no_query_path_or_locator_text():
    query = 'SENTINEL 합성 질의'
    summary = tools.summarize_args('search', {'query': query, 'mode': 'hybrid', 'limit': 5,
                                              'path_prefix': 'C:\\SENTINEL', 'ext': None, 'since': None, 'until': None,
                                              'collapse_by_document': True})
    assert summary == {'mode': 'hybrid', 'limit': 5, 'filters': ['path_prefix'], 'query_chars': len(query),
                       'query_sha12': summary['query_sha12'], 'collapse': True}
    assert len(summary['query_sha12']) == 12
    assert tools.summarize_args('search', {'query': 5, 'mode': 'SENTINEL', 'limit': 'x'})['mode'] == 'invalid'
    obj, _ = piece(1, 'SENTINEL 본문')
    snip = tools.summarize_args('get_snippet', {'chunk_id': None, 'locator': obj, 'context_units': 2,
                                                'max_chars': 4000})
    assert snip == {'by': 'locator', 'chunk_id': None, 'doc': 1, 'context_units': 2, 'max_chars': 4000}
    listed = tools.summarize_args('list_documents', {'path_prefix': 'C:\\SENTINEL', 'status': 'all', 'limit': 3,
                                                     'offset': 0})
    assert listed == {'filters': ['path_prefix'], 'status': 'all', 'limit': 3, 'offset': 0}
    recent = tools.summarize_args('recent_changes', {'kinds': ['added', 'SENTINEL'], 'since': '2026-10-01'})
    assert recent == {'filters': ['kinds', 'since'], 'kinds': ['added'], 'limit': None, 'offset': None}
    assert tools.summarize_args('status', {}) == {}
    for value in (summary, snip, listed, recent):
        assert 'SENTINEL' not in json.dumps(value, ensure_ascii=False)


def test_status_has_engine_virtualization_calls_and_process(home):
    assert tools.status(home)['error']['code'] == 'store_missing'
    store.open_home(home).close()
    clock = lambda: 100.0  # noqa: E731
    engine = search.Engine(home, config.SearchSettings(model_idle_seconds=60),
                           embedder_factory=lambda: embedder.HashEmbedder(64), clock=clock)
    result = tools.status(home, engine, started_at='2026-10-24T09:00:00+09:00', unloads=2)['result']
    assert set(result) == tools.STATUS_KEYS and result['schema_version'] == store.SCHEMA_VERSION == 3
    assert result['knowledge'] == {'entities': 0, 'assertions': {'active': 0, 'conflict': 0, 'withdrawn': 0,
                                                                 'superseded': 0},
                                   'tasks_open': 0, 'evidence': 0, 'events': 0}
    assert set(result['engine']) >= {'loaded', 'rows', 'load_failure'} | tools.ENGINE_STATUS_EXTRA_KEYS
    assert result['engine']['loaded'] is False and result['engine']['idle_seconds'] is None
    assert (result['engine']['model_idle_seconds'], result['engine']['unloads']) == (60, 2)
    engine.embedder()
    warm = tools.status(home, engine)['result']['engine']
    assert (warm['loaded'], warm['idle_seconds'], warm['loads']) == (True, 0.0, 1)
    assert set(result['home_virtualization']) == set(paths.VIRTUALIZATION_KEYS)
    assert result['calls'] == {'total': 0, 'by_client': {}, 'pending': 0}
    assert result['process']['started_at'] == '2026-10-24T09:00:00+09:00' and len(result['process']['python']) == 3
    assert tools.status(home)['result']['engine'] is None


# ---- the tools through server.handle --------------------------------------------------------------

def new_home(base: Path) -> Path:
    return paths.ensure_home(paths.validate_home(str(base/'home'), {}, None))


def hash_engine(home: Path, **settings) -> search.Engine:
    return search.Engine(home, config.SearchSettings(**settings), embedder_factory=lambda: embedder.HashEmbedder(DIMS))


def run_index(home: Path) -> dict:
    db = store.open_home(home)
    try:
        return index.run_index(db, home, embedder=embedder.HashEmbedder(DIMS))
    finally:
        db.close()


def rescan(home: Path, root) -> dict:
    db = store.open_home(home)
    try:
        return scan(db, root)
    finally:
        db.close()


class Built:
    """A data home with the synthetic bundle and corpus scanned and indexed (``HashEmbedder``)."""

    def __init__(self, base: Path):
        self.home = new_home(base)
        self.src = base/'src'
        synthetic_bundle.write_bundle(self.src/'bundle')
        self.corpus = corpus_module.build()
        corpus_module.write(self.corpus, self.src/'corpus')
        db = store.open_home(self.home)
        try:
            self.root = add_root(db, self.src)
            scan(db, self.root)
        finally:
            db.close()
        run_index(self.home)

    def state(self, **options) -> server.ServerState:
        state = server.make_state(self.home, engine=hash_engine(self.home), write_db=store.open_home(self.home))
        for key, value in options.items():
            setattr(state, key, value)
        return state


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    return Built(tmp_path_factory.mktemp('tools'))


@pytest.fixture
def state(built):
    value = built.state()
    yield value
    value.write_db.close()


def call(state, tool: str, /, **args) -> dict:  # positional: list_knowledge has an argument 'state'
    return json.loads(server.handle(state, tool, args, CLIENT))


def last_call(state) -> tuple:
    return state.write_db.execute('SELECT call_id, client, tool, ok, error_code, pieces, chars, args_json '
                                  'FROM tool_calls ORDER BY id DESC LIMIT 1').fetchone()


def body_pieces(envelope: dict) -> list[tuple[dict, str]]:
    """Every piece of document text in a response, read from the response structure (design M3 §3.1),
    independently of ``tools.pieces_of``: hit snippets; context before, pieces, context after."""
    if not envelope['ok']:
        return []
    result = envelope['result']
    found = [(hit['locator'], hit['snippet']) for hit in result.get('hits', ())]
    for key in ('context_before', 'pieces', 'context_after'):
        found += [(piece['locator'], piece['text']) for piece in result.get(key, ())]
    return found


def first_hits(state, query='합성 프로젝트', **args) -> list[dict]:
    envelope = call(state, 'search', query=query, **args)
    assert envelope['ok'], envelope
    assert envelope['result']['hits'], envelope
    return envelope['result']['hits']


def test_search_tool_exposes_m2_contract_plus_signals_and_collapse(state):
    for mode in search.MODES:
        envelope = call(state, 'search', query='합성 프로젝트 주간 회의', mode=mode, limit=5)  # fits the budget
        assert envelope['ok'] and set(envelope) == {'ok', 'result', 'delivered_sources', 'notice'}
        result = envelope['result']
        assert set(result) == search.RESULT_KEYS
        assert set(result['meta']) == search.META_KEYS | tools.SEARCH_META_EXTRA_KEYS
        assert set(result['meta']['signals']) == search.SIGNAL_KEYS
        assert set(result['meta']['engine']) == tools.SEARCH_ENGINE_KEYS
        assert result['meta']['truncated_hits'] == 0 and result['meta']['mode'] == mode
        assert 0 < len(result['hits']) <= 5
        engine_hits = state.engine.search('합성 프로젝트 주간 회의', mode=mode, limit=5)['hits']  # the M2 result
        assert [h['chunk_id'] for h in engine_hits] == [h['chunk_id'] for h in result['hits']]
        for hit, full in zip(result['hits'], engine_hits):
            assert search.HIT_KEYS <= set(hit) <= search.HIT_KEYS | search.HIT_OPTIONAL_KEYS
            assert all(set(ref) == tools.UNIT_REF_KEYS for ref in hit['units'])
            assert tools.expand_units(hit) == full['units']           # compact units rebuild the M2 locators
        assert envelope['delivered_sources'] == [locator.display(hit['locator']) for hit in result['hits']]
    collapsed = call(state, 'search', query='합성 프로젝트', mode='hybrid', limit=10, collapse_by_document=True)
    hits = collapsed['result']['hits']
    assert len({hit['doc']['id'] for hit in hits}) == len(hits) and [h['rank'] for h in hits] == \
        list(range(1, len(hits) + 1))
    plain = call(state, 'search', query='합성 프로젝트', mode='hybrid', limit=10)['result']
    if len({hit['doc']['id'] for hit in plain['hits']}) < len(plain['hits']):
        assert collapsed['result']['meta']['collapsed'] > 0
    assert last_call(state)[7] and json.loads(last_call(state)[7])['query_chars'] == len('합성 프로젝트')


def test_search_without_a_model_says_why_in_a_notice(built):
    state = server.make_state(built.home, engine=search.Engine(built.home), write_db=None)  # no model files
    envelope = call(state, 'search', query='합성 프로젝트', mode='hybrid')
    assert envelope['ok'] and envelope['result']['meta']['mode'] == 'lexical'
    assert envelope['result']['meta']['semantic_note'].startswith('model_missing')
    assert tools.SEMANTIC_NOTE_NOTICE.format('model_missing') in envelope['notice']
    lexical = call(state, 'search', query='합성 프로젝트', mode='lexical')
    assert not any(line.startswith('의미 검색 사용 불가') for line in lexical['notice'])


@pytest.mark.parametrize('args, code', [
    ({'query': '   '}, 'empty_query'),
    ({'query': 'x' * 1001}, 'query_too_long'),
    ({'query': '합성', 'limit': 51}, 'limit_out_of_range'),
    ({'query': '합성', 'mode': 'fuzzy'}, 'mode_invalid'),
    ({'query': '합성', 'ext': ['.exe']}, 'filter_invalid'),
])
def test_search_errors_are_envelopes(state, args, code):
    envelope = call(state, 'search', **args)
    assert envelope == {'ok': False, 'error': {'code': code, 'message': envelope['error']['message'],
                                               'retryable': False}}


def chunk_text(home: Path, chunk_id: int) -> str:
    db = store.open_read(home)
    try:
        return db.execute('SELECT text FROM chunks WHERE id=?', (chunk_id,)).fetchone()[0]
    finally:
        db.close()


def check_piece_shape(piece: dict) -> None:
    assert set(piece) == tools.PIECE_KEYS and isinstance(piece['truncated'], bool)


def test_get_snippet_by_chunk_returns_unit_pieces_with_exact_locators(state, built):
    hit = first_hits(state)[0]
    envelope = call(state, 'get_snippet', chunk_id=hit['chunk_id'])
    assert envelope['ok'], envelope
    result = envelope['result']
    assert set(result) == tools.SNIPPET_RESULT_KEYS
    assert result['chunk']['id'] == hit['chunk_id'] and result['version']['id'] == hit['version_id']
    assert result['version']['latest'] is True and result['version']['latest_version_id'] == hit['version_id']
    assert result['doc']['id'] == hit['doc']['id'] and result['context_before'] == result['context_after'] == []
    for piece in result['pieces']:
        check_piece_shape(piece)
        assert piece['truncated'] is False
    assert '\n'.join(piece['text'] for piece in result['pieces']) == chunk_text(built.home, hit['chunk_id'])
    assert envelope['delivered_sources'] == [locator.display(p['locator']) for p in result['pieces']]


def test_get_snippet_by_locator_returns_that_range(state):
    hit = first_hits(state)[0]
    envelope = call(state, 'get_snippet', locator=hit['locator'])
    assert envelope['ok'], envelope
    result = envelope['result']
    assert result['chunk'] is None and len(result['pieces']) == 1
    assert result['pieces'][0] == {'locator': hit['locator'], 'text': hit['snippet'], 'truncated': False}
    assert envelope['delivered_sources'] == [locator.display(hit['locator'])]


def test_get_snippet_context_units_are_whole_neighbouring_units(state, built):
    db = store.open_read(built.home)
    try:
        # a chunk in the middle of a version with at least two units on each side
        chunk_id, version_id, first, last = db.execute(
            'SELECT c.id, c.version_id, c.unit_first, c.unit_last FROM chunks c WHERE c.unit_first >= 3 AND '
            'c.unit_last + 2 <= (SELECT MAX(ordinal) FROM units u WHERE u.version_id = c.version_id) '
            'ORDER BY c.id LIMIT 1').fetchone()
        texts = dict(db.execute('SELECT ordinal, text FROM units WHERE version_id=?', (version_id,)).fetchall())
    finally:
        db.close()
    envelope = call(state, 'get_snippet', chunk_id=chunk_id, context_units=2)
    assert envelope['ok'], envelope
    result = envelope['result']
    assert [p['text'] for p in result['context_before']] == [texts[first - 2], texts[first - 1]]
    assert [p['text'] for p in result['context_after']] == [texts[last + 1], texts[last + 2]]
    for piece in result['context_before'] + result['context_after']:
        check_piece_shape(piece)
        assert piece['locator']['chars'] == [0, len(piece['text'])]
    assert envelope['delivered_sources'] == [locator.display(p['locator']) for p in
                                             result['context_before'] + result['pieces'] + result['context_after']]


def test_get_snippet_errors(state, built):
    hit = first_hits(state)[0]
    bad_hash = {**hit['locator'], 'text_sha256': hashlib.sha256(b'other').hexdigest()}
    wrong_doc = {**hit['locator'], 'doc': 999_999}
    cases = [
        ({}, 'argument_invalid'),
        ({'chunk_id': hit['chunk_id'], 'locator': hit['locator']}, 'argument_invalid'),
        ({'chunk_id': 0}, 'argument_invalid'),
        ({'chunk_id': hit['chunk_id'], 'context_units': 6}, 'argument_invalid'),
        ({'chunk_id': hit['chunk_id'], 'max_chars': 199}, 'argument_invalid'),
        ({'chunk_id': hit['chunk_id'], 'max_chars': 4001}, 'argument_invalid'),
        ({'locator': {'doc': 1}}, 'locator_invalid'),
        ({'locator': bad_hash}, 'locator_invalid'),
        ({'chunk_id': 99_999_999}, 'not_found'),
        ({'locator': wrong_doc}, 'not_found'),
    ]
    for args, code in cases:
        envelope = call(state, 'get_snippet', **args)
        assert envelope['ok'] is False and envelope['error']['code'] == code, (args, envelope)
        assert SENTINEL not in envelope['error']['message']
    missing = server.make_state(new_home(built.home.parent/'empty'), engine=hash_engine(built.home))
    assert call(missing, 'get_snippet', chunk_id=1)['error']['code'] == 'store_missing'


def changed_home(base: Path):
    """Three text files scanned and indexed; returns ``(home, root, folder)``."""
    home = new_home(base)
    folder = base/'src'
    folder.mkdir(parents=True)
    for name, topic in (('a.txt', '첫째'), ('b.txt', '둘째'), ('c.txt', '셋째')):
        (folder/name).write_text('\n'.join(f'{topic} 합성 문서의 {n}번째 줄 {SENTINEL}' for n in range(1, 8)),
                                 encoding='utf-8')
    db = store.open_home(home)
    try:
        root = add_root(db, folder)
        scan(db, root)
    finally:
        db.close()
    run_index(home)
    return home, root, folder


def test_get_snippet_of_an_old_version_is_returned_with_latest_false(tmp_path):
    home, root, folder = changed_home(tmp_path)
    state = server.make_state(home, engine=hash_engine(home))
    hit = first_hits(state, '첫째 합성 문서', mode='lexical')[0]
    rewrite(folder/'a.txt', ('첫째 합성 문서 새 판\n' + (folder/'a.txt').read_text(encoding='utf-8')).encode('utf-8'))
    rescan(home, root)
    old = call(state, 'get_snippet', locator=hit['locator'], context_units=1)
    assert old['ok'], old
    version = old['result']['version']
    assert version['id'] == hit['version_id'] and version['latest'] is False
    assert version['latest_version_id'] not in (None, hit['version_id'])
    assert snippet.OLD_VERSION_NOTICE.format(seq=version['seq'], latest=version['latest_version_id']) in old['notice']
    assert old['result']['pieces'][0]['text'] == hit['snippet']
    (folder/'a.txt').unlink()
    rescan(home, root)
    gone = call(state, 'get_snippet', chunk_id=hit['chunk_id'])
    assert gone['ok'] and gone['result']['version']['latest'] is False
    assert gone['result']['version']['latest_version_id'] is None
    assert gone['notice'] == [snippet.DELETED_NOTICE]


def test_list_documents_filters_pages_and_indexed_flag(built):
    state = built.state(budget=10**7)  # every row in one response (the size budget is S-3's)
    everything = call(state, 'list_documents', status='all', limit=500)
    assert everything['ok'] and everything['delivered_sources'] == []
    result = everything['result']
    assert set(result) == tools.LIST_RESULT_KEYS and result['has_more'] is False
    db = store.open_read(built.home)
    try:
        total = db.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM documents WHERE status='active'").fetchone()[0]
    finally:
        db.close()
    assert result['total'] == len(result['documents']) == total
    for row in result['documents']:
        assert set(row) == tools.DOCUMENT_KEYS
        assert row['latest'] is None or set(row['latest']) == tools.DOCUMENT_LATEST_KEYS
    assert [row['path'] for row in result['documents']] == sorted((row['path'] for row in result['documents']),
                                                                  key=os.path.normcase)
    assert any(row['indexed'] for row in result['documents'])
    assert all(row['indexed'] is False for row in result['documents']
               if row['latest'] is None or row['latest']['parse_status'] != 'ok')
    page = call(state, 'list_documents', limit=3, offset=3)['result']
    assert (page['total'], len(page['documents']), page['has_more']) == (active, 3, True)
    assert [r['id'] for r in page['documents']] == \
        [r['id'] for r in call(state, 'list_documents', limit=6)['result']['documents']][3:]
    texts = call(state, 'list_documents', ext=['.txt'], limit=500)['result']
    assert texts['documents'] and all(row['ext'] == '.txt' for row in texts['documents'])
    corpus_only = call(state, 'list_documents', path_prefix=str(built.src/'corpus'), limit=500)['result']
    assert corpus_only['total'] == len(built.corpus.topics)
    assert call(state, 'list_documents', changed_since='2999-01-01')['result']['total'] == 0
    assert call(state, 'list_documents', changed_since='2000-01-01')['result']['total'] == active
    for args, code in (({'status': 'gone'}, 'argument_invalid'), ({'limit': 0}, 'argument_invalid'),
                       ({'limit': 501}, 'argument_invalid'), ({'offset': -1}, 'argument_invalid'),
                       ({'ext': ['.exe']}, 'filter_invalid'), ({'changed_since': 'yesterday'}, 'filter_invalid')):
        assert call(state, 'list_documents', **args)['error']['code'] == code, args
    state.write_db.close()


def test_list_documents_marks_a_changed_document_not_indexed_until_index(tmp_path):
    home, root, folder = changed_home(tmp_path)
    state = server.make_state(home, engine=hash_engine(home))
    assert all(row['indexed'] for row in call(state, 'list_documents')['result']['documents'])
    rewrite(folder/'b.txt', '둘째 합성 문서 바뀜'.encode('utf-8'))
    rescan(home, root)
    rows = {Path(row['path']).name: row for row in call(state, 'list_documents')['result']['documents']}
    assert rows['b.txt']['indexed'] is False and rows['a.txt']['indexed'] is True
    run_index(home)
    assert all(row['indexed'] for row in call(state, 'list_documents')['result']['documents'])
    (folder/'c.txt').unlink()
    rescan(home, root)
    deleted = call(state, 'list_documents', status='deleted')['result']['documents']
    assert [Path(row['path']).name for row in deleted] == ['c.txt'] and deleted[0]['deleted_at']


def test_recent_changes_kinds_pages_and_default_window(tmp_path):
    home, root, folder = changed_home(tmp_path)
    state = server.make_state(home, engine=hash_engine(home), write_db=store.open_home(home))
    try:
        first = call(state, 'recent_changes')
        assert first['ok'] and first['delivered_sources'] == [] and first['notice'] == []
        result = first['result']
        assert set(result) == tools.CHANGES_RESULT_KEYS and result['until'] is None
        assert result['since'] is not None and result['total'] == 3
        assert {row['kind'] for row in result['changes']} == {'added'}
        for row in result['changes']:
            assert set(row) == tools.CHANGE_KEYS and set(row['doc']) == tools.CHANGE_DOC_KEYS
            assert row['scan_id'] and row['version_id'] and row['seq'] == 1 and row['parse_status'] == 'ok'
        rewrite(folder/'a.txt', '첫째 합성 문서 바뀜'.encode('utf-8'))
        (folder/'c.txt').unlink()
        rescan(home, root)
        changes = call(state, 'recent_changes')['result']['changes']
        assert [row['kind'] for row in changes][:2] in (['changed', 'deleted'], ['deleted', 'changed'])
        assert [row['id'] for row in changes] == sorted((row['id'] for row in changes), reverse=True)
        deleted = call(state, 'recent_changes', kinds=['deleted'])['result']
        assert deleted['total'] == 1 and deleted['changes'][0]['version_id'] is None
        assert Path(deleted['changes'][0]['doc']['path']).name == 'c.txt'
        assert deleted['changes'][0]['doc']['status'] == 'deleted'
        changed = call(state, 'recent_changes', kinds=['changed', 'added'], limit=2, offset=1)['result']
        assert (changed['total'], len(changed['changes']), changed['has_more']) == (4, 2, True)
        assert call(state, 'recent_changes', since='2999-01-01')['result']['total'] == 0
        assert call(state, 'recent_changes', until='2000-01-01', since='1999-01-01')['result']['total'] == 0
        assert call(state, 'recent_changes', ext=['.md'])['result']['total'] == 0
        for args, code in (({'kinds': ['moved']}, 'argument_invalid'), ({'kinds': []}, 'argument_invalid'),
                           ({'limit': 501}, 'argument_invalid'), ({'since': 'soon'}, 'filter_invalid'),
                           ({'since': '2031-01-02', 'until': '2031-01-01'}, 'filter_invalid')):
            assert call(state, 'recent_changes', **args)['error']['code'] == code, args
    finally:
        state.write_db.close()


def test_recent_changes_explains_scans_made_before_events_existed(tmp_path):
    home = new_home(tmp_path)
    db = store.open_home(home)
    try:
        db.execute("INSERT INTO scans(started_at, status, roots_json) VALUES('2026-10-01T00:00:00+09:00', 'done', '[]')")
    finally:
        db.close()
    envelope = call(server.make_state(home, engine=hash_engine(home)), 'recent_changes')
    assert envelope['ok'] and envelope['result']['total'] == 0 and envelope['notice'] == [tools.NO_EVENTS_NOTICE]


# ---- D-1..D-5: delivered sources (design M3 §3, §5) -----------------------------------------------

def reparsed_units(path: str, ext: str, cache: dict) -> dict:
    if path not in cache:
        data, _, _ = collect.read_source(path)
        cache[path] = {parsers.unit_json(u.unit): u.text for u in parsers.parse(data, ext, parsers.Limits()).units}
    return cache[path]


def check_delivery(state, envelope: dict, reparse: dict | None, sha_of: dict) -> int:
    """(a) delivered_sources == the pieces in order, (b) each piece re-derives from the store, (c) and from
    the re-parsed current file (``reparse`` None skips it), (d) the display names its four parts, (e) the
    deliveries rows of this call are exactly these pieces. Returns the number of pieces."""
    found = body_pieces(envelope)
    assert envelope['delivered_sources'] == [locator.display(obj) for obj, _ in found]
    call_id, client, tool, ok, _, pieces, chars, _ = last_call(state)
    assert (client, ok, pieces, chars) == (CLIENT[0], 1, len(found), sum(len(text) for _, text in found))
    db = store.open_read(state.home)
    try:
        ext = {row[0]: row[1] for row in db.execute('SELECT id, ext FROM documents')}
        for hit in (envelope['result'].get('hits', ()) if envelope['ok'] else ()):
            for unit in tools.expand_units(hit):                     # units[] carry no text, but bind exactly
                binding.validate_locator(db, unit)
        for obj, text in found:
            assert binding.validate_locator(db, obj)['text'] == text                      # (b)
            if reparse is not None:                                                         # (c)
                assert sha_of.setdefault(obj['path'], collect.read_source(obj['path'])[1]) == obj['sha256']
                start, end = obj['chars']
                assert reparsed_units(obj['path'], ext[obj['doc']], reparse)[parsers.unit_json(obj['unit'])][
                    start:end] == text
            shown = locator.display(obj)                                                    # (d)
            assert shown.startswith(obj['path']) and obj['sha256'][:12] in shown
            assert locator.unit_label(obj['unit']) in shown and f"chars {obj['chars'][0]}-{obj['chars'][1]}" in shown
        rows = db.execute('SELECT client, tool, locator_json, chars FROM deliveries WHERE call_id=? ORDER BY id',
                          (call_id,)).fetchall()
        assert len(rows) == len(found) and sum(row[3] for row in rows) == sum(len(t) for _, t in found)   # (e)
        for (row_client, row_tool, locator_json, row_chars), (obj, text) in zip(rows, found):
            assert (row_client, row_tool, row_chars) == (CLIENT[0], tool, len(text))
            assert binding.validate_locator(db, json.loads(locator_json))['text'] == text
    finally:
        db.close()
    return len(found)


def test_every_delivered_piece_rederives_from_the_current_file(tmp_path):
    built = Built(tmp_path)
    state = built.state()
    reparse, sha_of, delivered_total = {}, {}, 0
    queries = [q.text for q in built.corpus.queries if q.category != 'Q9_absent'][:9] + [SENTINEL]
    try:
        for query in queries:
            for mode in search.MODES:
                for collapse in (False, True):
                    envelope = call(state, 'search', query=query, mode=mode, limit=5, collapse_by_document=collapse)
                    assert envelope['ok'], envelope
                    delivered_total += check_delivery(state, envelope, reparse, sha_of)
                    for hit in envelope['result']['hits']:
                        for args in ({'chunk_id': hit['chunk_id'], 'context_units': 2},
                                     {'locator': hit['locator'], 'context_units': 1}):
                            piece = call(state, 'get_snippet', **args)
                            assert piece['ok'], piece
                            delivered_total += check_delivery(state, piece, reparse, sha_of)
        for tool, args in (('list_documents', {}), ('recent_changes', {}), ('status', {})):
            envelope = call(state, tool, **args)
            assert envelope['ok'] and check_delivery(state, envelope, reparse, sha_of) == 0
        db = store.open_read(built.home)
        try:
            report = binding.verify(db)
            assert report['ok'] and report['chunks_checked'] == db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
            assert db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0] == delivered_total > 0
        finally:
            db.close()

        # after a change (scan, no index): the old locator still gives the old piece, marked not latest
        hit = first_hits(state, '합성 프로젝트', mode='lexical', ext=['.txt'], path_prefix=str(built.src/'corpus'))[0]
        path = Path(hit['doc']['path'])
        rewrite(path, (path.read_text(encoding='utf-8') + '\n합성 변경 줄').encode('utf-8'))
        rescan(built.home, built.root)
        old = call(state, 'get_snippet', locator=hit['locator'])
        assert old['ok'] and old['result']['version']['latest'] is False
        check_delivery(state, old, None, sha_of)                    # (b) holds: the units are kept
        assert old['result']['version']['sha256'] != collect.read_source(str(path))[1]   # (c) would not
        for mode in search.MODES:
            again = call(state, 'search', query=SENTINEL, mode=mode, limit=50)['result']['hits']
            assert hit['version_id'] not in {h['version_id'] for h in again}
    finally:
        state.write_db.close()


def test_delivered_sources_equal_pieces_for_every_tool(state):
    hits = first_hits(state, limit=3)
    for tool, args in (('search', {'query': '합성 프로젝트', 'limit': 3}),
                       ('get_snippet', {'chunk_id': hits[0]['chunk_id'], 'context_units': 1}),
                       ('get_snippet', {'locator': hits[1]['locator'], 'context_units': 2}),
                       ('list_documents', {'limit': 5}), ('recent_changes', {'limit': 5}), ('status', {})):
        envelope = call(state, tool, **args)
        assert envelope['ok'], envelope
        found = body_pieces(envelope)
        assert envelope['delivered_sources'] == [locator.display(obj) for obj, _ in found]
        assert envelope['delivered_sources'] == tools.delivered(envelope)
        assert bool(found) == (tool in ('search', 'get_snippet'))


def test_error_responses_deliver_nothing_but_are_logged(state):
    before = state.write_db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0]
    for tool, args, code in (('search', {'query': ''}, 'empty_query'),
                             ('get_snippet', {}, 'argument_invalid'),
                             ('get_snippet', {'chunk_id': 99_999_999}, 'not_found'),
                             ('list_documents', {'status': 'x'}, 'argument_invalid'),
                             ('recent_changes', {'kinds': ['x']}, 'argument_invalid')):
        envelope = call(state, tool, **args)
        assert set(envelope) == {'ok', 'error'} and envelope['error']['code'] == code
        call_id, client, logged_tool, ok, error_code, pieces, chars, _ = last_call(state)
        assert (client, logged_tool, ok, error_code, pieces, chars) == (CLIENT[0], tool, 0, code, 0, 0)
        assert state.write_db.execute('SELECT COUNT(*) FROM deliveries WHERE call_id=?', (call_id,)).fetchone()[0] == 0
    assert state.write_db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0] == before


def test_deliveries_rows_match_pieces_and_call_id(state):
    search_envelope = call(state, 'search', query='합성 프로젝트', limit=4)
    search_call = last_call(state)
    hit = search_envelope['result']['hits'][0]
    snippet_envelope = call(state, 'get_snippet', chunk_id=hit['chunk_id'], context_units=1)
    snippet_call = last_call(state)
    assert search_call[0] != snippet_call[0] and re.fullmatch('[0-9a-f]{16}', search_call[0])
    for (call_id, *_), envelope in ((search_call, search_envelope), (snippet_call, snippet_envelope)):
        rows = state.write_db.execute('SELECT locator_json, chars FROM deliveries WHERE call_id=? ORDER BY id',
                                      (call_id,)).fetchall()
        assert [(json.loads(row[0]), row[1]) for row in rows] == [(obj, len(text)) for obj, text in
                                                                  body_pieces(envelope)]


def test_no_body_text_in_tool_calls_deliveries_or_stderr(state, capsys):
    hits = first_hits(state, f'{SENTINEL} 합성 프로젝트', limit=5)
    assert any(SENTINEL in hit['snippet'] for hit in hits)
    call(state, 'get_snippet', chunk_id=hits[0]['chunk_id'], context_units=2)
    call(state, 'get_snippet', locator=hits[0]['locator'])
    call(state, 'search', query=SENTINEL, mode='semantic')
    call(state, 'search', query=SENTINEL + '!' * 1000)  # query_too_long: the error names no query text
    db = state.write_db
    for table in ('tool_calls', 'deliveries'):
        for row in db.execute(f'SELECT * FROM {table}'):
            assert not any(SENTINEL in str(value) for value in row), (table, row)
    assert db.execute('SELECT COUNT(*) FROM units WHERE text LIKE ?', (f'%{SENTINEL}%',)).fetchone()[0] > 0
    captured = capsys.readouterr()
    assert SENTINEL not in captured.err and SENTINEL not in captured.out


# ---- S-1..S-3: the size budget per tool (design M3 §4) --------------------------------------------

def test_search_drops_whole_hits_to_fit_budget(built):
    roomy = built.state(budget=10**7)
    tight = built.state(budget=15_000)
    try:
        full = call(roomy, 'search', query='합성 프로젝트', limit=50)
        assert len(tools.dumps(full)) > 15_000
        text = server.handle(tight, 'search', {'query': '합성 프로젝트', 'limit': 50}, CLIENT)
        fitted = json.loads(text)
        assert len(text) <= 15_000 and fitted['ok']
        hits = fitted['result']['hits']
        dropped = len(full['result']['hits']) - len(hits)
        assert 0 < len(hits) and dropped > 0 and fitted['result']['meta']['truncated_hits'] == dropped
        assert [h['chunk_id'] for h in hits] == [h['chunk_id'] for h in full['result']['hits'][:len(hits)]]
        assert tools.SHRINK_HITS_NOTICE.format(dropped) in fitted['notice']
        assert fitted['delivered_sources'] == [locator.display(h['locator']) for h in hits]
        assert last_call(tight)[5] == len(hits)
        assert tight.write_db.execute('SELECT COUNT(*) FROM deliveries WHERE call_id=?',
                                      (last_call(tight)[0],)).fetchone()[0] == len(hits)
    finally:
        roomy.write_db.close()
        tight.write_db.close()


def long_lines_home(base: Path):
    """One text file of five lines of about 3,900 characters (words), scanned; returns ``(home, path)``."""
    home = new_home(base)
    folder = base/'src'
    folder.mkdir(parents=True)
    path = folder/'long.txt'
    path.write_text('\n'.join(' '.join(f'합성{line}낱말{n}' for n in range(430)) for line in range(1, 6)),
                    encoding='utf-8')
    db = store.open_home(home)
    try:
        scan(db, add_root(db, folder))
    finally:
        db.close()
    return home, path


def test_get_snippet_drops_context_then_shrinks_pieces(tmp_path):
    home, _ = long_lines_home(tmp_path)
    db = store.open_read(home)
    try:
        doc_id, sha256 = db.execute('SELECT d.id, v.sha256 FROM documents d JOIN versions v ON v.document_id = d.id'
                                    ).fetchone()
        unit_json, text = db.execute('SELECT locator_json, text FROM units WHERE ordinal = 3').fetchone()
        path = db.execute('SELECT path FROM documents').fetchone()[0]
    finally:
        db.close()
    whole = locator.make(doc_id, path, sha256, json.loads(unit_json), text, [0, len(text)])
    roomy = server.make_state(home, engine=hash_engine(home), write_db=store.open_home(home))
    full = call(roomy, 'get_snippet', locator=whole, context_units=2)
    assert full['ok'] and len(full['result']['context_before']) == len(full['result']['context_after']) == 2
    roomy.write_db.close()

    def fitted(budget):
        state = server.make_state(home, engine=hash_engine(home), write_db=store.open_home(home))
        state.budget = budget
        try:
            raw = server.handle(state, 'get_snippet', {'locator': whole, 'context_units': 2}, CLIENT)
            envelope = json.loads(raw)
            assert len(raw) <= budget and envelope['ok'], envelope
            assert envelope['delivered_sources'] == [locator.display(o) for o, _ in body_pieces(envelope)]
            call_id = last_call(state)[0]
            assert state.write_db.execute('SELECT COUNT(*) FROM deliveries WHERE call_id=?',
                                          (call_id,)).fetchone()[0] == len(body_pieces(envelope))
            return envelope
        finally:
            state.write_db.close()

    size = len(tools.dumps(full))
    unit = len(tools.dumps(full['result']['context_after'][-1]))  # one context piece, serialized
    three = fitted(size - 100)  # one context unit goes: the last one after
    assert len(three['result']['context_after']) == 1 and len(three['result']['context_before']) == 2
    assert tools.SHRINK_CONTEXT_NOTICE.format(1) in three['notice']
    one = fitted(size - 3 * unit + 50)  # three go: both after, then the first before
    assert three['result']['context_before'][1] == one['result']['context_before'][0]
    assert one['result']['context_after'] == [] and tools.SHRINK_CONTEXT_NOTICE.format(3) in one['notice']
    cut = fitted(3_000)  # no context left, the piece itself is halved at a word boundary
    assert cut['result']['context_before'] == cut['result']['context_after'] == []
    [piece] = cut['result']['pieces']
    assert piece['truncated'] is True and 0 < len(piece['text']) < len(text) and text.startswith(piece['text'])
    assert piece['locator']['chars'] == [0, len(piece['text'])]
    assert tools.TRUNCATED_NOTICE in cut['notice']
    assert tools.SHRINK_PIECE_NOTICE.format(len(piece['text'])) in cut['notice']
    db = store.open_read(home)
    try:
        assert binding.validate_locator(db, piece['locator'])['text'] == piece['text']
    finally:
        db.close()


def test_list_and_recent_shrink_rows_and_set_has_more(built):
    state = built.state(budget=4_000)
    try:
        for tool, key in (('list_documents', 'documents'), ('recent_changes', 'changes')):
            raw = server.handle(state, tool, {'limit': 500}, CLIENT)
            envelope = json.loads(raw)
            assert len(raw) <= 4_000 and envelope['ok']
            result = envelope['result']
            assert 0 < len(result[key]) < result['total'] and result['has_more'] is True
            assert result['limit'] == len(result[key])
            assert tools.SHRINK_ROWS_NOTICE.format(result['limit']) in envelope['notice']
            follow = json.loads(server.handle(state, tool, {'limit': result['limit'], 'offset': result['limit']},
                                              CLIENT))['result']
            assert follow[key][0]['id'] not in {row['id'] for row in result[key]}
    finally:
        state.write_db.close()


# ---- E-2: the long-lived server across scans and index runs (design M3 §6.4) ----------------------

def versions_of(home: Path, name: str) -> list[int]:
    db = store.open_read(home)
    try:
        return [row[0] for row in db.execute('SELECT v.id FROM versions v JOIN documents d ON d.id = v.document_id '
                                             'WHERE d.path LIKE ? ORDER BY v.seq', (f'%{name}',))]
    finally:
        db.close()


def add_line(path: Path, line: str) -> None:
    rewrite(path, (path.read_text(encoding='utf-8') + '\n' + line).encode('utf-8'))


def semantic_versions(state, query: str) -> set[int]:
    return {hit['version_id'] for hit in call(state, 'search', query=query, mode='semantic', limit=3)['result']['hits']}


def test_long_lived_server_never_returns_superseded_after_scan_and_sees_new_text_after_index(tmp_path, monkeypatch):
    home, root, folder = changed_home(tmp_path)
    state = server.make_state(home, engine=hash_engine(home))
    old_text = '첫째 합성 문서의 3번째 줄'
    [first] = versions_of(home, 'a.txt')
    assert first in semantic_versions(state, old_text)                     # the engine is warm (matrix loaded)
    assert state.engine.status()['loaded']

    new_line = '첫째 문서에 새로 들어간 합성 문장 갈매기 등대'
    add_line(folder/'a.txt', new_line)
    rescan(home, root)                                                     # scan only: a.txt seq 2 has no chunks
    for mode in search.MODES:
        hits = call(state, 'search', query=old_text, mode=mode, limit=10)['result']['hits']
        assert first not in {hit['version_id'] for hit in hits}, mode
    run_index(home)
    [_, second] = versions_of(home, 'a.txt')
    assert second in semantic_versions(state, new_line)                    # the new text, found semantically

    # mutation 1: a cache that ignores index_generation/data_generation never sees the new text
    real_vectors = search.Engine.vectors

    def stale(self, db):
        return self._vectors if self._vectors is not None else real_vectors(self, db)

    monkeypatch.setattr(search.Engine, 'vectors', stale)
    newer_line = '첫째 문서의 세 번째 판 합성 문장 해바라기 풍차'
    add_line(folder/'a.txt', newer_line)
    rescan(home, root)
    run_index(home)
    third = versions_of(home, 'a.txt')[-1]
    assert third not in semantic_versions(state, newer_line)
    monkeypatch.setattr(search.Engine, 'vectors', real_vectors)
    assert third in semantic_versions(state, newer_line)

    # mutation 2: without the "latest active version" rule a superseded version comes back after a scan
    add_line(folder/'a.txt', '첫째 문서의 네 번째 판')
    rescan(home, root)
    assert third not in {h['version_id'] for h in call(state, 'search', query=newer_line, mode='lexical')['result']['hits']}
    monkeypatch.setattr(store, 'ELIGIBLE_VERSIONS', "d.status = 'active' AND v.parse_status = 'ok' "
                                                    "AND v.unit_count > 0")
    assert third in {h['version_id'] for h in call(state, 'search', query=newer_line, mode='lexical')['result']['hits']}


# ---- review M3-2 / L5: a chunk id from an older search never names another text --------------------

@pytest.mark.parametrize('rowid_ids', [False, True])
def test_a_chunk_id_is_never_given_to_another_text(tmp_path, monkeypatch, rowid_ids):
    """The reviewer's scenario (M3-2). ``rowid_ids`` is the mutation check: with plain rowid ids (the
    old behaviour) the freed id goes to z.txt and ``get_snippet`` quietly answers with z.txt's text."""
    if rowid_ids:
        monkeypatch.setattr(store, 'take_chunk_ids', lambda db, count: db.execute(
            'SELECT COALESCE(MAX(id), 0) + 1 FROM chunks').fetchone()[0])
    home = new_home(tmp_path)
    folder = tmp_path/'src'
    folder.mkdir()
    (folder/'a.txt').write_text('알파 고유어 알파필드 일정', encoding='utf-8')
    (folder/'b.txt').write_text('베타 고유어 브라보필드 일정', encoding='utf-8')
    db = store.open_home(home)
    try:
        root = add_root(db, folder)
        scan(db, root)
    finally:
        db.close()
    run_index(home)
    state = server.make_state(home, engine=hash_engine(home))
    [hit] = [h for h in first_hits(state, '브라보필드', mode='lexical') if h['doc']['path'].endswith('b.txt')]
    (folder/'b.txt').unlink()
    rescan(home, root)
    run_index(home)                                    # b.txt's chunk (the highest id) is dropped
    (folder/'z.txt').write_text('제타 전혀 다른 새 파일 내용', encoding='utf-8')
    rescan(home, root)
    run_index(home)
    db = store.open_read(home)
    try:
        ids = [row[0] for row in db.execute('SELECT id FROM chunks ORDER BY id')]
    finally:
        db.close()
    if rowid_ids:
        reused = call(state, 'get_snippet', chunk_id=hit['chunk_id'])
        assert reused['ok'] and reused['result']['doc']['path'].endswith('z.txt')   # what the fix prevents
        return
    assert hit['chunk_id'] not in ids and max(ids) > hit['chunk_id']   # a new id, not the freed one
    gone = call(state, 'get_snippet', chunk_id=hit['chunk_id'])
    assert gone['ok'] is False and gone['error']['code'] == 'not_found'
    assert tools.CHUNK_GONE_HINT in gone['error']['message']   # review L5
    old = call(state, 'get_snippet', locator=hit['locator'])     # the hit's locator still gives its text
    assert old['ok'] and old['result']['pieces'][0]['text'] == hit['snippet']
    assert old['result']['version']['latest_version_id'] is None  # b.txt is deleted


def test_chunk_ids_keep_growing_after_the_highest_is_dropped(tmp_path):
    home = new_home(tmp_path)
    db = store.open_home(home)
    try:
        assert store.take_chunk_ids(db, 3) == 1 and store.take_chunk_ids(db, 1) == 4
        db.execute("INSERT INTO meta(key, value, updated_at) VALUES('x', '1', 'now')")  # unrelated row
        assert store.meta_get(db, store.CHUNK_ID_HIGH) == '4'
    finally:
        db.close()


# ---- M4 stage 0: recursive pieces, knowledge tool shells, summaries (design M4 §6, §7, §13.1) --------

def m3_pieces_of(envelope):
    """The M3 implementation of ``tools.pieces_of`` (commit 9d58229), kept to prove the recursive one gives
    the same pieces in the same order for every M3 response (design M4 §7.1, risk 7)."""
    if not envelope.get('ok'):
        return []
    result = envelope.get('result') or {}
    if 'hits' in result:
        return [(hit['locator'], hit['snippet']) for hit in result['hits']]
    if 'pieces' in result:
        return [(piece['locator'], piece['text']) for key in ('context_before', 'pieces', 'context_after')
                for piece in result.get(key) or ()]
    return []


def test_pieces_of_recursive_matches_m3_behaviour(built):
    roomy, tight = built.state(budget=10**7), built.state(budget=6_000)
    try:
        responses = []
        for state in (roomy, tight):   # tight: shrunk responses (dropped hits, context, cut pieces)
            for query in ('합성 프로젝트', SENTINEL, '견적', '계약 일정'):
                for args in ({}, {'mode': 'lexical', 'limit': 20}, {'collapse_by_document': True}):
                    responses.append(call(state, 'search', query=query, **args))
            hits = [hit for envelope in responses if envelope['ok'] for hit in envelope['result'].get('hits', ())][:6]
            for hit in hits:
                for units in (0, 1, 3):
                    responses.append(call(state, 'get_snippet', chunk_id=hit['chunk_id'], context_units=units))
                    responses.append(call(state, 'get_snippet', locator=hit['locator'], context_units=units,
                                          max_chars=200))
            for tool, args in (('list_documents', {}), ('recent_changes', {}), ('status', {}),
                               ('search', {'query': ''}), ('get_snippet', {'chunk_id': 99_999_999})):
                responses.append(call(state, tool, **args))
        with_context = [e for e in responses if e['ok'] and e['result'].get('context_before')]
        assert with_context and any(e['ok'] and e['result'].get('hits') for e in responses)
        for envelope in responses:
            assert tools.pieces_of(envelope) == m3_pieces_of(envelope)
            assert envelope.get('delivered_sources', []) == [locator.display(obj) for obj, _ in m3_pieces_of(envelope)]
    finally:
        roomy.write_db.close()
        tight.write_db.close()


def test_pieces_of_finds_quotes_at_any_depth_and_skips_what_is_not_text():
    (first, text), (second, other) = piece(1, '합성 인용 하나'), piece(2, '합성 인용 둘')
    envelope = tools.ok({'old': {'id': 1, 'evidence': [{'id': 5, 'locator': first, 'quote': text, 'requote': None}]},
                         'cut': None,
                         'new': {'id': 3, 'conflicts': [{'id': 2, 'value': '합성 값'}],
                                 'evidence': [{'id': 6, 'locator': second, 'quote': other,
                                               'requote': {'found': True, 'locator': first}},   # no text: not a piece
                                              {'id': 7, 'locator': first, 'quote': None, 'quote_chars': 7},  # dropped
                                              {'id': 8, 'locator': first, 'quote': text, 'text': text}]}})  # two texts
    assert tools.pieces_of(envelope) == [(first, text), (second, other)]
    assert tools.delivered(envelope) == [locator.display(first), locator.display(second)]
    assert tools.pieces_of(tools.ok({'items': [{'id': 1, 'evidence': None, 'evidence_count': 2}]})) == []


def test_knowledge_constants_match_the_design():
    assert len(tools.TOOL_NAMES) == len(set(tools.TOOL_NAMES)) == 15 and set(tools.SHRINK) == set(tools.TOOL_NAMES)
    assert set(knowledge.ERROR_CODES) <= set(tools.ERROR_CODES) and tools.NOT_IMPLEMENTED not in knowledge.ERROR_CODES
    assert tools.ASSERTION_KEYS >= {'id', 'kind', 'entity_id', 'predicate', 'object_entity_id', 'statement', 'data',
                                    'valid_from', 'valid_to', 'recorded_from', 'recorded_to', 'status',
                                    'status_as_of', 'source_kind', 'source_label', 'supersedes_id', 'recorded_by',
                                    'conflicts', 'evidence', 'evidence_count'}
    assert 'quote' in tools.EVIDENCE_KEYS and 'locator' in tools.EVIDENCE_KEYS
    assert set(knowledge.FIELDS) == set(store.ASSERTION_KINDS) and set(knowledge.DATA_KEYS) == set(store.ASSERTION_KINDS)
    assert (knowledge.SOURCES_MAX, knowledge.QUOTE_MAX, knowledge.STATEMENT_MAX, knowledge.REASON_MAX) == (5, 1000, 1000, 500)
    assert (tools.FIND_LIMIT_MAX, tools.ENTITY_LIMIT_MAX, tools.KNOWLEDGE_LIMIT_MAX, knowledge_query.HISTORY_MAX) == \
        (50, 100, 200, 50)
    assert server.KNOWLEDGE_BUSY_MS == 5000


NOTE = {'my_note': True}


@pytest.mark.parametrize('tool, args', [
    ('record_entity', {'kind': 'project', 'name': '합성 P', 'aliases': ['P']}),
    ('record_fact', {'entity_id': 1, 'predicate': '담당', 'value': '합성 값', 'valid_from': '2026-10-01', **NOTE}),
    ('record_fact', {'entity_id': 1, 'predicate': '담당', 'object_entity_id': 2,
                     'sources': [{'chunk_id': 3, 'quote': '합성 인용'}, {'locator': {'v': 1}, 'quote': '합성'}]}),
    ('record_decision', {'statement': '합성 결정', 'options': ['A', 'B'], 'chosen': 'A', 'decided_on': '2026-10-25', **NOTE}),
    ('record_task', {'statement': '합성 할 일', 'due': '2026-11-01', 'state': 'done', 'done_at': '2026-10-25', **NOTE}),
    ('supersede', {'assertion_id': 1, 'reason': '합성 교정', 'value': '새 값', 'old_valid_to': '2026-10-20', **NOTE}),
    ('supersede', {'assertion_id': 1, 'reason': '합성 완료', 'state': 'done', **NOTE}),
    ('retract', {'assertion_id': 1, 'reason': '합성 철회'}),
])
def test_writers_check_arguments_before_the_write_connection(state, tool, args):
    """Valid arguments pass the type checks and reach the write connection, which this state lacks
    (``store_missing``); the call is logged with its summary."""
    envelope = call(state, tool, **args)
    assert envelope['ok'] is False and envelope['error']['code'] == 'store_missing', envelope
    _, client, logged_tool, ok, error_code, pieces, chars, args_json = last_call(state)
    assert (client, logged_tool, ok, error_code, pieces, chars) == (CLIENT[0], tool, 0, 'store_missing', 0, 0)
    assert json.loads(args_json) == tools.summarize_args(tool, args)


@pytest.mark.parametrize('tool, args, code', [
    ('record_entity', {'kind': 'team', 'name': '합성'}, 'argument_invalid'),
    ('record_entity', {'kind': 'project', 'name': '합성', 'aliases': '합성'}, 'argument_invalid'),
    ('record_fact', {'entity_id': 0, 'predicate': '담당', **NOTE}, 'argument_invalid'),
    ('record_fact', {'entity_id': 1, 'predicate': '담당', 'valid_from': '2026-9-1', **NOTE}, 'argument_invalid'),
    ('record_fact', {'entity_id': 1, 'predicate': '담당'}, 'evidence_required'),
    ('record_fact', {'entity_id': 1, 'predicate': '담당', 'sources': []}, 'evidence_required'),
    ('record_fact', {'entity_id': 1, 'predicate': '담당', 'sources': [{'chunk_id': 1, 'quote': 'x'}], **NOTE},
     'evidence_required'),
    ('record_fact', {'entity_id': 1, 'predicate': '담당', 'sources': [{'quote': '합성'}]}, 'argument_invalid'),
    ('record_fact', {'entity_id': 1, 'predicate': '담당', 'sources': [{'chunk_id': 1, 'locator': {}, 'quote': 'x'}]},
     'argument_invalid'),
    ('record_fact', {'entity_id': 1, 'predicate': '담당', 'sources': [{'chunk_id': 1, 'quote': 'x', 'chars': [0, 1]}]},
     'argument_invalid'),
    ('record_fact', {'entity_id': 1, 'predicate': '담당', 'sources': [{'chunk_id': 1, 'quote': 'x'}] * 6},
     'argument_invalid'),
    ('record_decision', {'statement': None, **NOTE}, 'argument_invalid'),
    ('record_task', {'statement': '합성', 'state': 'later', **NOTE}, 'argument_invalid'),
    ('supersede', {'assertion_id': 1, 'reason': '합성', 'state': 'later', **NOTE}, 'argument_invalid'),
    ('supersede', {'assertion_id': 1, 'reason': '합성'}, 'evidence_required'),
    ('retract', {'assertion_id': 1, 'reason': None}, 'argument_invalid'),
    ('find_entity', {'query': '  '}, 'argument_invalid'),
    ('find_entity', {'query': '합성', 'limit': 51}, 'argument_invalid'),
    ('get_entity', {}, 'argument_invalid'),
    ('get_entity', {'entity_id': 1, 'name': '합성'}, 'argument_invalid'),
    ('get_entity', {'name': '합성'}, 'argument_invalid'),
    ('get_entity', {'entity_id': 1, 'as_of_recorded': '어제'}, 'argument_invalid'),
    ('get_entity', {'entity_id': 1, 'limit': 101}, 'argument_invalid'),
    ('list_knowledge', {'status': 'open-ish'}, 'argument_invalid'),
    ('list_knowledge', {'as_of_valid': '2026-10-25T00:00:00'}, 'argument_invalid'),
    ('list_knowledge', {'offset': -1}, 'argument_invalid'),
    ('get_history', {'assertion_id': -1}, 'argument_invalid'),
])
def test_knowledge_tool_argument_errors_are_envelopes_without_text(kstate, tool, args, code):
    envelope = call(kstate, tool, **args)
    assert envelope['ok'] is False and envelope['error']['code'] == code, envelope
    assert '합성' not in envelope['error']['message']   # names and ranges, never what the client sent


def test_knowledge_summaries_keep_no_text(state):
    text = f'{SENTINEL} 합성 본문'
    source = {'locator': {'v': 1, 'doc': 1, 'path': f'C:\\{SENTINEL}.txt'}, 'quote': text}
    calls = {
        'record_entity': {'kind': 'person', 'name': text, 'aliases': [text, text], 'note': text},
        'record_fact': {'entity_id': 4, 'predicate': text, 'value': text, 'unit': text, 'statement': text,
                        'valid_from': '2026-10-01', 'sources': [source, {'chunk_id': 9, 'quote': text}]},
        'record_decision': {'statement': text, 'options': [text], 'chosen': text, 'rationale': text, **NOTE},
        'record_task': {'statement': text, 'state': 'open', 'due': '2026-11-01', **NOTE},
        'supersede': {'assertion_id': 7, 'reason': text, 'value': text, 'predicate': text, 'old_valid_to': '2026-10-20',
                      'sources': [source]},
        'retract': {'assertion_id': 7, 'reason': text},
        'find_entity': {'query': text, 'kind': 'person', 'limit': 5},
        'get_entity': {'kind': 'person', 'name': text, 'as_of_valid': '2026-10-01'},
        'list_knowledge': {'predicate': text, 'entity_id': 4, 'as_of_valid': '2026-10-01', 'kind': 'fact'},
        'get_history': {'assertion_id': 7},
    }
    summaries = {tool: tools.summarize_args(tool, args) for tool, args in calls.items()}
    assert summaries['record_fact'] == {
        'kind': 'fact', 'entity': 4, 'object': None, 'predicate_chars': len(text), 'statement_chars': len(text),
        'value_chars': len(text), 'sources': 2, 'by': ['chunk_id', 'locator'], 'my_note': False,
        'quote_chars': 2 * len(text), 'valid': [True, False]}
    assert summaries['supersede'] == {'assertion': 7, 'fields': ['predicate', 'value'], 'old_valid_to': True,
                                      'sources': 1, 'by': ['locator'], 'my_note': False, 'quote_chars': len(text),
                                      'reason_chars': len(text)}
    assert summaries['record_entity'] == {'kind': 'person', 'name_chars': len(text), 'aliases': 2, 'note': True}
    assert summaries['list_knowledge']['filters'] == ['as_of_valid', 'entity_id', 'predicate']
    assert summaries['record_task']['state'] == 'open' and summaries['record_decision']['valid'] == [False, False]
    assert summaries['get_entity'] == {'by': 'name', 'entity': None, 'as_of': [True, False], 'include_closed': False,
                                       'limit': None}
    assert SENTINEL not in json.dumps(summaries, ensure_ascii=False)
    for tool, args in calls.items():           # through handle: stage 0 bodies, nothing with text logged
        call(state, tool, **args)
    for row in state.write_db.execute('SELECT * FROM tool_calls'):
        assert not any(SENTINEL in str(value) for value in row), row


def test_knowledge_envelope_moves_notice_and_delivers_the_quotes():
    obj, text = piece(1, '합성 인용')
    envelope = tools.knowledge_envelope({'assertion': {'id': 1, 'evidence': [{'id': 2, 'locator': obj, 'quote': text}]},
                                         'conflicts': [], 'duplicates': [], 'notice': ['합성 안내']})
    assert envelope == {'ok': True, 'result': {'assertion': {'id': 1, 'evidence': [{'id': 2, 'locator': obj,
                                                                                    'quote': text}]},
                                               'conflicts': [], 'duplicates': []},
                        'delivered_sources': [locator.display(obj)], 'notice': ['합성 안내']}


# ---- M4 stage 1: the writers through server.handle (design M4 §6.2, §6.5, §7) ------------------------

@pytest.fixture
def kstate(tmp_path):
    """A small scanned and indexed home (``test_knowledge.KB``) with both write connections."""
    from test_knowledge import KB
    kb = KB(tmp_path)
    write_db = store.open_home(kb.home)
    value = server.make_state(kb.home, engine=hash_engine(kb.home), write_db=write_db, know_db=kb.db)
    value.kb = kb
    yield value
    write_db.close()
    kb.db.close()


def quotes_of(node) -> list[tuple[dict, str]]:
    """Evidence quotes in a response, read from its structure (independently of ``tools.pieces_of``)."""
    return [(item['locator'], item['quote']) for item in tools.quote_items(node)]


def test_write_tools_return_envelopes_with_assertion_keys(kstate):
    kb = kstate.kb
    entity = call(kstate, 'record_entity', kind='project', name='합성 과제 31', aliases=['과제31'])
    assert entity['ok'] and set(entity['result']) == tools.RECORD_ENTITY_RESULT_KEYS
    assert set(entity['result']['entity']) == tools.ENTITY_KEYS and entity['delivered_sources'] == []
    assert entity['result']['entity']['created_by'] == f'{CLIENT[0]}/record_entity'
    entity_id = entity['result']['entity']['id']
    quote = '계약 상태는 검토 중'
    fact = call(kstate, 'record_fact', entity_id=entity_id, predicate='계약 상태', value='검토 중',
                sources=[{'chunk_id': kb.chunk('notes.txt', 1), 'quote': quote}], valid_from='2026-10-01')
    decision = call(kstate, 'record_decision', statement='합성 결정', options=['A', 'B'], chosen='A', my_note=True)
    task = call(kstate, 'record_task', statement='합성 할 일', due='2026-11-01', my_note=True)
    for envelope in (fact, decision, task):
        assert envelope['ok'], envelope
        assert set(envelope['result']) == tools.RECORD_RESULT_KEYS
        assert set(envelope['result']['assertion']) == tools.ASSERTION_KEYS
    [evidence] = fact['result']['assertion']['evidence']
    assert set(evidence) == tools.EVIDENCE_KEYS and evidence['quote'] == quote
    assert fact['delivered_sources'] == [locator.display(evidence['locator'])]
    assert fact['result']['assertion']['recorded_by'] == f'{CLIENT[0]}/record_fact'
    assert decision['delivered_sources'] == [] and task['result']['assertion']['data']['state'] == 'open'
    new = call(kstate, 'supersede', assertion_id=fact['result']['assertion']['id'], reason='합성 갱신', value='승인',
               old_valid_to='2026-10-20', sources=[{'locator': kb.loc('notes.txt', 1), 'quote': '합성 과제 31'}])
    assert new['ok'] and set(new['result']) == tools.SUPERSEDE_RESULT_KEYS
    assert all(set(new['result'][key]) == tools.ASSERTION_KEYS for key in ('old', 'cut', 'new'))
    assert new['delivered_sources'] == [locator.display(obj) for obj, _ in quotes_of(new['result'])]
    assert len(new['delivered_sources']) == 2                      # the cut's copied source and the new one
    closed = call(kstate, 'retract', assertion_id=task['result']['assertion']['id'], reason='합성 철회')
    assert closed['ok'] and set(closed['result']) == tools.RETRACT_RESULT_KEYS and closed['delivered_sources'] == []
    events = kb.db.execute('SELECT kind, recorded_by, call_id FROM assertion_events ORDER BY id').fetchall()
    logged = dict(kstate.write_db.execute('SELECT call_id, tool FROM tool_calls').fetchall())
    assert all(logged[call_id] == by.split('/')[1] and by.startswith(CLIENT[0] + '/') for _, by, call_id in events)


def test_write_errors_are_envelopes_and_leave_nothing(kstate):
    kb = kstate.kb
    entity = call(kstate, 'record_entity', kind='project', name='합성 과제')['result']['entity']['id']
    before = {t: kb.count(t) for t in ('assertions', 'evidence', 'assertion_events', 'links')}
    for args, code in (({'sources': [{'chunk_id': kb.chunk('notes.txt', 1), 'quote': '없는 문장'}]},
                        'evidence_quote_not_found'),
                       ({'sources': [{'chunk_id': 999_999, 'quote': '합성 과제'}]}, 'chunk_not_found'),
                       ({}, 'evidence_required'),
                       ({'entity_id': 999, 'my_note': True}, 'entity_not_found')):
        envelope = call(kstate, 'record_fact', **{'entity_id': entity, 'predicate': '담당', 'value': '합성 값', **args})
        assert envelope['ok'] is False and envelope['error']['code'] == code, envelope
        assert last_call(kstate)[3:6] == (0, code, 0)
    assert call(kstate, 'retract', assertion_id=999, reason='합성')['error']['code'] == 'assertion_not_found'
    assert {t: kb.count(t) for t in before} == before


def test_write_response_drops_quotes_last(kstate):
    kb = kstate.kb
    sources = [{'locator': kb.loc('notes.txt', n), 'quote': text} for n, text in
               ((1, '합성 과제 31의 계약'), (3, '여러 개인 합성'), (5, '표지 문장은'), (6, '걸친 인용의'), (7, '여기 있습니다'))]
    roomy = call(kstate, 'record_decision', statement='합성 결정', sources=sources)
    size = len(tools.dumps(roomy))
    kstate.budget = size - 1                                     # one quote has to go
    tight = call(kstate, 'record_decision', statement='합성 결정 2', sources=sources)
    assert tight['ok'], tight
    evidence = tight['result']['assertion']['evidence']
    assert [e['quote'] for e in evidence[:-1]] == [s['quote'] for s in sources[:-1]] and evidence[-1]['quote'] is None
    assert evidence[-1]['quote_chars'] == len(sources[-1]['quote'])
    assert tight['delivered_sources'] == [locator.display(e['locator']) for e in evidence[:-1]]
    assert tools.SHRINK_QUOTES_NOTICE.format(1) in tight['notice']
    assert last_call(kstate)[5] == len(sources) - 1                # pieces logged: the quotes that left
    kstate.budget = 600
    assert call(kstate, 'record_decision', statement='합성 결정 3', sources=sources)['error']['code'] == \
        'response_too_large'


def entity_envelope(rows: int, quote: str = '합성 인용 문장') -> dict:
    obj, _ = piece(1, quote)
    item = lambda n: {'id': n, 'evidence': [{'id': n, 'locator': obj, 'quote': quote, 'quote_chars': len(quote)}]}  # noqa: E731
    return tools.ok({'entity': {'id': 1}, **{key: [item(n) for n in range(rows)] for key in knowledge_query.ENTITY_LISTS},
                     'predicates_in_use': [], 'has_more': {key: False for key in knowledge_query.ENTITY_LISTS},
                     'as_of': {'valid': None, 'recorded': None}})


def test_get_entity_and_history_shrink_quotes_then_rows():
    envelope = entity_envelope(2)
    for _ in range(8):                                             # every quote first, the last one first
        assert tools.shrink_entity(envelope)
    assert all(row['evidence'][0]['quote'] is None for key in knowledge_query.ENTITY_LISTS
               for row in envelope['result'][key])
    assert tools.SHRINK_QUOTES_NOTICE.format(8) in envelope['notice']
    for expected in ('tasks', 'tasks', 'decisions', 'decisions', 'related', 'related', 'facts', 'facts'):
        assert tools.shrink_entity(envelope)
        assert envelope['result']['has_more'][expected] is True
    assert tools.SHRINK_ENTITY_NOTICE.format(8) in envelope['notice']
    assert not tools.shrink_entity(envelope)
    fitted = tools.fit(entity_envelope(5), tools.shrink_entity, len(tools.dumps(entity_envelope(5))) // 2)
    assert fitted['ok'] and fitted['delivered_sources'] == [locator.display(o) for o, _ in quotes_of(fitted['result'])]
    obj, _ = piece(1, '합성 이력')
    chain = [{'id': n, 'evidence': [{'id': n, 'locator': obj, 'quote': f'합성 이력 {n}', 'quote_chars': 7}],
              'events': [], 'links': []} for n in range(3)]
    history = tools.ok({'chain': chain, 'truncated': False})
    for _ in range(3):                                             # quotes from the root on
        assert tools.shrink_history(history)
    assert [row['evidence'][0]['quote'] for row in history['result']['chain']] == [None, None, None]
    assert tools.shrink_history(history) and history['result']['truncated'] is True
    assert [row['id'] for row in history['result']['chain']] == [1, 2]
    assert tools.SHRINK_QUOTES_NOTICE.format(3) in history['notice']
    assert tools.SHRINK_HISTORY_NOTICE.format(1) in history['notice']


def test_read_tools_return_entity_and_assertion_keys_and_deliver_only_quotes(kstate):
    kb = kstate.kb
    entity = call(kstate, 'record_entity', kind='project', name='합성 과제 31', aliases=['과제31'])['result']['entity']
    source = [{'locator': kb.loc('notes.txt', 1), 'quote': '계약 상태는 검토 중'}]
    fact = call(kstate, 'record_fact', entity_id=entity['id'], predicate='계약 상태', value='검토 중', sources=source)
    call(kstate, 'record_fact', entity_id=entity['id'], predicate='계약 상태', value='승인', my_note=True)  # conflict
    call(kstate, 'record_task', statement='합성 할 일', entity_id=entity['id'], my_note=True)
    found = call(kstate, 'find_entity', query='과제31')
    assert found['ok'] and set(found['result']) == tools.FIND_RESULT_KEYS and found['delivered_sources'] == []
    assert [e['id'] for e in found['result']['entities']] == [entity['id']]
    assert all(set(e) == tools.ENTITY_KEYS for e in found['result']['entities'])
    detail = call(kstate, 'get_entity', entity_id=entity['id'])
    by_name = call(kstate, 'get_entity', kind='project', name=' 합성 과제 31 ')
    for envelope in (detail, by_name):
        assert envelope['ok'] and set(envelope['result']) == tools.ENTITY_RESULT_KEYS, envelope
        assert envelope['result']['entity']['id'] == entity['id']
        assert all(set(row) == tools.ASSERTION_KEYS for key in knowledge_query.ENTITY_LISTS
                   for row in envelope['result'][key])
        assert envelope['delivered_sources'] == [locator.display(o) for o, _ in quotes_of(envelope['result'])]
        assert len(envelope['delivered_sources']) == 1
    assert last_call(kstate)[5] == 1                                 # one piece logged
    listed = call(kstate, 'list_knowledge', entity_id=entity['id'], kind='fact')
    assert listed['ok'] and set(listed['result']) == tools.KNOWLEDGE_LIST_RESULT_KEYS
    assert listed['delivered_sources'] == [] and len(listed['result']['items']) == 2
    assert all(row['evidence'] is None and set(row) == tools.ASSERTION_KEYS for row in listed['result']['items'])
    history = call(kstate, 'get_history', assertion_id=fact['result']['assertion']['id'])
    assert history['ok'] and set(history['result']) == tools.HISTORY_RESULT_KEYS
    assert history['delivered_sources'] == [locator.display(o) for o, _ in quotes_of(history['result'])] != []
    assert call(kstate, 'get_entity', kind='project', name='없는 합성')['error']['code'] == 'entity_not_found'
    assert call(kstate, 'get_history', assertion_id=999)['error']['code'] == 'assertion_not_found'
    kstate.budget = len(tools.dumps(detail)) - 1                    # the quote goes before any row
    tight = call(kstate, 'get_entity', entity_id=entity['id'])
    assert tight['ok'] and tight['delivered_sources'] == [] and quotes_of(tight['result']) == []
    assert [row['id'] for row in tight['result']['facts']] == [row['id'] for row in detail['result']['facts']]


def test_a_cut_supersede_answers_the_old_value_before_the_cut_and_the_new_one_after(kstate, monkeypatch):
    """Coordinator decision 2026-10-25 (design M4 §4.3 record): the cut row C stays open in the record, so
    current knowledge gives C before old_valid_to and N from it; before the supersede only the old row."""
    clock = ['2026-10-25T10:00:00+09:00']
    monkeypatch.setattr(store, 'now', lambda: clock[0])
    kb = kstate.kb
    entity = call(kstate, 'record_entity', kind='project', name='합성 과제 31')['result']['entity']['id']
    clock[0] = '2026-10-25T10:01:00+09:00'
    old = call(kstate, 'record_fact', entity_id=entity, predicate='계약 상태', value='검토 중', valid_from='2026-10-01',
               sources=[{'locator': kb.loc('notes.txt', 1), 'quote': '검토 중'}])['result']['assertion']
    clock[0] = '2026-10-25T10:05:00+09:00'
    out = call(kstate, 'supersede', assertion_id=old['id'], reason='합성 갱신', value='승인', old_valid_to='2026-10-20',
               my_note=True)['result']
    cut, new = out['cut'], out['new']
    assert (cut['recorded_to'], new['recorded_to'], new['supersedes_id'], cut['supersedes_id']) == \
        (None, None, cut['id'], old['id'])

    def ids(**args):
        envelope = call(kstate, 'list_knowledge', entity_id=entity, **args)
        assert envelope['ok'], envelope
        return [(row['id'], row['data']['value']) for row in envelope['result']['items']]
    assert ids(as_of_valid='2026-10-10') == [(cut['id'], '검토 중')]
    assert ids(as_of_valid='2026-10-20') == ids(as_of_valid='2026-10-25') == [(new['id'], '승인')]
    assert sorted(ids()) == [(cut['id'], '검토 중'), (new['id'], '승인')]
    assert ids(as_of_recorded='2026-10-25T10:03:00+09:00') == [(old['id'], '검토 중')]
    chain = call(kstate, 'get_history', assertion_id=new['id'])['result']['chain']
    assert [row['id'] for row in chain] == [old['id'], cut['id'], new['id']]


def test_no_knowledge_text_in_tool_calls_deliveries_or_stderr(kstate, capsys):
    kb = kstate.kb
    text = f'{SENTINEL} 합성 본문'
    entity = call(kstate, 'record_entity', kind='person', name=text, aliases=[text], note=text)
    entity_id = entity['result']['entity']['id']
    fact = call(kstate, 'record_fact', entity_id=entity_id, predicate=text, value=text, statement=text,
                sources=[{'locator': kb.loc('notes.txt', 5), 'quote': SENTINEL}])
    assert fact['ok'], fact
    call(kstate, 'supersede', assertion_id=fact['result']['assertion']['id'], reason=text, value=text + ' 2',
         my_note=True)
    call(kstate, 'record_decision', statement=text, rationale=text, my_note=True)
    call(kstate, 'record_fact', entity_id=entity_id, predicate=text, value=text,
         sources=[{'locator': kb.loc('notes.txt', 1), 'quote': text}])       # not found: no text in the error
    for row in kstate.write_db.execute('SELECT * FROM tool_calls'):
        assert not any(SENTINEL in str(value) for value in row), row
    deliveries = kstate.write_db.execute('SELECT locator_json, chars FROM deliveries').fetchall()
    assert len(deliveries) == 1 and json.loads(deliveries[0][0])['unit'] == {'kind': 'line', 'line': 5}
    assert kb.db.execute('SELECT COUNT(*) FROM assertions WHERE data_json LIKE ?', (f'%{SENTINEL}%',)).fetchone()[0] == 3  # the knowledge store holds it
    captured = capsys.readouterr()
    assert SENTINEL not in captured.err and SENTINEL not in captured.out


# ---- review M4 v1: L3 (emptied by the budget), L4 (offsets), L8 (answer failed after the commit) ----------

def test_a_result_emptied_by_the_budget_says_so_instead_of_paging():
    obj, text = piece(1, '합성 인용 ' * 40)
    rows = tools.ok({'items': [{'id': 1, 'statement': text}], 'total': 1, 'limit': 1, 'offset': 0, 'has_more': False})
    budget = len(tools.dumps(tools.ok({'items': [], 'total': 1, 'limit': 0, 'offset': 0, 'has_more': True}))) + 250
    fitted = tools.fit(rows, tools.SHRINK['list_knowledge'], budget)
    assert fitted['ok'] and fitted['result']['items'] == [] and fitted['notice'] == [tools.EMPTIED_NOTICE]
    history = tools.ok({'chain': [{'id': 1, 'statement': text, 'evidence': []}], 'truncated': False, 'branches': []})
    fitted = tools.fit(history, tools.shrink_history, budget)
    assert fitted['result']['chain'] == [] and fitted['notice'] == [tools.EMPTIED_NOTICE]
    detail = entity_envelope(1)
    for _ in range(4 + 4):                                         # four quotes, then four rows
        tools.shrink_entity(detail)
    assert tools.EMPTIED_NOTICE in detail['notice']


@pytest.mark.parametrize('value, ok', [('2026-10-25T06:35:00+05:30', True), ('2026-10-25T06:35:00+05:30:15', False),
                                       ('2026-10-25T06:35:00-03:30', True), ('2026-10-25T06:35:00-04:00:30', False)])
def test_offsets_with_seconds_are_refused(kstate, value, ok):
    for tool, args in (('list_knowledge', {'as_of_recorded': value}), ('list_knowledge', {'recorded_since': value}),
                       ('search', {'query': '합성 과제', 'since': value})):
        envelope = call(kstate, tool, **args)
        assert envelope['ok'] is ok, (tool, envelope)
        if not ok:
            assert envelope['error']['code'] in ('argument_invalid', 'filter_invalid')
    if not ok:
        with pytest.raises(knowledge.KnowledgeError):
            knowledge_query.recorded_instant(value)


def test_a_write_whose_answer_fails_after_the_commit_names_its_ids(kstate, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError('합성 고장')
    entity = call(kstate, 'record_entity', kind='project', name='합성 과제')['result']['entity']['id']
    monkeypatch.setattr(knowledge_query, 'assertion_row', broken)
    envelope = call(kstate, 'record_fact', entity_id=entity, predicate='p', value='v', my_note=True)
    stored = kstate.kb.db.execute('SELECT MAX(id) FROM assertions').fetchone()[0]
    assert envelope['ok'] is False and envelope['error']['code'] == 'internal_error'
    assert f'stored (id {stored})' in envelope['error']['message'] and envelope['error']['retryable'] is False
    assert last_call(kstate)[3:5] == (0, 'internal_error')
    assert any(record.exc_info for record in caplog.records)    # the traceback goes to stderr

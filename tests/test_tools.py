"""Tool functions (design M3 §2-§6): envelope helpers, delivered pieces, the size budget, the call-log
summary, the five tools through ``server.handle`` (the path ``serve`` takes, without stdio), binding
re-derivation of every delivered piece (§5, D-1..D-5), the size budget per tool (S-1..S-3) and the
long-lived engine across scans and index runs (E-2, with mutation checks). Synthetic text only.

``get_snippet`` calls ``snippet.by_chunk``/``by_locator`` (stage 1, B): the tests that need them fail
with ``NotImplementedError`` until B's implementation is in."""
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
from workmcp import binding, collect, config, embedder, index, locator, parsers, paths, search, server, store, tools

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
    return tools.ok({'doc': {}, 'version': {}, 'chunk': None, 'pieces': [as_piece(main)],
                     'context_before': [as_piece(before)], 'context_after': [as_piece(after)]}), (before, main, after)


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
    assert set(result) == tools.STATUS_KEYS and result['schema_version'] == store.SCHEMA_VERSION == 2
    assert set(result['engine']) >= {'loaded', 'rows', 'load_failure'} | tools.ENGINE_STATUS_EXTRA_KEYS
    assert result['engine']['loaded'] is False and result['engine']['idle_seconds'] is None
    assert (result['engine']['model_idle_seconds'], result['engine']['unloads']) == (60, 2)
    engine.embedder()
    warm = tools.status(home, engine)['result']['engine']
    assert (warm['loaded'], warm['idle_seconds'], warm['loads']) == (True, 0.0, 1)
    assert set(result['home_virtualization']) == set(paths.VIRTUALIZATION_KEYS)
    assert result['calls'] == {'total': 0, 'by_client': {}}
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


def call(state, tool: str, **args) -> dict:
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
        for hit in result['hits']:
            assert search.HIT_KEYS <= set(hit) <= search.HIT_KEYS | search.HIT_OPTIONAL_KEYS
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
    assert tools.OLD_VERSION_NOTICE.format(version['seq'], version['latest_version_id']) in old['notice']
    assert old['result']['pieces'][0]['text'] == hit['snippet']
    (folder/'a.txt').unlink()
    rescan(home, root)
    gone = call(state, 'get_snippet', chunk_id=hit['chunk_id'])
    assert gone['ok'] and gone['result']['version']['latest'] is False
    assert gone['result']['version']['latest_version_id'] is None
    assert tools.DELETED_NOTICE.format(gone['result']['version']['seq']) in gone['notice']


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

"""Tool functions without the SDK (design M3 §2-§4): envelope helpers, delivered pieces, the size
budget, the call-log summary and ``status``. Stage 0: the four other tools answer not_implemented.
Synthetic text only."""
import json

import pytest

from workmcp import config, embedder, locator, paths, search, store, tools

H = '0' * 64


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


@pytest.mark.parametrize('call', [
    lambda home: tools.search(home, None, 'x'),
    lambda home: tools.get_snippet(home, chunk_id=1),
    lambda home: tools.list_documents(home),
    lambda home: tools.recent_changes(home),
])
def test_stage0_tools_answer_not_implemented(home, call):
    assert call(home)['error']['code'] == tools.NOT_IMPLEMENTED


def test_virtualization_probe_outside_appdata_needs_no_probe(tmp_path):
    env = {'LOCALAPPDATA': str(tmp_path/'AppData'/'Local'), 'APPDATA': str(tmp_path/'AppData'/'Roaming'),
           'USERPROFILE': str(tmp_path)}
    outside = paths.virtualization_probe(tmp_path/'Motive_work', env)
    assert outside == {'checked': True, 'under_appdata': False, 'redirected_to': None, 'package': None}
    inside = paths.virtualization_probe(tmp_path/'AppData'/'Local'/'Motive_work', env)
    assert inside['under_appdata'] is True and inside['checked'] is False  # stage 1 probes for a redirect
    package = tmp_path/'AppData'/'Local'/'Packages'/'Fake_pkg'/'LocalCache'/'Local'/'Motive_work'
    assert 'Fake_pkg' in paths.appdata_warning(package, env)
    assert paths.appdata_warning(tmp_path/'Motive_work', env) is None

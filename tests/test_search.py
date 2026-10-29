"""Search (design M2 §5, §6.2 S1-S5, §10): the M3 result contract, exact sources that re-derive from
the current files (completion condition ④), modes, filters, limits, hostile input, the model-less
path (condition ③, part) and the vector-memory paths. Semantic search runs on ``HashEmbedder``
(test-only, not a model); real-model numbers are the stage-2 measurement.
"""
import ast
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from fixtures import SENTINEL
from fixtures import bundle as synthetic_bundle
from fixtures import corpus as corpus_module
from test_collect import add_root, rewrite, scan
from workmcp import binding, chunks, collect, index, locator, parsers, paths, search, store
from workmcp.config import SearchSettings
from workmcp.embedder import HashEmbedder

SEARCH_PY = Path(search.__file__)
DIMS = 256


def hash_factory(dims=DIMS):
    return lambda: HashEmbedder(dims)


def build_home(home: Path, write) -> tuple[Path, Path]:
    """A data home with ``write(root)`` scanned and indexed (HashEmbedder); returns (home, root)."""
    paths.ensure_home(home)
    root = home.parent/'src'
    write(root)
    db = store.open_home(home)
    try:
        scan(db, add_root(db, root))
        result = index.run_index(db, home, embedder=HashEmbedder(DIMS))
        assert result['chunks_total'] > 0
    finally:
        db.close()
    return home, root


@pytest.fixture(scope='module')
def corpus():
    return corpus_module.build()


@pytest.fixture(scope='module')
def indexed(tmp_path_factory, corpus):
    """The evaluation corpus, scanned and indexed once for the module."""
    return build_home(tmp_path_factory.mktemp('corpus')/'home', lambda root: corpus_module.write(corpus, root))


@pytest.fixture(scope='module')
def bundle_home(tmp_path_factory):
    """The M1 bundle (nine parsers, edge cases, a 20,000-character line split into pieces)."""
    return build_home(tmp_path_factory.mktemp('bundle')/'home', synthetic_bundle.write_bundle)


@pytest.fixture
def engine(indexed):
    return search.Engine(indexed[0], embedder_factory=hash_factory())


def ro(home):
    return store.open_read(home)


# ---- contract (design M2 §5.7) ---------------------------------------------------------------------

def check_contract(result: dict) -> None:
    assert set(result) == search.RESULT_KEYS
    meta = result['meta']
    assert set(meta) == search.META_KEYS
    assert meta['mode'] in search.MODES and meta['search_version'] == search.SEARCH_VERSION
    assert set(meta['lexical']) == search.LEXICAL_META_KEYS and meta['lexical']['engine'] in ('fts5', 'like', 'none')
    assert set(meta['lexical']['terms']) == {'match', 'short'}
    assert meta['semantic'] in (search.UNAVAILABLE, search.NOT_REQUESTED) or set(meta['semantic']) == search.SEMANTIC_META_KEYS
    assert set(meta['filters']) == search.FILTER_KEYS and set(meta['elapsed_ms']) == search.ELAPSED_KEYS
    assert isinstance(meta['eligible_chunks'], int) and isinstance(result['notice'], list)
    assert isinstance(meta['collapsed'], int) and meta['collapsed'] >= 0
    signals = meta['signals']
    assert set(signals) == search.SIGNAL_KEYS and isinstance(signals['low'], bool)
    lexical_ran = meta['mode'] != 'semantic'
    for key in ('lexical_candidates', 'exact_phrase_hits'):
        assert (signals[key] is not None) == lexical_ran
    assert signals['lexical_candidates'] in (None, meta['lexical']['candidates'])
    assert signals['exact_phrase_hits'] is None or signals['exact_phrase_hits'] <= signals['lexical_candidates']
    if isinstance(meta['semantic'], dict) and meta['semantic']['candidates']:
        cosines = [h['scores']['cosine'] for h in result['hits'] if h['scores']['cosine'] is not None]
        assert signals['cosine_top1'] is not None
        assert not cosines or signals['cosine_top1'] >= max(cosines) - 1e-4
        assert (signals['cosine_top10'] is not None) == (meta['semantic']['candidates'] >= 10)
    else:
        assert signals['cosine_top1'] is None and signals['cosine_top10'] is None
    if signals['cosine_gap'] is not None:
        assert signals['cosine_gap'] == pytest.approx(signals['cosine_top1'] - signals['cosine_top10'], abs=2e-4)
    assert signals['low'] == search.low_signal(signals)
    for rank, hit in enumerate(result['hits'], 1):
        assert search.HIT_KEYS <= set(hit) <= search.HIT_KEYS | search.HIT_OPTIONAL_KEYS
        assert hit['rank'] == rank and isinstance(hit['chunk_id'], int) and isinstance(hit['version_id'], int)
        assert set(hit['doc']) == search.DOC_KEYS
        assert set(hit['scores']) == search.SCORE_KEYS and hit['scores']['score_is_probability'] is False
        assert hit['scores']['hit'] in search.HIT_REASONS
        assert isinstance(hit['snippet'], str) and 0 < len(hit['snippet']) <= chunks.SNIPPET_MAX
        locator.check_shape(hit['locator'])
        assert 1 <= len(hit['units']) <= search.UNITS_CAP
        for item in hit['units']:
            locator.check_shape(item)
            assert (item['doc'], item['path'], item['sha256']) == (hit['doc']['id'], hit['doc']['path'],
                                                                   hit['doc']['sha256'])
    json.dumps(result, ensure_ascii=False)  # the M3 tool returns it as JSON


def test_result_contract_in_every_mode(engine, corpus):
    for mode in search.MODES:
        for query in corpus.queries[::12]:
            result = engine.search(query.text, mode=mode)
            check_contract(result)
            assert result['meta']['mode'] == mode
    lexical = engine.search('사양서 개정판', mode='lexical')
    assert lexical['meta']['semantic'] == search.NOT_REQUESTED and lexical['meta']['lexical']['engine'] == 'fts5'
    hybrid = engine.search('사양서 개정판')
    assert hybrid['meta']['semantic']['model'] == 'hash-ngram-test-only'
    assert hybrid['meta']['semantic']['real_model'] is False and hybrid['meta']['semantic']['streamed'] is False


def rederive(db, hit, parsed_cache):
    """Completion condition ④ for one hit (design M2 §5.8)."""
    # (a) the snippet locator checks against the store and quotes exactly the snippet
    assert binding.validate_locator(db, hit['locator'])['text'] == hit['snippet']
    # (b) every units[] locator checks too
    for item in hit['units']:
        binding.validate_locator(db, item)
    # (c) the current file parsed again gives the same unit text, and chars cut the snippet
    path = hit['doc']['path']
    if path not in parsed_cache:
        data, sha, _ = collect.read_source(path)
        assert sha == hit['doc']['sha256']
        result = parsers.parse(data, hit['doc']['ext'], parsers.Limits())
        parsed_cache[path] = {parsers.unit_json(u.unit): u.text for u in result.units}
    units = parsed_cache[path]
    start, end = hit['locator']['chars']
    assert units[parsers.unit_json(hit['locator']['unit'])][start:end] == hit['snippet']
    for item in hit['units']:
        s, e = item['chars']
        assert locator.text_sha256(units[parsers.unit_json(item['unit'])][s:e]) == item['text_sha256']
    # (d) the chunk text is the slice of its joined units
    first, last, cs, ce, text = db.execute('SELECT unit_first, unit_last, char_start, char_end, text FROM chunks '
                                           'WHERE id=?', (hit['chunk_id'],)).fetchone()
    joined = '\n'.join(t for (t,) in db.execute('SELECT text FROM units WHERE version_id=? AND ordinal BETWEEN ? '
                                                'AND ? ORDER BY ordinal', (hit['version_id'], first, last)))
    assert joined[cs:ce] == text


BUNDLE_QUERIES = ('회의록', '가상 거래처 A', '견적 1,234,000원', 'quote Q-0042', '일정표 초안', SENTINEL,
                  '글상자 안 문장', '그룹 밖 도형', '두 번째 줄', '가가가가가가')


def test_every_hit_rederives_from_the_current_file(bundle_home):
    home, _ = bundle_home
    engine = search.Engine(home, embedder_factory=hash_factory())
    db = ro(home)
    parsed, seen, split_pieces = {}, 0, 0
    try:
        for query in BUNDLE_QUERIES:
            for mode in search.MODES:
                result = engine.search(query, mode=mode, limit=20)
                check_contract(result)
                assert result['hits'], (query, mode)
                for hit in result['hits']:
                    rederive(db, hit, parsed)
                    seen += 1
                    split_pieces += hit['units'][0]['chars'][0] > 0 or 'long_line' in hit['doc']['path']
        report = binding.verify(db)
        chunk_count = db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
    finally:
        db.close()
    assert seen >= 300 and split_pieces > 0
    assert report['ok'], report
    assert report['chunks_checked'] == chunk_count


def test_hit_has_locator_and_units_with_exact_chars(engine, indexed):
    db = ro(indexed[0])
    try:
        result = engine.search('PRJ-012 사양서 개정판', mode='lexical', limit=5)
        hit = result['hits'][0]
        assert hit['doc']['path'].endswith('topic12.' + hit['doc']['ext'].lstrip('.'))
        assert hit['snippet'].startswith('PRJ-012 사양서 개정판')
        snippet_unit = hit['locator']['unit']
        assert any(item['unit'] == snippet_unit for item in hit['units'])  # the snippet is inside a covered unit
        for item in hit['units']:
            text = binding.validate_locator(db, item)['text']
            assert len(text) == item['chars'][1] - item['chars'][0]
    finally:
        db.close()


def test_snippet_locator_display_has_path_hash_unit_chars(engine):
    hit = engine.search('PRJ-012 사양서 개정판', mode='lexical', limit=1)['hits'][0]
    line = locator.display(hit['locator'])
    start, end = hit['locator']['chars']
    assert hit['doc']['path'] in line and hit['doc']['sha256'][:12] in line
    assert locator.unit_label(hit['locator']['unit']) in line and f'chars {start}-{end}' in line


def test_units_list_is_capped(tmp_path):
    lines = [f'{SENTINEL} 짧은 줄 모음'] + [f'항목{n:03d}' for n in range(1, 200)]

    def write(root):
        root.mkdir(parents=True)
        (root/'many.txt').write_text('\n'.join(lines), encoding='utf-8')

    home, _ = build_home(tmp_path/'home', write)
    result = search.search(ro(home), '항목005', mode='lexical')
    hit = result['hits'][0]
    assert len(hit['units']) == search.UNITS_CAP and hit['units_truncated'] > 0
    db = ro(home)
    first, last = db.execute('SELECT unit_first, unit_last FROM chunks WHERE id=?', (hit['chunk_id'],)).fetchone()
    db.close()
    assert hit['units_truncated'] == last - first + 1 - search.UNITS_CAP
    check_contract(result)


# ---- query transformation and hostile input (S3) ---------------------------------------------------

def test_lexical_terms_split_long_short_and_quote_phrases():
    terms = search.lexical_terms('A사 계약서 "검토"')
    assert terms.tokens == ('A사', '계약서', '검토') and terms.long == ('계약서',) and terms.short == ('A사', '검토')
    assert terms.match[0] == '"A사 계약서 ""검토"""'                          # whole query, quotes doubled
    assert '"A사 계약서"' in terms.match and '"계약서 ""검토"' in terms.match     # neighbour pairs as written
    assert '"계약서"' in terms.match and '"A사"' not in terms.match              # short tokens never MATCH
    assert search.lexical_terms('PRJ-012 견적').match[:2] == ('"PRJ-012 견적"', '"PRJ-012"')
    assert search.lexical_terms('QA').match == () and search.lexical_terms('QA').short == ('QA',)
    many = search.lexical_terms(' '.join(f'낱말{n}' for n in range(200)))
    assert len(many.tokens) == search.MAX_TOKENS and len(many.match) <= 130


HOSTILE = ['"', '""', '"""', '*', 'NEAR(', 'NEAR(a b, 2)', 'a:b', 'text:견적', '-견적', '^견적', '(견적)', '견적 AND',
           'OR OR', '\x00\x01견적\x7f', '😀😀😀 견적', 'a' * 1000, '견적서' * 333, '\ud800견적서', '{a b}', '[x]',
           "'; DROP TABLE chunks; --", '%_\\', 'NOT 견적서', '   견적서   ', '​​견적']


def test_fts_expression_never_raises_on_hostile_queries(engine, indexed):
    for query in HOSTILE:
        for mode in search.MODES:
            check_contract(engine.search(query, mode=mode))
    for bad in ['', '   ', '\x00\x01', '​', None, 'a' * 1001]:
        with pytest.raises(search.SearchError) as caught:
            engine.search(bad)
        assert caught.value.code in ('empty_query', 'query_too_long')
    db = ro(indexed[0])
    try:
        assert db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0] > 0   # nothing was dropped
    finally:
        db.close()


def test_empty_query_and_limit_errors(engine):
    cases = [(dict(query=''), 'empty_query'), (dict(query='견적', limit=0), 'limit_out_of_range'),
             (dict(query='견적', limit=51), 'limit_out_of_range'), (dict(query='견적', limit=10**9), 'limit_out_of_range'),
             (dict(query='견적', limit=True), 'limit_out_of_range'), (dict(query='견적', limit='5'), 'limit_out_of_range'),
             (dict(query='견적', mode='fuzzy'), 'mode_invalid'), (dict(query='x' * 1001), 'query_too_long')]
    for kwargs, code in cases:
        with pytest.raises(search.SearchError) as caught:
            engine.search(**kwargs)
        assert caught.value.code == code
    assert set(search.ERROR_CODES) >= {code for _, code in cases}


def test_limits_are_enforced(engine):
    bad_filters = [dict(path_prefix=''), dict(path_prefix='x' * 1025), dict(path_prefix='a\x00b'),
                   dict(ext=['.exe']), dict(ext='.txt'), dict(ext=['.txt'] * 21), dict(ext=[]), dict(ext=[3]),
                   dict(ext=['txt']), dict(since='yesterday'), dict(until='2031-13-01'),
                   dict(since='2031-02-01', until='2031-01-01'), dict(ext=['.' + 'a' * 5000])]
    for kwargs in bad_filters:
        with pytest.raises(search.SearchError) as caught:
            engine.search('사양서 개정판', **kwargs)
        assert caught.value.code == 'filter_invalid', kwargs
    result = engine.search('사양서 개정판', limit=50)
    assert len(result['hits']) == 50
    assert engine.search('사양서 개정판', mode='lexical', limit=1)['hits'][0]['rank'] == 1
    filters = engine.search('사양서', ext=['.TXT', '.txt', '.md'])['meta']['filters']
    assert filters['ext'] == ['.md', '.txt']


def test_short_only_query_uses_like_fallback_or_notice(indexed):
    home = indexed[0]
    engine = search.Engine(home, embedder_factory=hash_factory())
    result = engine.search('QA', mode='lexical')
    assert result['meta']['lexical']['engine'] == 'like' and result['hits']
    assert all('qa' in h['snippet'].casefold() for h in result['hits'])
    assert result['meta']['lexical']['terms'] == {'match': [], 'short': ['QA']}
    # (c) MATCH found nothing but a short token is there: LIKE again
    result = engine.search('견적 없는말이다', mode='lexical')
    assert result['meta']['lexical']['engine'] == 'like' and result['hits']
    # over the LIKE cap: no lexical search, a notice instead
    small = search.Engine(home, replace(SearchSettings(), like_max_chunks=5), embedder_factory=hash_factory())
    capped = small.search('QA', mode='lexical')
    assert capped['hits'] == [] and search.SHORT_QUERY_NOTICE in capped['notice']
    assert capped['meta']['lexical']['engine'] == 'none'
    assert search.NO_RESULTS_NOTICE in capped['notice']


def test_like_is_used_when_fts5_is_missing(indexed, monkeypatch):
    monkeypatch.setattr(search, 'fts_usable', lambda db: False)
    result = search.Engine(indexed[0]).search('사양서 개정판', mode='lexical')
    assert result['meta']['lexical']['engine'] == 'like' and result['hits']
    assert result['meta']['lexical']['terms']['match'] == []


# ---- ranking ---------------------------------------------------------------------------------------

def test_rrf_average_ranks_and_hit_reasons():
    ranks = search.average_ranks({'a': 3.0, 'b': 2.0, 'c': 2.0, 'd': 1.0})
    assert ranks == {'a': 1.0, 'b': 2.5, 'c': 2.5, 'd': 4.0}
    assert search.average_ranks({'x': (5.0, 0.0), 'y': (5.0, 0.5), 'z': (5.0, 0.0)}) == {'y': 1.0, 'x': 2.5, 'z': 2.5}
    assert search.average_ranks({}) == {}
    fused = search.rrf([{'a': 1.0, 'b': 2.0}, {'b': 1.0, 'c': 2.0}])
    assert fused['b'] == pytest.approx(1 / 62 + 1 / 61) and fused['a'] == pytest.approx(1 / 61)
    assert fused['c'] == pytest.approx(1 / 62)
    # tied chunks share a contribution (no position bias inside a tie group)
    tied = search.rrf([search.average_ranks({'p': 1.0, 'q': 1.0})])
    assert tied['p'] == tied['q'] == pytest.approx(1 / 61.5)


def test_modes_lexical_semantic_hybrid(engine):
    query = 'PRJ-012 사양서 개정판'
    lexical = engine.search(query, mode='lexical', limit=50)
    semantic = engine.search(query, mode='semantic', limit=50)
    hybrid = engine.search(query, mode='hybrid', limit=50)
    assert {h['scores']['hit'] for h in lexical['hits']} == {'lexical'}
    assert all(h['scores']['cosine'] is None and h['scores']['bm25'] is not None for h in lexical['hits'])
    bm25 = [h['scores']['bm25'] for h in lexical['hits']]
    assert bm25 == sorted(bm25)
    assert {h['scores']['hit'] for h in semantic['hits']} == {'semantic'}
    cosines = [h['scores']['cosine'] for h in semantic['hits']]
    assert cosines == sorted(cosines, reverse=True) and min(cosines) > 0
    assert semantic['meta']['lexical']['engine'] == 'none'
    # chunks holding the whole query come first (search v2), each group in fusion order
    flags = [h['scores']['exact_phrase'] for h in hybrid['hits']]
    assert flags == sorted(flags, reverse=True)
    for group in (True, False):
        fusion = [h['scores']['fusion'] for h in hybrid['hits'] if h['scores']['exact_phrase'] is group]
        assert fusion == sorted(fusion, reverse=True)
    reasons = {h['scores']['hit'] for h in hybrid['hits']}
    assert 'both' in reasons
    for hit in hybrid['hits']:
        expected = sum(1 / (60 + r) for r in (hit['scores']['lexical_rank'], hit['scores']['semantic_rank'])
                       if r is not None)
        assert hit['scores']['fusion'] == pytest.approx(expected)
    # the exact topic sentence is first lexically
    assert lexical['hits'][0]['doc']['path'].endswith(('topic12.' + lexical['hits'][0]['doc']['ext'][1:]))


def test_no_results_notice(engine):
    result = engine.search('존재하지않는낱말조합입니다', mode='lexical')
    assert result['hits'] == [] and result['notice'] == [search.NO_RESULTS_NOTICE]
    assert engine.search('사양서 개정판')['notice'] == []


# ---- filters and eligible versions -----------------------------------------------------------------

def test_filters_path_prefix_ext_since_until(engine, indexed):
    home, root = indexed
    by_prefix = engine.search('사양서 개정판', path_prefix=str(root/'docx'), limit=50)
    assert by_prefix['hits'] and all(Path(h['doc']['path']).parent.name == 'docx' for h in by_prefix['hits'])
    lower = engine.search('사양서 개정판', path_prefix=str(root/'DOCX').upper(), limit=50)   # Windows: case-insensitive
    assert [h['chunk_id'] for h in lower['hits']] == [h['chunk_id'] for h in by_prefix['hits']]
    assert engine.search('사양서', path_prefix=str(root/'doc%'))['hits'] == []             # % is literal
    by_ext = engine.search('사양서 개정판', ext=['.hwp', '.pdf'], limit=50)
    assert by_ext['hits'] and {h['doc']['ext'] for h in by_ext['hits']} <= {'.hwp', '.pdf'}
    assert by_ext['meta']['eligible_chunks'] < engine.search('사양서')['meta']['eligible_chunks']
    for mode in search.MODES:   # semantic candidates are filtered too
        hits = engine.search('사양서 개정판', mode=mode, ext=['.eml'], limit=50)['hits']
        assert hits and {h['doc']['ext'] for h in hits} == {'.eml'}
    everything = engine.search('사양서', since='2000-01-01', until='2999-12-31T23:59:59+09:00')
    assert everything['hits'] and search.OBSERVED_NOTICE in everything['notice']
    future = engine.search('사양서', since='2999-01-01')
    assert future['hits'] == [] and future['meta']['eligible_chunks'] == 0
    assert engine.search('사양서', until='2000-01-01T00:00:00Z')['hits'] == []


def test_latest_active_versions_only(tmp_path):
    def write(root):
        root.mkdir(parents=True)
        (root/'a.txt').write_text(f'{SENTINEL} 첫 판의 고유문장 알파입니다', encoding='utf-8')
        (root/'b.txt').write_text(f'{SENTINEL} 지워질 문서의 고유문장 베타입니다', encoding='utf-8')

    home, root = build_home(tmp_path/'home', write)
    rewrite(root/'a.txt', f'{SENTINEL} 둘째 판의 고유문장 감마입니다'.encode())
    (root/'b.txt').unlink()
    db = store.open_home(home)
    try:
        scan(db, add_root(db, root))
        index.run_index(db, home, embedder=HashEmbedder(DIMS))
    finally:
        db.close()
    engine = search.Engine(home, embedder_factory=hash_factory())
    for mode in search.MODES:
        texts = [h['snippet'] for h in engine.search('고유문장', mode=mode, limit=50)['hits']]
        assert texts and all('감마' in t for t in texts), (mode, texts)
    assert engine.search('알파입니다', mode='lexical')['hits'] == []
    assert engine.search('베타입니다', mode='lexical')['hits'] == []


# ---- model-less paths (completion condition ③, part) ------------------------------------------------

def test_lexical_only_when_model_missing_with_note(indexed):
    engine = search.Engine(indexed[0])          # the real manifest model; this home has no model files
    for mode in ('hybrid', 'semantic'):
        result = engine.search('사양서 개정판', mode=mode)
        check_contract(result)
        assert result['meta']['mode'] == 'lexical' and result['hits']
        assert result['meta']['semantic'] == search.UNAVAILABLE
        assert result['meta']['semantic_note'].startswith('model_missing: ')
    assert engine.status()['loaded'] is False and engine.loads == 1


def test_search_survives_runtime_import_failure(indexed):
    def blocked():
        raise ImportError('DLL load failed (synthetic)')

    engine = search.Engine(indexed[0], embedder_factory=blocked)
    result = engine.search('사양서 개정판')
    assert result['hits'] and result['meta']['mode'] == 'lexical'
    assert result['meta']['semantic_note'] == 'runtime_import_failed:ImportError'
    assert {h['scores']['hit'] for h in result['hits']} == {'lexical'}


def test_model_load_failure_is_tried_once_per_process(indexed):
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError('synthetic load failure')

    engine = search.Engine(indexed[0], embedder_factory=broken)
    notes = {engine.search('사양서', mode=m)['meta']['semantic_note'] for m in ('hybrid', 'semantic', 'hybrid')}
    assert notes == {'runtime_error:RuntimeError'} and len(calls) == 1
    engine.reset()
    engine.search('사양서')
    assert len(calls) == 2


def test_search_does_not_load_the_model_for_lexical_mode(indexed):
    calls = []

    def factory():
        calls.append(1)
        return HashEmbedder(DIMS)

    engine = search.Engine(indexed[0], embedder_factory=factory)
    engine.search('사양서 개정판', mode='lexical')
    assert calls == [] and engine.status()['loaded'] is False
    with pytest.raises(search.SearchError):
        engine.search('', mode='hybrid')          # bad input never touches the model
    assert calls == []
    engine.search('사양서 개정판')
    assert calls == [1]


def test_query_embedding_is_called_once_per_search(indexed):
    class Spy(HashEmbedder):
        calls = []

        def embed(self, texts, *, query):
            self.calls.append((len(texts), query))
            return super().embed(texts, query=query)

    engine = search.Engine(indexed[0], embedder_factory=lambda: Spy(DIMS))
    engine.search('사양서 개정판')
    engine.search('견적 금액', mode='semantic')
    assert Spy.calls == [(1, True), (1, True)]


# ---- vector memory (S1) and the engine ---------------------------------------------------------------

def test_matrix_is_float16_and_sized(engine, indexed):
    engine.search('사양서')
    cache = engine._vectors
    db = ro(indexed[0])
    try:
        distinct = db.execute('SELECT COUNT(DISTINCT text_sha256) FROM chunks').fetchone()[0]
    finally:
        db.close()
    assert cache.matrix.dtype == np.float16 and cache.matrix.shape == (distinct, DIMS)
    assert cache.matrix.nbytes == distinct * DIMS * 2 == engine.status()['matrix_bytes']
    assert len(cache.chunk_ids) == len(cache.chunk_rows) >= distinct and cache.invalid_vectors == 0


def test_fp16_matrix_ranks_like_fp32(engine, indexed, corpus):
    engine.search('사양서')
    cache = engine._vectors
    db = ro(indexed[0])
    try:
        full = {key: index.from_blob(blob, DIMS) for key, blob in
                db.execute('SELECT text_sha256, vector FROM chunk_vectors')}
    finally:
        db.close()
    exact = np.stack([full[key] for key in cache.keys]).astype(np.float32)
    model = HashEmbedder(DIMS)
    for query in corpus.queries[:30]:
        q = search.embed_query(model, query.text, DIMS)
        fp32 = np.argsort(-(exact @ q), kind='stable')[:20]
        fp16 = np.argsort(-cache.scores(q), kind='stable')[:20]
        overlap = len(set(fp32) & set(fp16))
        assert overlap >= 19, (query.text, overlap)
        assert np.max(np.abs(exact @ q - cache.scores(q))) < 2e-3


def test_over_cap_switches_to_streaming(indexed, corpus):
    home = indexed[0]
    cached = search.Engine(home, embedder_factory=hash_factory())
    streamed = search.Engine(home, replace(SearchSettings(), max_vectors_in_memory=10), embedder_factory=hash_factory())
    for query in corpus.queries[::7]:
        for kwargs in ({}, {'ext': ['.docx', '.txt']}):
            a = cached.search(query.text, mode='semantic', limit=50, **kwargs)
            b = streamed.search(query.text, mode='semantic', limit=50, **kwargs)
            assert b['meta']['semantic']['streamed'] is True and a['meta']['semantic']['streamed'] is False
            assert [h['chunk_id'] for h in a['hits']] == [h['chunk_id'] for h in b['hits']]
            assert [h['scores']['cosine'] for h in a['hits']] == pytest.approx([h['scores']['cosine'] for h in b['hits']])
    assert streamed.status()['rows'] == 0 and streamed.status()['matrix_bytes'] == 0


def test_corrupt_vectors_are_skipped_and_counted(tmp_path):
    lines = [f'{SENTINEL} 벡터 손상 시험 첫 줄', '두 번째 줄의 고유한 내용']

    def write(root):
        root.mkdir(parents=True)
        (root/'v.txt').write_text('\n'.join(lines), encoding='utf-8')

    home, _ = build_home(tmp_path/'home', write)
    db = sqlite3.connect(paths.db_path(home))
    db.execute("UPDATE chunk_vectors SET vector = zeroblob(length(vector))")
    db.commit()
    db.close()
    engine = search.Engine(home, embedder_factory=hash_factory())
    result = engine.search('고유한 내용', mode='hybrid')
    assert result['hits'] and engine._vectors.invalid_vectors >= 1 and engine._vectors.rows == 0
    assert {h['scores']['hit'] for h in result['hits']} == {'lexical'}


def test_matrix_reloads_when_index_generation_changes(tmp_path):
    def write(root):
        root.mkdir(parents=True)
        (root/'g.txt').write_text(f'{SENTINEL} 세대 시험 문서 첫 판', encoding='utf-8')

    home, root = build_home(tmp_path/'home', write)
    engine = search.Engine(home, embedder_factory=hash_factory())
    engine.search('세대 시험')
    first = engine._vectors
    engine.search('세대 시험')
    assert engine._vectors is first                     # same generation: cached
    (root/'h.txt').write_text(f'{SENTINEL} 새로 생긴 문서의 다른 내용', encoding='utf-8')
    db = store.open_home(home)
    try:
        scan(db, add_root(db, root))
        index.run_index(db, home, embedder=HashEmbedder(DIMS))
    finally:
        db.close()
    result = engine.search('새로 생긴 문서', mode='semantic')
    assert engine._vectors is not first and engine._vectors.generation != first.generation
    assert engine._vectors.rows > first.rows and any('새로 생긴' in h['snippet'] for h in result['hits'])


def test_engine_unloads_after_idle(indexed):
    now = [1000.0]
    engine = search.Engine(indexed[0], replace(SearchSettings(), model_idle_seconds=600),
                           embedder_factory=hash_factory(), clock=lambda: now[0])
    assert engine.maybe_unload() is False                          # nothing loaded
    engine.search('사양서')
    assert engine.status()['loaded'] and engine.status()['rows'] > 0
    now[0] += 599
    assert engine.maybe_unload() is False and engine.status()['loaded']
    engine.search('견적')                                           # use refreshes the clock
    now[0] += 599
    assert engine.maybe_unload() is False
    assert engine.maybe_unload(now[0] + 1) is True
    status = engine.status()
    assert set(status) == search.ENGINE_STATUS_KEYS
    assert status == {'loaded': False, 'rows': 0, 'generation': None, 'data_generation': None, 'matrix_bytes': 0,
                      'load_failure': None, 'idle_seconds': 599.0, 'loads': 1,
                      'last_load_ms': status['last_load_ms'], 'loading': False, 'last_verify_ms': None}
    assert status['last_load_ms'] >= 0
    assert engine.search('사양서')['meta']['semantic']['vectors'] > 0   # loads again on demand
    assert engine.loads == 2


def test_engine_loads_in_the_background_and_searches_lexically_meanwhile(indexed):
    """2026-10-29 (VPS start): ``preload_in_background`` loads on a daemon thread; meanwhile ``embedder()``
    answers None at once, a semantic or hybrid search is lexical with ``MODEL_LOADING_NOTE``, the idle release
    leaves it alone and a second preload does not start; ``done`` runs after the load, then searches are
    semantic. A failed background load is recorded once like a lazy one."""
    import threading
    release, entered, done = threading.Event(), threading.Event(), threading.Event()
    threads = []

    def slow():
        threads.append(threading.current_thread())
        entered.set()
        assert release.wait(30)
        return HashEmbedder(DIMS)

    engine = search.Engine(indexed[0], embedder_factory=slow)
    assert engine.preload_in_background(done.set) is True
    assert entered.wait(30) and engine.loading and engine.status()['loading'] is True
    assert threads[0].daemon and threads[0] is not threading.current_thread()
    assert engine.embedder() is None and engine.preload_in_background() is False
    hybrid = engine.search('사양서')
    assert hybrid['meta']['semantic_note'] == search.MODEL_LOADING_NOTE and hybrid['meta']['mode'] == 'lexical'
    assert hybrid['meta']['semantic'] == search.UNAVAILABLE and hybrid['hits']          # lexical hits
    assert engine.search('사양서', mode='lexical')['meta']['semantic_note'] is None
    assert engine.maybe_unload(10**12) is False and engine.loads == 1
    release.set()
    assert done.wait(30) and not engine.loading and engine.status()['loaded']
    assert engine.search('사양서', mode='semantic')['meta']['semantic']['vectors'] > 0
    assert engine.loads == 1 and engine.preload_in_background() is False                 # loaded: nothing to do

    def broken():
        raise RuntimeError('synthetic')
    failed, finished = search.Engine(indexed[0], embedder_factory=broken), threading.Event()
    assert failed.preload_in_background(finished.set) and finished.wait(30)
    assert failed.load_failure == 'runtime_error:RuntimeError' and not failed.loading
    assert failed.search('사양서')['meta']['semantic_note'] == 'runtime_error:RuntimeError'
    assert failed.preload_in_background() is False and failed.loads == 1


def test_server_threads_default_is_half_the_cpus():
    assert SearchSettings().effective_threads(16) == 8 and SearchSettings().effective_threads(1) == 1
    assert replace(SearchSettings(), threads=3).effective_threads(16) == 3


# ---- read-only (S5) and the static "no point outside the table" checks (§6.4) ----------------------

def test_search_opens_read_only(indexed, monkeypatch):
    seen = []
    real = sqlite3.connect

    def spy(database, *args, **kwargs):
        seen.append((str(database), kwargs.get('uri', False)))
        return real(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', spy)
    engine = search.Engine(indexed[0], embedder_factory=hash_factory())
    for mode in search.MODES:
        engine.search('사양서 개정판', mode=mode)
    assert seen and all(db.endswith('?mode=ro') and uri for db, uri in seen)   # not even ':memory:'


def test_vector_loading_and_fetchall_only_in_allowed_functions():
    tree = ast.parse(SEARCH_PY.read_text(encoding='utf-8'))
    places = {'frombuffer': set(), 'from_blob': set(), 'fetchall': set()}

    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, (ast.FunctionDef, ast.ClassDef)):
                name = f'{owner}.{child.name}' if owner else child.name
            if isinstance(child, ast.Attribute) and child.attr in places:
                places[child.attr].add(owner)
            if isinstance(child, ast.Name) and child.id in places:
                places[child.id].add(owner)
            visit(child, name)

    visit(tree, '')
    assert places['frombuffer'] <= {'VectorCache.load', 'VectorCache.stream'}
    assert places['frombuffer'] == {'VectorCache.load', 'VectorCache.stream'}
    assert places['from_blob'] == set()
    assert places['fetchall'] == {'_hits'}


# ---- console output ----------------------------------------------------------------------------------

def test_format_hits(engine):
    result = engine.search('PRJ-012 사양서 개정판', limit=3)
    lines = search.format_hits(result)
    assert lines[0].startswith('3 hits  mode hybrid  lexical fts5')
    assert any(locator.display(result['hits'][0]['locator']) in line for line in lines)
    assert any(line.strip() == ' '.join(result['hits'][0]['snippet'].split()) for line in lines)
    empty = search.format_hits(engine.search('존재하지않는낱말조합입니다', mode='lexical'))
    assert empty[0].startswith('0 hits') and empty[-1] == f'notice: {search.NO_RESULTS_NOTICE}'
    noted = search.format_hits(search.Engine(engine.home).search('사양서'))
    assert any(line.startswith('semantic: model_missing') for line in noted)


def test_hybrid_puts_chunks_holding_the_whole_query_first(tmp_path):
    """Stage 2: in hybrid mode a chunk that holds the whole query verbatim ranks before chunks that only
    share words or meaning with it; lexical and semantic orders are unchanged (search v2)."""
    def write(root):
        root.mkdir(parents=True)
        (root/'exact.txt').write_text('첫 줄은 관계없는 합성 문장이다.\n합성 납품분 검수 결과 보고는 금요일이다.\n',
                                      encoding='utf-8')
        (root/'near.txt').write_text('합성 납품분 검수 결과 검수 결과 납품분 보고 보고 결과 검수 납품분 합성 보고.\n',
                                     encoding='utf-8')
        (root/'other.txt').write_text('관계없는 합성 회의 기록이다.\n', encoding='utf-8')

    home, root = build_home(tmp_path/'home', write)
    engine = search.Engine(home, embedder_factory=hash_factory())
    query = '납품분 검수 결과 보고는'
    result = engine.search(query, mode='hybrid')
    check_contract(result)
    first = result['hits'][0]
    assert Path(first['doc']['path']).name == 'exact.txt' and first['scores']['exact_phrase'] is True
    assert all(hit['scores']['exact_phrase'] is False for hit in result['hits'][1:])
    for mode in ('lexical', 'semantic'):
        assert all(hit['scores']['exact_phrase'] is False for hit in engine.search(query, mode=mode)['hits'])
    db = ro(home)
    try:
        ids = [row[0] for row in db.execute('SELECT id FROM chunks')]
        assert len(search.exact_phrase_ids(db, '  검수 결과   보고는 ', ids)) == 1   # whitespace folded
        assert search.exact_phrase_ids(db, '납품', ids) == set()                  # shorter than a trigram
        assert search.exact_phrase_ids(db, '납품분 검수', []) == set()
    finally:
        db.close()
    assert '[both, exact]' in search.format_hits(result)[1] or '[lexical, exact]' in search.format_hits(result)[1]


# ---- review M2-1: a long-lived Engine follows scan, not only index -------------------------------

STALE_TEXTS = {'a.txt': '합성 삭제 대상 문서 알파 회의록 내용 첫째 줄입니다.\n알파 문서 둘째 줄 납품 일정 확인.\n',
               'b.txt': '합성 변경 대상 문서 베타 견적서 원래 내용입니다.\n베타 문서 둘째 줄 검수 결과 보고.\n',
               'c.txt': '합성 그대로 문서 감마 사양서 내용입니다.\n감마 문서 둘째 줄 담당자 회의 기록.\n'}
NEW_B = '합성 변경 대상 문서 베타 새로 쓴 내용입니다.\n베타 문서 둘째 줄 새 견적 금액 확정.\n'
STALE_QUERIES = ('알파 회의록 내용', '베타 견적서 원래', '베타 새로 쓴 내용', '감마 사양서 내용', '합성 문서')


def stale_home(tmp_path):
    def write(root):
        root.mkdir(parents=True)
        for name, text in STALE_TEXTS.items():
            (root/name).write_text(text, encoding='utf-8')
    return build_home(tmp_path/'home', write)


def eligible_versions(home) -> dict[str, int]:
    db = ro(home)
    try:
        return {Path(path).name: version for version, path in
                db.execute(f'SELECT v.id, d.path FROM {store.eligible_versions_sql()}')}
    finally:
        db.close()


def assert_only_eligible(engine, home):
    latest = eligible_versions(home)
    seen = 0
    for query in STALE_QUERIES:
        for mode in search.MODES:
            for hit in engine.search(query, mode=mode, limit=20)['hits']:
                name = Path(hit['doc']['path']).name
                assert name in latest and hit['version_id'] == latest[name], (mode, query, name)
                seen += 1
    assert seen > 0
    return latest


def test_long_lived_engine_never_returns_deleted_or_superseded_versions_after_scan(tmp_path):
    home, root = stale_home(tmp_path)
    engine = search.Engine(home, embedder_factory=hash_factory())
    before = assert_only_eligible(engine, home)   # the cache is loaded now
    assert set(before) == {'a.txt', 'b.txt', 'c.txt'}
    (root/'a.txt').unlink()
    rewrite(root/'b.txt', NEW_B.encode('utf-8'))
    db = store.open_home(home)
    try:
        scan(db, add_root(db, root))              # scan only: no index run, index_generation unchanged
    finally:
        db.close()
    latest = assert_only_eligible(engine, home)
    assert set(latest) == {'b.txt', 'c.txt'}      # b's new version is the latest, but has no chunks yet
    for mode in search.MODES:
        names = {Path(h['doc']['path']).name for h in engine.search('합성 문서', mode=mode, limit=20)['hits']}
        assert names == {'c.txt'}, mode
    db = store.open_home(home)
    try:
        index.run_index(db, home, embedder=HashEmbedder(DIMS))
    finally:
        db.close()
    latest = assert_only_eligible(engine, home)
    assert set(latest) == {'b.txt', 'c.txt'}
    for mode in search.MODES:                     # the new version is found in every mode
        top = engine.search('베타 새로 쓴 내용', mode=mode)['hits']
        assert top and Path(top[0]['doc']['path']).name == 'b.txt' and top[0]['version_id'] == latest['b.txt']


def test_long_lived_engine_after_an_index_run_that_stopped(tmp_path):
    """Chunks dropped and added by an index run that never reached its end (no index_generation bump)."""
    home, root = stale_home(tmp_path)
    engine = search.Engine(home, embedder_factory=hash_factory())
    assert_only_eligible(engine, home)
    rewrite(root/'b.txt', NEW_B.encode('utf-8'))

    class Failing:
        identity = HashEmbedder(DIMS).identity

        def embed(self, texts, *, query):
            raise KeyboardInterrupt

    db = store.open_home(home)
    try:
        scan(db, add_root(db, root))
        generation = store.meta_get(db, 'index_generation')
        with pytest.raises(KeyboardInterrupt):
            index.run_index(db, home, embedder=Failing())
        assert store.meta_get(db, 'index_generation') == generation
    finally:
        db.close()
    latest = assert_only_eligible(engine, home)
    assert set(latest) == {'a.txt', 'b.txt', 'c.txt'}
    lexical = engine.search('베타 새로 쓴 내용', mode='lexical')['hits']
    assert lexical[0]['version_id'] == latest['b.txt']


def test_hits_drop_a_candidate_whose_chunk_text_changed(tmp_path):
    home, _ = stale_home(tmp_path)
    db = ro(home)
    try:
        chunk_id, text_sha256 = db.execute('SELECT id, text_sha256 FROM chunks ORDER BY id').fetchone()
        scores = {'bm25': None, 'lexical_rank': None, 'cosine': 0.5, 'semantic_rank': 1.0, 'fusion': 0.01,
                  'exact_phrase': False, 'hit': 'semantic', 'score_is_probability': False}
        assert len(search._hits(db, [(chunk_id, scores)], [], {chunk_id: text_sha256})) == 1
        assert search._hits(db, [(chunk_id, scores)], [], {chunk_id: 'f' * 64}) == []
    finally:
        db.close()


# ---- M3 §9.1 N1: an FTS index out of step means the LIKE path ---------------------------------------

DIRTY_NEW = f'{SENTINEL} 합성 변경문서 감마델타엡실론 확정사항입니다.\n'
DIRTY_QUERY = '감마델타엡실론 확정사항입니다'          # every token >= 3 characters: no LIKE fallback by (c)


def old_fts_usable(db):
    """The M2 judgement (table there and readable, ``fts_dirty`` not looked at)."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'").fetchone() is None:
        return False
    try:
        db.execute('SELECT rowid FROM chunks_fts LIMIT 0').fetchone()
    except sqlite3.Error:
        return False
    return True


def test_fts_dirty_switches_lexical_to_like_with_notice(tmp_path, monkeypatch):
    home, root = stale_home(tmp_path)
    engine = search.Engine(home, embedder_factory=hash_factory())
    assert engine.search('베타 견적서 원래', mode='lexical')['meta']['lexical']['engine'] == 'fts5'
    with monkeypatch.context() as patch:              # chunks change in a Python without FTS5
        patch.setattr(store, 'fts5_available', lambda: False)
        rewrite(root/'b.txt', DIRTY_NEW.encode('utf-8'))
        db = store.open_home(home)
        try:
            scan(db, add_root(db, root))
            index.run_index(db, home, embedder=HashEmbedder(DIMS))
            assert store.meta_get(db, 'fts_dirty') == '1'
        finally:
            db.close()
    assert store.fts5_available()                     # FTS5 is back; chunks_fts is still out of step
    latest = eligible_versions(home)
    for mode in ('lexical', 'hybrid'):
        result = engine.search(DIRTY_QUERY, mode=mode)
        check_contract(result)
        assert result['meta']['lexical']['engine'] == 'like', mode
        assert search.FTS_DIRTY_NOTICE in result['notice'] and search.SHORT_QUERY_NOTICE not in result['notice']
        top = result['hits'][0]
        assert Path(top['doc']['path']).name == 'b.txt' and top['version_id'] == latest['b.txt']
        assert '감마델타엡실론' in top['snippet']
    # over the LIKE cap: no lexical search, the same notice
    capped = search.Engine(home, replace(SearchSettings(), like_max_chunks=1)).search(DIRTY_QUERY, mode='lexical')
    assert capped['meta']['lexical']['engine'] == 'none' and capped['hits'] == []
    assert search.FTS_DIRTY_NOTICE in capped['notice'] and search.SHORT_QUERY_NOTICE not in capped['notice']
    # mutation check: with the M2 judgement the stale FTS index is used and misses the current text
    with monkeypatch.context() as patch:
        patch.setattr(search, 'fts_usable', old_fts_usable)
        stale = engine.search(DIRTY_QUERY, mode='lexical')
        assert stale['meta']['lexical']['engine'] == 'fts5' and stale['hits'] == []
        assert search.FTS_DIRTY_NOTICE not in stale['notice']
    # the next index with FTS5 rebuilds it: back to FTS5, the new text found
    db = store.open_home(home)
    try:
        index.run_index(db, home, embedder=HashEmbedder(DIMS))
        assert store.meta_get(db, 'fts_dirty') is None
    finally:
        db.close()
    rebuilt = engine.search(DIRTY_QUERY, mode='lexical')
    assert rebuilt['meta']['lexical']['engine'] == 'fts5' and rebuilt['hits'][0]['version_id'] == latest['b.txt']
    assert search.FTS_DIRTY_NOTICE not in rebuilt['notice']


# ---- M3 §9.2 N2: an index run that stopped must re-map the semantic candidates ---------------------

N2_TEXTS = {'near.txt': '합성 근접 문서 오메가 항목 정밀 측정 결과 보고서',
            'mid1.txt': '합성 근접 문서 오메가 항목 측정 일정 보고',
            'mid2.txt': '합성 근접 문서 오메가 측정 장비 목록'}
N2_QUERY = N2_TEXTS['near.txt']


class StopsAtFirstBatch:
    """Embeds nothing: the index run stops in its vector phase, after the chunk phase committed."""
    identity = HashEmbedder(DIMS).identity

    def embed(self, texts, *, query):
        raise KeyboardInterrupt


def test_stopped_index_run_remaps_semantic_candidates(tmp_path, monkeypatch):
    def write(root):
        root.mkdir(parents=True)
        for name, text in N2_TEXTS.items():
            (root/name).write_text(f'{SENTINEL} {text}\n', encoding='utf-8')

    home, root = build_home(tmp_path/'home', write)
    settings = replace(SearchSettings(), semantic_candidates=2)
    engines = [search.Engine(home, settings, embedder_factory=hash_factory()) for _ in range(2)]
    for engine in engines:                            # warm: matrix and chunk mapping cached
        before = engine.search(N2_QUERY, mode='semantic', limit=2)['hits']
        assert [Path(h['doc']['path']).name for h in before][0] == 'near.txt' and len(before) == 2
    (root/'near.txt').unlink()                        # the best candidate's document goes away
    (root/'new.txt').write_text(f'{SENTINEL} 합성 새 문서 아직 벡터 없음\n', encoding='utf-8')
    db = store.open_home(home)
    try:
        scan(db, add_root(db, root))
        generation, data_generation = store.meta_get(db, 'index_generation'), store.meta_get(db, 'data_generation')
        with pytest.raises(KeyboardInterrupt):
            index.run_index(db, home, embedder=StopsAtFirstBatch())
        assert store.meta_get(db, 'index_generation') == generation       # the matrix stays valid
        assert store.meta_get(db, 'data_generation') != data_generation
    finally:
        db.close()
    # the mapping is rebuilt: the two remaining documents fill the two candidates
    hits = engines[0].search(N2_QUERY, mode='semantic', limit=2)['hits']
    assert sorted(Path(h['doc']['path']).name for h in hits) == ['mid1.txt', 'mid2.txt']
    assert engines[0].status()['data_generation'] == eligible_data_generation(home)
    # mutation check: without the re-map the dropped chunk takes a candidate slot and one hit is lost
    with monkeypatch.context() as patch:
        patch.setattr(search.VectorCache, 'remap', lambda self, db: None)
        assert len(engines[1].search(N2_QUERY, mode='semantic', limit=2)['hits']) == 1


def eligible_data_generation(home):
    db = ro(home)
    try:
        return store.meta_get(db, 'data_generation', '0')
    finally:
        db.close()


# ---- M3 §2.2: one hit per document ------------------------------------------------------------------

def test_collapse_by_document_keeps_first_hit_per_document(engine, indexed):
    query = '합성 프로젝트 사양서 개정판 담당'
    total_skipped = 0
    for mode in search.MODES:
        full = engine.search(query, mode=mode, limit=50)
        assert full['meta']['collapsed'] == 0
        collapsed = engine.search(query, mode=mode, limit=10, collapse_by_document=True)
        check_contract(collapsed)
        hits = collapsed['hits']
        docs = [h['doc']['id'] for h in hits]
        assert len(docs) == len(set(docs)) == 10, mode
        assert [h['rank'] for h in hits] == list(range(1, 11))
        # the same hits as walking the full order and keeping each document's first one
        first_per_doc, skipped = [], 0
        for hit in full['hits']:
            if hit['doc']['id'] in {h['doc']['id'] for h in first_per_doc}:
                skipped += 1
                continue
            if len(first_per_doc) == 10:
                break
            first_per_doc.append(hit)
        assert [h['chunk_id'] for h in hits] == [h['chunk_id'] for h in first_per_doc], mode
        assert collapsed['meta']['collapsed'] == skipped, mode
        total_skipped += skipped
        for hit, same in zip(hits, first_per_doc):
            assert hit['scores'] == same['scores'] and hit['snippet'] == same['snippet']
    assert total_skipped > 0
    # the limit is reached before the candidates end: only the skips before the stop are counted
    wide = engine.search(query, mode='lexical', limit=50, collapse_by_document=True)
    assert len({h['doc']['id'] for h in wide['hits']}) == len(wide['hits']) == 50
    assert 0 < wide['meta']['collapsed'] < wide['meta']['lexical']['candidates'] - len(wide['hits'])


# ---- M3 §9.3: the facts of an absent-information judgement ----------------------------------------

def test_signals_block_is_present_and_low_rule_is_computed(engine, corpus):
    present = engine.search('PRJ-012 사양서 개정판')
    signals = present['meta']['signals']
    assert signals['lexical_candidates'] > 0 and signals['exact_phrase_hits'] >= 1
    assert signals['cosine_top1'] is not None and signals['cosine_top10'] is not None and signals['low'] is False
    semantic = engine.search('PRJ-012 사양서 개정판', mode='semantic')['meta']['signals']
    assert semantic['lexical_candidates'] is None and semantic['exact_phrase_hits'] is None
    assert semantic['cosine_top1'] == signals['cosine_top1']
    lexical = engine.search('PRJ-012 사양서 개정판', mode='lexical')['meta']['signals']
    assert lexical['cosine_top1'] is None and lexical['exact_phrase_hits'] == signals['exact_phrase_hits']
    # the lexical scores do not change: exact_phrase stays a hybrid-only score (M2 contract)
    assert not any(h['scores']['exact_phrase'] for h in engine.search('PRJ-012 사양서 개정판', mode='lexical')['hits'])
    # a model-less engine: no cosine facts
    assert search.Engine(engine.home).search('사양서')['meta']['signals']['cosine_top1'] is None


def test_low_signal_rules():
    facts = {'lexical_candidates': 0, 'exact_phrase_hits': 0, 'cosine_top1': 0.40, 'cosine_top10': 0.35,
             'cosine_gap': 0.05}
    rule = {'cosine_top1': 0.5, 'gap': 0.08}
    assert [search.low_signal(facts, {**rule, 'rule': r}) for r in search.LOW_SIGNAL_RULES] == [True] * 5
    # R5 (adopted): R3, or a low cosine when no candidate holds the whole query
    r5 = {**rule, 'rule': 'R5'}
    assert search.low_signal({**facts, 'lexical_candidates': 3}, r5) is True
    assert search.low_signal({**facts, 'lexical_candidates': 3, 'exact_phrase_hits': 1}, r5) is False
    assert search.low_signal({**facts, 'lexical_candidates': 3, 'cosine_top1': 0.6}, r5) is False
    assert search.low_signal({**facts, 'cosine_top1': 0.6}, r5) is True          # R3 alone
    assert search.low_signal({**facts, 'lexical_candidates': None, 'exact_phrase_hits': None}, r5) is False
    assert search.LOW_SIGNAL == {'rule': 'R5', 'cosine_top1': 0.49, 'gap': None}
    assert search.low_signal({**facts, 'lexical_candidates': 3}, {**rule, 'rule': 'R3'}) is False
    assert search.low_signal({**facts, 'lexical_candidates': 3}, {**rule, 'rule': 'R4'}) is False
    assert search.low_signal({**facts, 'cosine_top1': 0.6}, {**rule, 'rule': 'R4'}) is False
    assert search.low_signal({**facts, 'cosine_gap': 0.2}, {**rule, 'rule': 'R2'}) is False
    # a fact that mode did not produce, or a threshold not set, never makes a rule true
    assert search.low_signal({**facts, 'cosine_top1': None}, {**rule, 'rule': 'R1'}) is False
    assert search.low_signal(facts, {'rule': 'R1', 'cosine_top1': None, 'gap': None}) is False
    assert search.low_signal({**facts, 'lexical_candidates': None, 'exact_phrase_hits': None},
                             {**rule, 'rule': 'R3'}) is False
    with pytest.raises(ValueError):
        search.low_signal(facts, {'rule': 'R9'})
    assert search.signals_of(None, None, []) == {**search.DEFAULT_SIGNALS}
    built = search.signals_of(0, 0, [0.5 - n / 100 for n in range(12)], {**rule, 'rule': 'R2'})
    assert (built['cosine_top1'], built['cosine_top10'], built['cosine_gap'], built['low']) == (0.5, 0.41, 0.09, False)
    assert search.signals_of(2, 1, [0.3] * 9)['cosine_top10'] is None


def test_low_signal_fires_on_absent_query_fixture(engine, corpus, monkeypatch):
    """HashEmbedder checks the rule computation only; the thresholds come from the real-model
    measurement (doc/validation/2026-10-24_M3-신호측정_v1.md)."""
    absent = corpus_module.absent_queries()
    assert len(absent) == len(set(absent)) == 60
    fired = 0
    for text in absent:
        result = engine.search(text)
        signals = result['meta']['signals']
        assert signals['low'] == search.low_signal(signals)
        if signals['low']:
            fired += 1
            assert result['hits'] and any(line.startswith('질의와 가까운 내용이 적습니다') for line in result['notice'])
    assert fired > 0                                   # the default rule (R5) fires on some absent queries
    phrase = engine.search('PRJ-012 사양서 개정판')
    assert phrase['meta']['signals']['low'] is False and phrase['notice'] == []
    # another rule from the constant: every hybrid result is judged by it
    monkeypatch.setattr(search, 'LOW_SIGNAL', {'rule': 'R1', 'cosine_top1': 1.01, 'gap': None})
    assert engine.search('PRJ-012 사양서 개정판')['meta']['signals']['low'] is True


def test_absent_queries_are_not_in_the_corpus(corpus):
    texts = [text.casefold() for path in corpus.unit_texts for text in corpus.unit_texts[path]]
    for query in corpus_module.absent_queries():
        assert not any(query.casefold() in text for text in texts), query
    for seed in (11, 23):
        other = corpus_module.build(seed)
        texts = [t.casefold() for path in other.unit_texts for t in other.unit_texts[path]]
        assert not any(q.casefold() in t for q in corpus_module.absent_queries() for t in texts)


# ---- M3 §2.6, §6.3: engine status --------------------------------------------------------------------

def test_engine_status_reports_generations_idle_and_load_time(indexed):
    now = [50.0]
    engine = search.Engine(indexed[0], embedder_factory=hash_factory(), clock=lambda: now[0])
    cold = engine.status()
    assert set(cold) == search.ENGINE_STATUS_KEYS
    assert (cold['loaded'], cold['idle_seconds'], cold['loads'], cold['last_load_ms']) == (False, None, 0, None)
    engine.search('사양서')
    now[0] += 12.5
    warm = engine.status()
    db = ro(indexed[0])
    try:
        assert warm['generation'] == store.meta_get(db, 'index_generation', '0')
        assert warm['data_generation'] == store.meta_get(db, 'data_generation', '0')
    finally:
        db.close()
    assert warm['loaded'] and warm['idle_seconds'] == 12.5 and warm['loads'] == 1 and warm['last_load_ms'] >= 0
    json.dumps(warm)

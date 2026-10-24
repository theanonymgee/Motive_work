"""The evaluation harness over the synthetic query set (design M2 §8.3, §10): the metrics are
computed correctly and the harness runs end to end on ``HashEmbedder``. HashEmbedder is not a
model, so its semantic numbers mean nothing; the lexical side is the same as with a real model.
Real-model numbers are the stage-2 measurement (``tests/probes/measure_search.py``)."""
from pathlib import Path

import pytest

from fixtures import corpus as corpus_module
from fixtures.corpus import Query
from test_search import build_home, hash_factory
from workmcp import search

ROOT = Path('C:/corpus') if Path('C:/').exists() else Path('/corpus')


def fake_hit(rel_path, *units):
    return {'doc': {'path': str(ROOT/rel_path)}, 'units': [{'unit': unit} for unit in units]}


def test_score_query_metrics_by_hand():
    a, b = {'kind': 'line', 'line': 3}, {'kind': 'paragraph', 'index': 2}
    query = Query('Q1-001', 'Q1_phrase', 'x', gold=(('txt/a.txt', a),), gold_docs=('txt/a.txt',))
    first = corpus_module.score_query(query, [fake_hit('txt/a.txt', a)], ROOT)
    assert first == {'doc@1': 1.0, 'doc@5': 1.0, 'doc@10': 1.0, 'unit@10': 1.0, 'rr@10': 1.0}
    hits = [fake_hit(f'docx/o{n}.docx', b) for n in range(3)] + [fake_hit('txt/a.txt', {'kind': 'line', 'line': 4})]
    fourth = corpus_module.score_query(query, hits, ROOT)
    assert fourth == {'doc@1': 0.0, 'doc@5': 1.0, 'doc@10': 1.0, 'unit@10': 0.0, 'rr@10': 0.25}
    late = [fake_hit(f'docx/o{n}.docx') for n in range(10)] + [fake_hit('txt/a.txt', a)]
    assert corpus_module.score_query(query, late, ROOT) == {'doc@1': 0.0, 'doc@5': 0.0, 'doc@10': 0.0,
                                                            'unit@10': 0.0, 'rr@10': 0.0}
    sixth = [fake_hit(f'docx/o{n}.docx') for n in range(5)] + [fake_hit('txt/a.txt', b, a)]
    assert corpus_module.score_query(query, sixth, ROOT)['doc@5'] == 0.0
    assert corpus_module.score_query(query, sixth, ROOT)['unit@10'] == 1.0
    assert corpus_module.score_query(query, [], ROOT)['rr@10'] == 0.0
    outside = {'doc': {'path': str(ROOT.parent/'elsewhere'/'a.txt')}, 'units': []}
    assert corpus_module.hit_rel_path(outside, ROOT) is None


def test_summarize_averages_per_category_and_leaves_q8_q9_out():
    one = {'doc@1': 1.0, 'doc@5': 1.0, 'doc@10': 1.0, 'unit@10': 1.0, 'rr@10': 1.0}
    zero = dict.fromkeys(one, 0.0)
    table = corpus_module.summarize([('Q1_phrase', one), ('Q1_phrase', zero), ('Q3_acronym', one),
                                     ('Q8_two_chars', one), ('Q9_absent', zero)])
    assert table['Q1_phrase'] == {'n': 2, 'doc@1': 0.5, 'doc@5': 0.5, 'doc@10': 0.5, 'unit@10': 0.5, 'rr@10': 0.5}
    assert table['all']['n'] == 3 and table['all']['doc@1'] == pytest.approx(2 / 3)
    assert 'Q8_two_chars' not in table and 'Q4_synonym' not in table


def test_eval_harness_computes_metrics_with_hash_embedder(tmp_path):
    corpus = corpus_module.build()
    home, root = build_home(tmp_path/'home', lambda r: corpus_module.write(corpus, r))
    engine = search.Engine(home, embedder_factory=hash_factory())
    report = corpus_module.evaluate(lambda text, mode: engine.search(text, mode=mode, limit=10), corpus, root)
    assert set(report) == {'lexical', 'semantic', 'hybrid'}
    metric_queries = sum(q.category in corpus_module.METRIC_CATEGORIES for q in corpus.queries)
    for mode, part in report.items():
        table = part['table']
        assert table['all']['n'] == metric_queries == len(part['rows'])
        assert set(table) == {*corpus_module.METRIC_CATEGORIES, 'all'}
        for row in table.values():
            assert 0.0 <= row['doc@1'] <= row['doc@5'] <= row['doc@10'] <= 1.0
            assert row['doc@1'] <= row['rr@10'] <= row['doc@10']
        # the table is the mean of the rows it came from
        q1 = [scores['doc@10'] for _, cat, scores, _ in part['rows'] if cat == 'Q1_phrase']
        assert table['Q1_phrase']['doc@10'] == pytest.approx(sum(q1) / len(q1))
        assert len(part['behaviour']) == 15
    # Lexical search is the same with any embedder: verbatim phrases are found.
    assert report['lexical']['table']['Q1_phrase']['doc@10'] >= 0.8
    assert report['lexical']['table']['Q6_mixed']['doc@10'] >= 0.8
    # Q8: short queries go through LIKE. Q9: no phrase matches (FTS finds nothing); the short words
    # of the query then go through LIKE (design M2 §5.3 (c)), so hits are possible but recorded only.
    behaviour = report['lexical']['behaviour']
    assert {b['engine'] for b in behaviour.values() if b['category'] == 'Q8_two_chars'} == {'like'}
    for item in behaviour.values():
        if item['category'] == 'Q9_absent':
            assert item['engine'] == 'like' and item['doc@10'] is None
            assert (item['hits'] == 0) == (search.NO_RESULTS_NOTICE in item['notice'])

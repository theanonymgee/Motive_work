"""End to end (design M2 §11.3, completion conditions ②③④): ``scan -> index -> search -> verify``.

- ``test_cli_scan_index_search_verify_without_a_model``: the commands a user types, in a data home
  without model files: ``index`` builds chunks and FTS only (②, ③), a second ``index`` changes
  nothing (②), ``search --json`` answers lexically with ``semantic_note`` (③), every hit's source
  re-derives from the store and names file, version hash, unit and characters (④), ``verify`` binds
  every chunk (④).
- ``test_corpus_hybrid_search_follows_a_change_and_rederives_from_the_files``: the evaluation corpus
  with ``HashEmbedder`` (test-only, not a model) in every mode; a changed document is re-chunked and
  re-embedded alone (②); hits name only the latest versions and every snippet is cut again from the
  re-parsed current file (④).
- ``test_cli_scan_index_then_the_server_tools_answer_with_exact_sources`` (M3): ``doctor``'s app
  virtualization line, then the five tools through ``server.handle`` on the CLI-built home.

Synthetic data only; temporary data home (conftest).
"""
import json

from fixtures import SENTINEL
from fixtures import bundle as synthetic_bundle
from fixtures import corpus as corpus_module
from test_collect import add_root, scan
from workmcp import binding, cli, collect, index, locator, parsers, paths, search, store
from workmcp.embedder import HashEmbedder


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def check_hit_sources(db, hits):
    """④: the snippet locator and every ``units[]`` locator check against the store; the four source
    parts are in the display line."""
    for hit in hits:
        checked = binding.validate_locator(db, hit['locator'])
        assert checked['text'] == hit['snippet']
        for unit in hit['units']:
            binding.validate_locator(db, unit)
        shown = locator.display(hit['locator'])
        start, end = hit['locator']['chars']
        assert shown.startswith(hit['doc']['path']) and hit['doc']['sha256'][:12] in shown
        assert locator.unit_label(hit['locator']['unit']) in shown and f'chars {start}-{end}' in shown


def test_cli_scan_index_search_verify_without_a_model(workmcp_home, tmp_path, capsys):
    home = paths.ensure_home(paths.resolve_home())
    root = tmp_path/'bundle'
    synthetic_bundle.write_bundle(root)
    assert run(capsys, 'scan', '--root', str(root))[0] == 0

    code, out, err = run(capsys, 'index')
    assert code == 0, err
    assert 'vectors: skipped (model_missing' in out and 'embedded 0' in out
    code, out, _ = run(capsys, 'index')
    assert code == 0 and 'chunked 0 (reused 0' in out and 'dropped 0 versions' in out

    code, out, err = run(capsys, 'search', SENTINEL, '--json')
    assert code == 0, err
    result = json.loads(out)
    assert set(result) == search.RESULT_KEYS
    assert result['meta']['mode'] == 'lexical' and result['meta']['semantic_note'].startswith('model_missing')
    assert result['hits'] and result['notice'] == []
    db = store.open_read(home)
    try:
        check_hit_sources(db, result['hits'])
        chunk_rows = db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
    finally:
        db.close()

    code, out, _ = run(capsys, 'search', SENTINEL)
    assert code == 0 and out.splitlines()[0].startswith(f"{len(result['hits'])} hits  mode lexical")
    assert 'semantic: model_missing' in out
    code, out, _ = run(capsys, 'search', '없는주제어구절', '--mode', 'semantic', '--json')
    assert code == 0 and json.loads(out)['notice'] == [search.NO_RESULTS_NOTICE]

    code, out, _ = run(capsys, 'verify')
    assert code == 0 and 'binding ok' in out and f'chunks {chunk_rows}' in out


def test_corpus_hybrid_search_follows_a_change_and_rederives_from_the_files(workmcp_home, tmp_path):
    home = paths.ensure_home(paths.resolve_home())
    corpus = corpus_module.build()
    root = tmp_path/'corpus'
    corpus_module.write(corpus, root)
    embedder = HashEmbedder(256)
    db = store.open_home(home)
    try:
        root_id = add_root(db, root)
        scan(db, root_id)
        first = index.run_index(db, home, embedder=embedder)
        assert first['chunked'] == first['versions_eligible'] == len(corpus.topics)
        assert first['embedded'] == db.execute('SELECT COUNT(DISTINCT text_sha256) FROM chunks').fetchone()[0]

        query = next(q for q in corpus.queries
                     if q.category == 'Q1_phrase' and q.gold_docs[0].endswith(('.txt', '.md')))
        target = root/query.gold_docs[0]
        engine = search.Engine(home, embedder_factory=lambda: embedder)
        before = engine.search(query.text, mode='lexical')
        assert corpus_module.hit_rel_path(before['hits'][0], root) in query.gold_docs

        # ②: change the gold document; only it is re-chunked and only its new text embedded
        data = target.read_bytes()
        target.write_text(target.read_text(encoding='utf-8') + '\n합성 추가 문장: 변경 확인용 줄.', encoding='utf-8')
        scan(db, root_id)
        second = index.run_index(db, home, embedder=embedder)
        assert (second['chunked'], second['dropped_versions']) == (1, 1)
        assert 0 < second['embedded'] <= 2 and second['reused_vectors'] >= 0
        assert data != target.read_bytes()
    finally:
        db.close()

    latest = {}
    db = store.open_read(home)
    try:
        for version_id, doc_id in db.execute(f'SELECT v.id, d.id FROM {store.eligible_versions_sql()}'):
            latest[doc_id] = version_id
        cache = {}
        for q in corpus.queries[:40]:
            for mode in search.MODES:
                result = engine.search(q.text, mode=mode)
                check_hit_sources(db, result['hits'])
                for hit in result['hits']:
                    assert latest[hit['doc']['id']] == hit['version_id']  # latest active versions only
                    path = hit['doc']['path']
                    if path not in cache:   # ④: cut again from the re-parsed current file
                        data_now, _, _ = collect.read_source(path)
                        parsed = parsers.parse(data_now, hit['doc']['ext'], parsers.Limits())
                        cache[path] = {parsers.unit_json(u.unit): u.text for u in parsed.units}
                    start, end = hit['locator']['chars']
                    assert cache[path][parsers.unit_json(hit['locator']['unit'])][start:end] == hit['snippet']
        report = binding.verify(db)
        assert report['ok'] and report['chunks_checked'] == db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
    finally:
        db.close()


def test_cli_scan_index_then_the_server_tools_answer_with_exact_sources(workmcp_home, tmp_path, capsys):
    """M3: after ``scan`` and ``index`` from the CLI, ``doctor`` names the app virtualization state and the
    server's tools (``server.handle``, the ``serve`` path without stdio) find, quote and list the documents."""
    import os

    from workmcp import server, tools
    home = paths.ensure_home(paths.resolve_home())
    root = tmp_path/'bundle'
    synthetic_bundle.write_bundle(root)
    assert run(capsys, 'scan', '--root', str(root))[0] == 0
    assert run(capsys, 'index')[0] == 0
    code, out, _ = run(capsys, 'doctor')
    assert code == 0
    assert paths.virtualization_line(home, paths.virtualization_probe(home, os.environ)) in out.splitlines()
    db = store.open_home(home)
    state = server.make_state(home, write_db=db)
    try:
        status = json.loads(server.handle(state, 'status', {}, ('e2e', '1')))['result']
        assert status['schema_version'] == 2 and status['last_scan'] is not None
        found = json.loads(server.handle(state, 'search', {'query': SENTINEL, 'limit': 3}, ('e2e', '1')))
        assert found['ok'] and found['result']['hits']
        hit = found['result']['hits'][0]
        quoted = json.loads(server.handle(state, 'get_snippet', {'chunk_id': hit['chunk_id'], 'context_units': 1},
                                          ('e2e', '1')))
        assert quoted['ok'] and quoted['delivered_sources'] == tools.delivered(quoted)
        changes = json.loads(server.handle(state, 'recent_changes', {'kinds': ['added']}, ('e2e', '1')))['result']
        listed = json.loads(server.handle(state, 'list_documents', {'status': 'all', 'limit': 1}, ('e2e', '1')))
        assert changes['total'] == listed['result']['total'] > 0
        reader = store.open_read(home)
        try:
            check_hit_sources(reader, [{**h, 'units': tools.expand_units(h)} for h in found['result']['hits']])
            for piece in quoted['result']['context_before'] + quoted['result']['pieces'] + \
                    quoted['result']['context_after']:
                assert binding.validate_locator(reader, piece['locator'])['text'] == piece['text']
        finally:
            reader.close()
        calls = json.loads(server.handle(state, 'status', {}, ('e2e', '1')))['result']['calls']
        assert calls == {'total': 5, 'by_client': {'e2e': 5}}  # a call is logged after its answer is made
    finally:
        db.close()

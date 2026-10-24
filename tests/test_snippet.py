"""``get_snippet`` core (design M3 §2.3, §13): unit pieces of a chunk or a locator with exact locators,
whole-unit context, the piece cap with a locator for the cut range, old versions answered with
``latest`` false, and errors that carry no text. ``snippet.cover`` is the computation of
``search._hits`` (the same function), so a chunk's pieces are exactly the hit's ``units[]``.
"""
import pytest

from fixtures import SENTINEL
from fixtures import bundle as synthetic_bundle
from test_collect import add_root, rewrite, scan
from test_search import BUNDLE_QUERIES, build_home, hash_factory
from workmcp import binding, chunks, collect, index, locator, parsers, search, snippet, store
from workmcp.embedder import HashEmbedder

DIMS = 256
WORDS = ' '.join(f'낱말{n:04d}' for n in range(900))           # one unit of 8,099 characters, words only
LINES = [f'{SENTINEL} 조각 시험 문서 첫째 줄 알파 항목입니다.', '둘째 줄 베타 항목 납품 일정 확인.',
         '셋째 줄 감마 항목 검수 결과 보고.', WORDS, '다섯째 줄 델타 항목 예산 점검.',
         '여섯째 줄 엡실론 항목 회의 기록.', '일곱째 줄 제타 항목 마지막 줄.']


@pytest.fixture(scope='module')
def bundle_home(tmp_path_factory):
    return build_home(tmp_path_factory.mktemp('snippet-bundle')/'home', synthetic_bundle.write_bundle)


def write_lines(root):
    root.mkdir(parents=True)
    (root/'pieces.txt').write_text('\n'.join(LINES) + '\n', encoding='utf-8')
    (root/'other.txt').write_text(f'{SENTINEL} 다른 문서 한 줄 내용입니다.\n', encoding='utf-8')


@pytest.fixture
def lines_home(tmp_path):
    return build_home(tmp_path/'home', write_lines)


def ro(home):
    return store.open_read(home)


def check_piece(db, piece, max_chars=snippet.PIECE_MAX_CHARS):
    assert set(piece) == snippet.PIECE_KEYS and isinstance(piece['truncated'], bool)
    locator.check_shape(piece['locator'])
    assert 0 < len(piece['text']) <= max_chars
    assert binding.validate_locator(db, piece['locator'])['text'] == piece['text']


def check_result(db, result, max_chars=snippet.PIECE_MAX_CHARS):
    assert set(result) == snippet.RESULT_KEYS
    assert set(result['doc']) == snippet.DOC_KEYS and set(result['version']) == snippet.VERSION_KEYS
    assert result['chunk'] is None or set(result['chunk']) == snippet.CHUNK_KEYS
    assert result['pieces']
    for key in ('context_before', 'pieces', 'context_after'):
        for piece in result[key]:
            check_piece(db, piece, max_chars)
            obj = piece['locator']
            assert (obj['doc'], obj['path'], obj['sha256']) == (result['doc']['id'], result['doc']['path'],
                                                                result['version']['sha256'])


def ordinal_of(db, version_id, unit):
    return db.execute('SELECT ordinal FROM units WHERE version_id = ? AND locator_json = ?',
                      (version_id, parsers.unit_json(unit))).fetchone()[0]


# ---- cover is the search computation -------------------------------------------------------------

def test_cover_matches_search_hits_units(bundle_home):
    home, _ = bundle_home
    engine = search.Engine(home, embedder_factory=hash_factory(DIMS))
    db = ro(home)
    seen = multi = 0
    try:
        for query in BUNDLE_QUERIES:
            for mode in search.MODES:
                for hit in engine.search(query, mode=mode, limit=20)['hits']:
                    result = snippet.by_chunk(db, hit['chunk_id'])
                    check_result(db, result)
                    assert [p['locator'] for p in result['pieces']][:search.UNITS_CAP] == hit['units']
                    assert result['chunk']['id'] == hit['chunk_id'] and result['version']['id'] == hit['version_id']
                    assert result['version']['latest'] is True
                    assert result['version']['latest_version_id'] == hit['version_id']
                    assert result['doc'] == {k: hit['doc'][k] for k in snippet.DOC_KEYS}
                    # the pieces joined by the unit separator are the chunk text
                    text = db.execute('SELECT text FROM chunks WHERE id = ?', (hit['chunk_id'],)).fetchone()[0]
                    assert '\n'.join(p['text'] for p in result['pieces']) == text
                    seen += 1
                    multi += len(result['pieces']) > 1
    finally:
        db.close()
    assert seen >= 300 and multi > 0


def test_cover_on_rows():
    rows = [(1, '{"kind": "line", "line": 1}', 'abcde'), (2, '{"kind": "line", "line": 2}', 'fgh'),
            (3, '{"kind": "line", "line": 3}', 'ijklm')]
    # joined: 'abcde\nfgh\nijklm' -> [3, 12) is 'de\nfgh\nij'
    assert snippet.cover(rows, 3, 12) == [(1, {'kind': 'line', 'line': 1}, 'abcde', (3, 5)),
                                          (2, {'kind': 'line', 'line': 2}, 'fgh', (0, 3)),
                                          (3, {'kind': 'line', 'line': 3}, 'ijklm', (0, 2))]
    assert snippet.cover(rows, 5, 6) == []                     # only the separator
    assert [c[0] for c in snippet.cover(rows, 6, 9)] == [2]


def test_every_piece_rederives_from_the_current_file(bundle_home):
    """Each piece and context piece: the stored unit gives its text back, and so does the file parsed
    again (design M3 §5 (b)-(c), on the snippet level)."""
    home, _ = bundle_home
    db = ro(home)
    parsed, checked = {}, 0
    try:
        ids = [row[0] for row in db.execute(f'SELECT c.id FROM {store.latest_chunks_sql()} ORDER BY c.id')]
        for chunk_id in ids:
            result = snippet.by_chunk(db, chunk_id, context_units=2)
            check_result(db, result)
            path = result['doc']['path']
            if path not in parsed:
                data, sha, _ = collect.read_source(path)
                assert sha == result['version']['sha256']
                units = parsers.parse(data, result['doc']['ext'], parsers.Limits()).units
                parsed[path] = {parsers.unit_json(u.unit): u.text for u in units}
            for key in ('context_before', 'pieces', 'context_after'):
                for piece in result[key]:
                    start, end = piece['locator']['chars']
                    assert parsed[path][parsers.unit_json(piece['locator']['unit'])][start:end] == piece['text']
                    checked += 1
    finally:
        db.close()
    assert checked > len(ids)


# ---- context -----------------------------------------------------------------------------------

def test_context_units_are_whole_neighbouring_units(lines_home):
    home, _ = lines_home
    db = ro(home)
    try:
        hit = search.Engine(home).search('셋째 줄 감마 항목', mode='lexical')['hits'][0]
        for n in range(0, snippet.CONTEXT_UNITS_MAX + 1):
            result = snippet.by_locator(db, hit['locator'], context_units=n)
            check_result(db, result)
            assert result['chunk'] is None and len(result['pieces']) == 1
            assert result['pieces'][0]['text'] == hit['snippet'] and result['pieces'][0]['locator'] == hit['locator']
            first = ordinal_of(db, hit['version_id'], hit['locator']['unit'])
            before = [ordinal_of(db, hit['version_id'], p['locator']['unit']) for p in result['context_before']]
            after = [ordinal_of(db, hit['version_id'], p['locator']['unit']) for p in result['context_after']]
            assert before == list(range(max(1, first - n), first))        # the version starts at ordinal 1
            assert after == list(range(first + 1, min(len(LINES), first + n) + 1))
            for piece in result['context_before'] + result['context_after']:
                stored = db.execute('SELECT text FROM units WHERE version_id = ? AND locator_json = ?',
                                    (hit['version_id'], parsers.unit_json(piece['locator']['unit']))).fetchone()[0]
                assert piece['locator']['chars'][0] == 0 and stored.startswith(piece['text'])
                assert piece['truncated'] == (len(stored) > snippet.PIECE_MAX_CHARS)
                assert piece['truncated'] or piece['locator']['chars'][1] == len(stored)
        assert snippet.context(db, hit['version_id'], first, first, 0) == ([], [])
        assert snippet.context(db, hit['version_id'], first, first, 1) == (result['context_before'][-1:],
                                                                           result['context_after'][:1])
    finally:
        db.close()


def test_context_goes_across_chunks_and_by_chunk_matches_by_locator(lines_home):
    home, _ = lines_home
    db = ro(home)
    try:
        chunk_ids = [row[0] for row in db.execute(f'SELECT c.id FROM {store.latest_chunks_sql()} AND d.path LIKE ? '
                                                  'ORDER BY c.ordinal', ['%pieces.txt'])]
        assert len(chunk_ids) > 2                 # the long line splits the document
        for chunk_id in chunk_ids:
            result = snippet.by_chunk(db, chunk_id, context_units=1)
            check_result(db, result)
            for piece in result['pieces']:
                alone = snippet.by_locator(db, piece['locator'])
                assert alone['pieces'] == [piece] and alone['version'] == result['version']
    finally:
        db.close()


# ---- the piece cap -------------------------------------------------------------------------------

def test_piece_truncation_keeps_locator_exact(lines_home):
    home, _ = lines_home
    db = ro(home)
    try:
        hit = search.Engine(home).search('셋째 줄 감마 항목', mode='lexical')['hits'][0]
        result = snippet.by_locator(db, hit['locator'], context_units=1)
        long_piece = result['context_after'][0]
        assert long_piece['truncated'] is True and len(long_piece['text']) <= snippet.PIECE_MAX_CHARS
        for max_chars in (200, 333, 1000, snippet.PIECE_MAX_CHARS):
            cut = snippet.by_locator(db, hit['locator'], context_units=1, max_chars=max_chars)
            check_result(db, cut, max_chars)
            piece = cut['context_after'][0]
            assert piece['truncated'] and len(piece['text']) <= max_chars
            assert WORDS.startswith(piece['text']) and WORDS[len(piece['text'])] == ' '   # ends on a word end
            assert piece['locator']['chars'] == [0, len(piece['text'])]
            assert piece['locator']['text_sha256'] == locator.text_sha256(piece['text'])
            assert any('잘랐습니다' in line for line in snippet.notices(cut, max_chars))
        whole = snippet.by_locator(db, hit['locator'], max_chars=200)
        assert whole['pieces'][0]['truncated'] is False and snippet.notices(whole, 200) == []
    finally:
        db.close()


def test_trim_cuts_inside_the_locator_range():
    unit_text = '머리말 ' + ' '.join(['가나다라'] * 30) + ' 꼬리'
    obj = locator.make(1, 'C:\\x.txt', 'a' * 64, {'kind': 'line', 'line': 1}, unit_text, [4, len(unit_text)])
    piece = {'locator': obj, 'text': unit_text[4:], 'truncated': False}
    assert snippet.trim(piece, 10_000) == piece and snippet.trim(piece, 10_000) is not piece
    cut = snippet.trim(piece, 22)
    assert cut['truncated'] and cut['text'] == '가나다라 가나다라 가나다라 가나다라' and len(cut['text']) == 19
    start, end = cut['locator']['chars']
    assert unit_text[start:end] == cut['text'] and start == 4
    assert locator.check_text(cut['locator'], unit_text) == cut['text']
    assert obj['chars'] == [4, len(unit_text)]                  # the input is not changed
    spaced = locator.make(1, 'C:\\x.txt', 'a' * 64, {'kind': 'line', 'line': 1}, '  ' + 'x' * 50, [0, 52])
    hard = snippet.trim({'locator': spaced, 'text': '  ' + 'x' * 50, 'truncated': False}, 10)
    assert hard['text'] == '  ' + 'x' * 8 and hard['locator']['chars'] == [0, 10]   # one long word: hard cut


# ---- old versions and deleted documents ----------------------------------------------------------

def test_old_version_is_returned_with_latest_false(lines_home):
    home, root = lines_home
    db = ro(home)
    try:
        hit = search.Engine(home).search('셋째 줄 감마 항목', mode='lexical')['hits'][0]
    finally:
        db.close()
    rewrite(root/'pieces.txt', ('\n'.join(LINES[:2] + ['셋째 줄 바뀐 내용.']) + '\n').encode('utf-8'))
    write = store.open_home(home)
    try:
        scan(write, add_root(write, root))            # scan only: the old chunks are still stored
    finally:
        write.close()
    db = ro(home)
    try:
        new_id = db.execute('SELECT v.id FROM versions v JOIN documents d ON d.id = v.document_id WHERE d.path = ? '
                            'AND v.superseded_at IS NULL', (hit['doc']['path'],)).fetchone()[0]
        assert new_id != hit['version_id']
        for result in (snippet.by_chunk(db, hit['chunk_id'], context_units=1),
                       snippet.by_locator(db, hit['locator'], context_units=1)):
            check_result(db, result)
            assert result['version']['id'] == hit['version_id'] and result['version']['latest'] is False
            assert result['version']['latest_version_id'] == new_id and result['version']['seq'] == 1
            assert snippet.notices(result)[0] == snippet.OLD_VERSION_NOTICE.format(seq=1, latest=new_id)
    finally:
        db.close()
    write = store.open_home(home)
    try:
        index.run_index(write, home, embedder=HashEmbedder(DIMS))   # drops the old chunks; units stay
    finally:
        write.close()
    db = ro(home)
    try:
        try:                                          # the old chunk is gone (its id may name a new chunk)
            assert snippet.by_chunk(db, hit['chunk_id'])['version']['id'] != hit['version_id']
        except snippet.NotFound:
            pass
        assert snippet.by_locator(db, hit['locator'])['version']['latest'] is False   # its units stay
    finally:
        db.close()
    (root/'pieces.txt').unlink()
    write = store.open_home(home)
    try:
        scan(write, add_root(write, root))
    finally:
        write.close()
    db = ro(home)
    try:
        result = snippet.by_locator(db, hit['locator'])
        assert result['version']['latest'] is False and result['version']['latest_version_id'] is None
        assert snippet.notices(result) == [snippet.DELETED_NOTICE]
    finally:
        db.close()


# ---- errors ---------------------------------------------------------------------------------------

def test_errors_name_ids_not_text(lines_home):
    home, _ = lines_home
    db = ro(home)
    try:
        hit = search.Engine(home).search('셋째 줄 감마 항목', mode='lexical')['hits'][0]
        missing = db.execute('SELECT MAX(id) + 1000 FROM chunks').fetchone()[0]
        with pytest.raises(snippet.NotFound, match=f'chunk {missing} not found'):
            snippet.by_chunk(db, missing)
        with pytest.raises(locator.LocatorError) as shape:
            snippet.by_locator(db, {**hit['locator'], 'extra': 1})
        assert shape.value.code == 'locator_shape_invalid'
        with pytest.raises(locator.LocatorError) as mismatch:
            snippet.by_locator(db, {**hit['locator'], 'text_sha256': 'b' * 64})
        assert mismatch.value.code == 'locator_text_mismatch'
        with pytest.raises(locator.LocatorError) as out_of_range:
            snippet.by_locator(db, {**hit['locator'], 'chars': [0, 100_000]})
        assert out_of_range.value.code == 'coordinate_out_of_range'
        with pytest.raises(snippet.NotFound) as no_version:
            snippet.by_locator(db, {**hit['locator'], 'sha256': 'c' * 64})
        with pytest.raises(snippet.NotFound) as no_unit:
            snippet.by_locator(db, {**hit['locator'], 'unit': {'kind': 'line', 'line': 999}})
        assert 'cccccccccccc' in str(no_version.value)
        assert '줄 999' in str(no_unit.value) and hit['locator']['sha256'][:12] in str(no_unit.value)
        for exc in (no_version.value, no_unit.value):
            assert hit['snippet'] not in str(exc) and SENTINEL not in str(exc) and hit['doc']['path'] not in str(exc)
    finally:
        db.close()


def test_whole_neighbourhood_at_the_context_maximum(lines_home):
    """The core takes the caller's connection; with a read-only one every call works."""
    home, _ = lines_home
    db = ro(home)
    try:
        chunk_id = db.execute('SELECT MIN(id) FROM chunks').fetchone()[0]
        result = snippet.by_chunk(db, chunk_id, context_units=snippet.CONTEXT_UNITS_MAX)
        check_result(db, result)
        assert len(result['pieces'][0]['text']) <= chunks.CHUNK_MAX
    finally:
        db.close()

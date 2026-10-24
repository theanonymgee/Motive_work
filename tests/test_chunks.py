"""Chunk rules (design M2 §2, §6.2 I3, §10). Synthetic values and the synthetic bundle only."""
import hashlib
import random
import sys
import tracemalloc
from pathlib import PurePosixPath

import pytest

from fixtures import LINES_EN, LINES_KO
from fixtures.bundle import documents
from workmcp import chunks, locator, parsers


def test_constants_and_version():
    assert (chunks.CHUNK_TARGET, chunks.CHUNK_MAX, chunks.CHUNK_OVERLAP, chunks.SNIPPET_MAX) == (700, 900, 140, 600)
    assert chunks.CHUNK_VERSION == 'units-700-900-140-v1'
    assert chunks.COORD_VERSION == locator.COORD_VERSION == 'unit-words-sentences-v1'


def test_chunk_spec_hash_and_shape():
    spec = chunks.ChunkSpec(1, 2, 3, 0, 7, '합성 청크 본문')
    assert spec.text_sha256() == hashlib.sha256('합성 청크 본문'.encode()).hexdigest()
    with pytest.raises(AttributeError):
        spec.text = 'x'  # frozen


@pytest.mark.parametrize('unit, key', [
    ({'kind': 'page', 'page': 3}, ('page', 3)),
    ({'kind': 'sheet_cell', 'sheet': '시트1', 'cell': 'A1'}, ('sheet', '시트1')),
    ({'kind': 'slide', 'slide': 2, 'shape': 1}, ('slide', 2)),
    ({'kind': 'slide_cell', 'slide': 2, 'shape': 4, 'row': 1, 'col': 1}, ('slide', 2)),
    ({'kind': 'mail', 'part': 1, 'line': 9}, ('part', 1)),
    ({'kind': 'mail_header'}, ('header',)),
    ({'kind': 'section_para', 'section': 2, 'index': 5}, ('section', 2)),
    ({'kind': 'paragraph', 'index': 4}, ('body',)),
    ({'kind': 'line', 'line': 1}, ('body',)),
    ({'kind': 'cell', 'row': 1, 'columns': [1, 2]}, ('body',)),
])
def test_group_key_marks_the_boundaries_a_chunk_never_crosses(unit, key):
    assert chunks.group_key(unit) == key


# -- helpers ---------------------------------------------------------------------------------

def sentence(rng, words=(4, 14)):
    pool = ' '.join(LINES_KO + LINES_EN).replace('.', '').split()
    end = rng.choice(['.', '.', '?', '!', ''])
    return ' '.join(rng.choice(pool) for _ in range(rng.randint(*words))) + end


def synthetic_units(seed=7, count=120):
    """Units of mixed kinds and lengths: short lines, ~300-char paragraphs, pages that change, and a
    few units over CHUNK_MAX (several sentences, and one long run of words without punctuation)."""
    rng = random.Random(seed)
    units, page = [], 1
    for ordinal in range(1, count + 1):
        roll = rng.random()
        if roll < 0.05:
            text = ' '.join(sentence(rng) for _ in range(rng.randint(15, 40)))
        elif roll < 0.07:
            text = ' '.join(sentence(rng).rstrip('.?!') for _ in range(60))  # one sentence, > CHUNK_MAX
        elif roll < 0.5:
            text = sentence(rng, (2, 6))
        else:
            text = ' '.join(sentence(rng) for _ in range(rng.randint(2, 6)))
        if rng.random() < 0.1:
            page += 1
        units.append((ordinal, {'kind': 'page', 'page': page}, text))
    return units


def joined_slice(units, spec):
    texts = [text for _, _, text in units]
    return '\n'.join(texts[spec.unit_first - 1:spec.unit_last])[spec.char_start:spec.char_end]


def check_invariants(units, specs):
    """Design M2 §6.2 I3 / §2.2: ordinals 1..n, text == joined slice, <= CHUNK_MAX, every unit covered,
    no group boundary crossed, whole-unit chunks span their units, pieces cover a large unit."""
    by_ordinal = {ordinal: (unit, text) for ordinal, unit, text in units}
    assert [spec.ordinal for spec in specs] == list(range(1, len(specs) + 1))
    covered = set()
    for spec in specs:
        assert spec.text == joined_slice(units, spec)
        assert 0 < len(spec.text) <= chunks.CHUNK_MAX
        keys = {chunks.group_key(by_ordinal[o][0]) for o in range(spec.unit_first, spec.unit_last + 1)}
        assert len(keys) == 1
        if spec.unit_first != spec.unit_last or len(by_ordinal[spec.unit_first][1]) <= chunks.CHUNK_MAX:
            assert spec.char_start == 0 and spec.char_end == len(spec.text)  # whole units only
        covered |= set(range(spec.unit_first, spec.unit_last + 1))
    assert covered == set(by_ordinal)
    for ordinal, (_, text) in by_ordinal.items():
        if len(text) > chunks.CHUNK_MAX:
            pieces = sorted((s.char_start, s.char_end) for s in specs if s.unit_first == s.unit_last == ordinal)
            assert pieces[0][0] == 0 and pieces[-1][1] == len(text)
            assert all(b[0] <= a[1] for a, b in zip(pieces, pieces[1:]))  # no gap between pieces


# -- build_chunks ----------------------------------------------------------------------------

def test_build_chunks_is_deterministic_and_covers_every_unit():
    units = synthetic_units()
    first = chunks.build_chunks(units)
    assert first == chunks.build_chunks(list(units))
    assert any(len(text) > chunks.CHUNK_MAX for _, _, text in units)
    check_invariants(units, first)
    for seed in range(5):
        check_invariants(synthetic_units(seed, 60), chunks.build_chunks(synthetic_units(seed, 60)))


def test_whole_unit_chunks_are_filled_up_to_the_target():
    units = [(i, {'kind': 'line', 'line': i}, f'합성 줄 {i:03d} ' + '가' * 90) for i in range(1, 41)]
    specs = chunks.build_chunks(units)
    for spec in specs[:-1]:
        assert len(spec.text) <= chunks.CHUNK_TARGET
        following = units[spec.unit_last][2]  # the next unit did not fit
        assert len(spec.text) + 1 + len(following) > chunks.CHUNK_TARGET


def test_group_boundaries_are_never_crossed():
    units, ordinal = [], 0
    for kind, field in (('sheet_cell', 'sheet'), ('slide', 'slide'), ('mail', 'part'), ('section_para', 'section')):
        for group in (1, 2, 3):
            for _ in range(3):
                ordinal += 1
                unit = {'kind': kind, field: f'시트{group}' if field == 'sheet' else group}
                units.append((ordinal, unit, f'합성 {kind} {group} 내용 {ordinal}'))
    ordinal += 1
    units.append((ordinal, {'kind': 'mail_header'}, 'From: a@example.com'))
    specs = chunks.build_chunks(units)
    check_invariants(units, specs)
    assert len(specs) == 4 * 3 + 1  # one chunk per group: short units, no overlap across a group change


def test_overlap_carries_whole_units_only():
    units = [(i, {'kind': 'paragraph', 'index': i}, f'합성 문단 {i:02d} ' + 'x' * (50 + (i * 37) % 120))
             for i in range(1, 60)]
    specs = chunks.build_chunks(units)
    check_invariants(units, specs)
    for previous, spec in zip(specs, specs[1:]):
        carried = range(spec.unit_first, previous.unit_last + 1)
        if carried:
            texts = [units[o - 1][2] for o in carried]
            assert len('\n'.join(texts)) <= chunks.CHUNK_OVERLAP
            assert spec.unit_first > previous.unit_first  # never all of the previous chunk
        assert spec.unit_first <= previous.unit_last + 1  # no unit skipped
    assert any(spec.unit_first <= previous.unit_last for previous, spec in zip(specs, specs[1:]))


def test_carry_rule():
    assert chunks.carry([(1, 'a' * 100), (2, 'b' * 60), (3, 'c' * 79)]) == [(2, 'b' * 60), (3, 'c' * 79)]
    assert chunks.carry([(1, 'a' * 10), (2, 'b' * 10)]) == [(2, 'b' * 10)]  # never all
    assert chunks.carry([(1, 'a'), (2, 'b' * 141)]) == []
    assert chunks.carry([(1, 'a')]) == []


def test_carried_overlap_never_pushes_a_chunk_past_the_maximum():
    units = [(1, {'kind': 'line', 'line': 1}, 'a' * 600), (2, {'kind': 'line', 'line': 2}, 'b' * 100),
             (3, {'kind': 'line', 'line': 3}, 'c' * 30), (4, {'kind': 'line', 'line': 4}, 'd' * 850)]
    specs = chunks.build_chunks(units)
    check_invariants(units, specs)
    assert specs[-1].unit_last == 4 and len(specs[-1].text) <= chunks.CHUNK_MAX


def test_large_unit_is_split_at_sentences_and_covers_the_unit():
    rng = random.Random(3)
    text = ' '.join(sentence(rng) for _ in range(80))
    assert len(text) > 3 * chunks.CHUNK_MAX
    pieces = chunks.split_large_unit(9, text, 4)
    starts = {locator.anchor(text, s)['start'] for s in locator.sentences(text)} | {0}
    assert [p.ordinal for p in pieces] == list(range(4, 4 + len(pieces)))
    for piece in pieces:
        assert (piece.unit_first, piece.unit_last) == (9, 9)
        assert piece.text == text[piece.char_start:piece.char_end]
        assert piece.char_start in starts  # pieces start at a sentence
        assert piece.char_end == len(text) or piece.char_end in starts
        assert len(piece.text) <= chunks.CHUNK_TARGET + chunks.CHUNK_OVERLAP
    assert pieces[0].char_start == 0 and pieces[-1].char_end == len(text)
    for previous, piece in zip(pieces, pieces[1:]):
        assert piece.char_start <= previous.char_end  # covered without gaps
        assert previous.char_end - piece.char_start <= chunks.CHUNK_OVERLAP  # overlap is one short sentence


def test_oversized_sentence_is_hard_cut_at_word_boundary():
    rng = random.Random(5)
    text = ' '.join(sentence(rng).rstrip('.?!') for _ in range(120))  # one sentence, no line break
    assert len(locator.sentences(text)) == 1 and len(text) > 3 * chunks.CHUNK_MAX
    pieces = chunks.split_large_unit(1, text, 1)
    for piece in pieces[:-1]:
        cut = piece.char_end
        assert text[cut - 1] == ' ' and text[cut] != ' '  # at a word start
        assert chunks.CHUNK_TARGET - chunks.HARD_CUT_WINDOW <= len(piece.text) <= chunks.CHUNK_TARGET
    assert pieces[-1].char_end == len(text)
    assert [p.char_start for p in pieces[1:]] == [p.char_end for p in pieces[:-1]]  # no overlap from a cut
    # without any space in the window the cut is exactly CHUNK_TARGET characters
    solid = chunks.split_large_unit(1, 'x' * 2000, 1)
    assert [(p.char_start, p.char_end) for p in solid] == [(0, 700), (700, 1400), (1400, 2000)]


def test_chunk_text_equals_joined_slice():
    units = synthetic_units(11, 80)
    for spec in chunks.build_chunks(units):
        assert spec.text == joined_slice(units, spec)


@pytest.mark.parametrize('text', ['a b. c\nd e! f', '  앞 공백  뒤 공백  ', '하나. 둘?\n\n셋', 'one', '',
                                  'x\u2028y z.\u200bw', '“인용.” 다음 문장', 'a.b c… d'])
def test_streamed_sentence_starts_equal_locator_sentences(text):
    expected = [locator.anchor(text, s)['start'] for s in locator.sentences(text)] if text.strip() else []
    assert list(chunks._sentence_starts(text)) == expected


def test_build_chunks_memory_is_linear():
    """Design M2 §6.2 I1: ~5 MB of unit text -> peak Python allocation <= 3x the input size."""
    rng = random.Random(1)
    big = ' '.join(sentence(rng) for _ in range(30_000))           # one huge unit (TXT one-line case)
    lines = [(i, {'kind': 'line', 'line': i}, sentence(rng)) for i in range(2, 30_000)]
    units = [(1, {'kind': 'line', 'line': 1}, big), *lines]
    size = sum(sys.getsizeof(text) for _, _, text in units)
    assert size > 2_500_000
    tracemalloc.start()
    try:
        specs = chunks.build_chunks(units)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert specs and peak <= 3 * size, (peak, size)


# -- snippets --------------------------------------------------------------------------------

def test_snippet_is_inside_one_unit_and_word_aligned():
    rng = random.Random(2)
    first = ' '.join(sentence(rng) for _ in range(20))
    second = ' '.join(sentence(rng) for _ in range(30)) + ' 고유표지어 ' + ' '.join(sentence(rng) for _ in range(30))
    units = [(1, {'kind': 'paragraph', 'index': 1}, first, (0, len(first))),
             (2, {'kind': 'paragraph', 'index': 2}, second, (0, len(second)))]
    ordinal, start, end = chunks.snippet_in_unit(units, ['zz', '고유표지어'])
    assert ordinal == 2 and 0 < end - start <= chunks.SNIPPET_MAX
    snippet = second[start:end]
    assert '고유표지어' in snippet
    assert second[start - 1] == ' ' and second[start] != ' '  # word-aligned at both ends
    assert end == len(second) or (second[end] == ' ' and second[end - 1] != ' ')
    obj = locator.make(1, 'C:\\synthetic\\a.docx', 'a' * 64, units[1][1], second, [start, end])
    assert locator.check_text(obj, second) == snippet
    # case-insensitive; the longest term wins; the window stays inside the chunk range of the unit
    units = [(1, {'kind': 'line', 'line': 1}, 'Alpha beta GAMMA delta', (6, 22))]
    assert chunks.snippet_in_unit(units, ['gamma', 'alpha']) == (1, 6, 22)


def test_snippet_without_match_takes_the_first_unit():
    rng = random.Random(4)
    long = ' '.join(sentence(rng) for _ in range(40))
    units = [(3, {'kind': 'line', 'line': 3}, long, (0, len(long))), (4, {'kind': 'line', 'line': 4}, 'x', (0, 1))]
    ordinal, start, end = chunks.snippet_in_unit(units, ['없는낱말'])
    assert (ordinal, start) == (3, 0) and end <= chunks.SNIPPET_MAX
    assert long[end] == ' ' or long[end - 1] != ' '
    assert chunks.snippet_in_unit(units[1:], []) == (4, 0, 1)
    with pytest.raises(ValueError):
        chunks.snippet_in_unit([], ['a'])


# -- format shapes (design M2 §2.4) ----------------------------------------------------------

def bundle_units(ext):
    out = []
    for item, data in documents():
        if PurePosixPath(item.rel_path).suffix.lower() != ext or item.status != 'ok':
            continue
        result = parsers.parse(data, ext, parsers.Limits())
        if result.status == 'ok' and result.units:
            out.append([(u.ordinal, u.unit, u.text) for u in result.units])
    return out


@pytest.mark.parametrize('ext', ['.txt', '.csv', '.pdf', '.docx', '.pptx', '.xlsx', '.eml', '.hwp', '.hwpx'])
def test_format_shapes(ext):
    documents_units = bundle_units(ext)
    assert documents_units, ext
    for units in documents_units:
        specs = chunks.build_chunks(units)
        check_invariants(units, specs)
        kinds = {unit['kind'] for _, unit, _ in units}
        if 'mail_header' in kinds:  # the header block is a chunk of its own
            headers = [o for o, unit, _ in units if unit['kind'] == 'mail_header']
            assert any(s.unit_first == s.unit_last == headers[0] for s in specs)
        if ext == '.pdf':  # a new chunk on every page
            pages = {unit['page'] for _, unit, _ in units}
            assert len(specs) >= len(pages)

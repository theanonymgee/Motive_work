"""Chunk rules (design M2 §2). Stage 0: constants, ``ChunkSpec`` and ``group_key``; the algorithm tests
(``build_chunks``, sentence split, snippets) come with its body in stage 1. Synthetic values only.
"""
import hashlib

import pytest

from workmcp import chunks, locator


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

"""Locator format checks (design §3.3) on synthetic unit text."""
import pytest

from workmcp import locator
from workmcp.locator import LocatorError

TEXT = '합성 문단입니다. 두 번째 문장!\n셋째 줄'
SHA = 'a' * 64


def sample(chars=(0, 6)):
    return locator.make(1, r'C:\synthetic\a.docx', SHA, {'kind': 'paragraph', 'index': 3}, TEXT, chars)


def test_word_anchor_and_sentences_match_the_ported_tokenizer():
    assert locator.literal(TEXT, [1, 2]) == '합성 문단입니다.'
    assert locator.sentences(TEXT) == [[1, 2], [3, 5], [6, 7]]
    with pytest.raises(locator.CoordinateError) as info:
        locator.anchor(TEXT, [3, 2])
    assert info.value.code == 'coordinate_reversed'


def test_make_check_and_display():
    obj = sample()
    assert locator.check_text(obj, TEXT) == TEXT[0:6]
    assert locator.display(obj) == r'C:\synthetic\a.docx @ aaaaaaaaaaaa / 문단 3 / chars 0-6'


@pytest.mark.parametrize('change, code', [
    ({'v': 2}, 'locator_version_unsupported'),
    ({'v': True}, 'locator_version_unsupported'),
    ({'v': 1.0}, 'locator_version_unsupported'),
    ({'v': '1'}, 'locator_version_unsupported'),
    ({'sha256': 'XYZ'}, 'locator_hash_invalid'),
    ({'unit': {'kind': 'page'}}, 'unit_shape_invalid'),
    ({'unit': {'kind': 'nope'}}, 'unit_kind_invalid'),
    ({'unit': {'kind': 'sheet_cell', 'sheet': 'S1', 'cell': 'b12'}}, 'unit_shape_invalid'),
    ({'chars': [5, 5]}, 'coordinate_reversed'),
    ({'chars': [0, 999]}, 'coordinate_out_of_range'),
    ({'chars': [0, 7]}, 'locator_text_mismatch'),
])
def test_bad_locators_get_fixed_codes(change, code):
    obj = {**sample(), **change}
    with pytest.raises(LocatorError) as info:
        locator.check_text(obj, TEXT)
    assert info.value.code == code


def test_quote_range():
    assert locator.quote_range(TEXT, '두 번째') == [10, 14]
    with pytest.raises(LocatorError) as info:
        locator.quote_range(TEXT, '없는 말')
    assert info.value.code == 'evidence_quote_not_found'

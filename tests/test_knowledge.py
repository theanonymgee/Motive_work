"""Knowledge write path (design M4 §2-§4). Stage 0: the error class, the normalisation helpers, the fixed
signatures of ``knowledge`` and ``knowledge_query`` (bodies raise NotImplementedError until stage 1) and
the import cycle between them. Synthetic text only."""
import inspect
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from workmcp import knowledge, knowledge_query, store

ROOT = Path(__file__).resolve().parents[1]


def test_knowledge_error_carries_code_message_and_retryable():
    busy = knowledge.KnowledgeError('store_busy', 'the store is busy', retryable=True)
    assert (busy.code, busy.message, busy.retryable) == ('store_busy', 'the store is busy', True)
    assert isinstance(busy, ValueError) and knowledge.KnowledgeError('kind_mismatch', 'x').retryable is False


def test_norm_text_is_nfc_stripped_bounded_and_refuses_control_characters():
    decomposed = unicodedata.normalize('NFD', '  합성 문장\t둘\n')
    assert knowledge.norm_text(decomposed, 'statement', 100) == '합성 문장\t둘'
    assert knowledge.norm_text('가' * 5, 'name', 5) == '가' * 5
    for value, max_chars, min_chars in (('가' * 6, 5, 1), ('   ', 5, 1), ('가', 5, 2), (None, 5, 1), (3, 5, 1)):
        with pytest.raises(knowledge.KnowledgeError) as caught:
            knowledge.norm_text(value, 'name', max_chars, min_chars=min_chars)
        assert caught.value.code == 'argument_invalid' and caught.value.message.startswith('name ')
    with pytest.raises(knowledge.KnowledgeError, match='control characters'):
        knowledge.norm_text('합성\x00문장', 'quote', 100)
    with pytest.raises(knowledge.KnowledgeError, match='control characters'):
        knowledge.norm_text('합성\x85문장', 'quote', 100)


@pytest.mark.parametrize('value, good', [
    ('2026-10-25', True), ('2026-02-29', False), ('2026-9-01', False), ('2026-10-25T00:00:00', False),
    ('20261025', False), (' 2026-10-25', False), (None, False), (20261025, False), ('2024-02-29', True),
])
def test_check_date_takes_yyyy_mm_dd_only(value, good):
    if good:
        assert knowledge.check_date(value, 'valid_from') == value
    else:
        with pytest.raises(knowledge.KnowledgeError, match='valid_from must be a date YYYY-MM-DD'):
            knowledge.check_date(value, 'valid_from')


def test_constants_follow_the_store():
    assert knowledge.ENTITY_KINDS == store.ENTITY_KINDS and knowledge.ASSERTION_KINDS == ('fact', 'decision', 'todo')
    assert knowledge.TASK_STATES == ('open', 'done', 'cancelled')
    assert all(set(knowledge.DATA_KEYS[kind]) <= set(knowledge.FIELDS[kind]) for kind in knowledge.ASSERTION_KINDS)
    assert [f.name for f in knowledge.Bound.__dataclass_fields__.values()] == [
        'version_id', 'chunk_id', 'locator', 'quote', 'quote_sha256', 'current', 'flexible']


STUBS = [
    (knowledge.bind_sources, (None, [])), (knowledge.record_entity, (None,)), (knowledge.record, (None, 'fact')),
    (knowledge.supersede, (None, 1)), (knowledge.retract, (None, 1)),
    (knowledge_query.visible_sql, (None, None, False)), (knowledge_query.assertion_row, (None, 1)),
    (knowledge_query.entity_row, (None, 1)), (knowledge_query.entity_by_name, (None, 'project', 'x')),
    (knowledge_query.find_entities, (None, 'x', None, 20)), (knowledge_query.entity_detail, (None, 1)),
    (knowledge_query.list_assertions, (None, {}, 50, 0)), (knowledge_query.history, (None, 1)),
]


@pytest.mark.parametrize('function, args', STUBS, ids=[f.__name__ for f, _ in STUBS])
def test_stage_0_bodies_are_not_implemented(function, args):
    """The shared signatures are fixed (stage 1 B and C build on them); their bodies come in stage 1."""
    keywords = {name: None for name, p in inspect.signature(function).parameters.items()
                if p.kind is p.KEYWORD_ONLY and p.default is p.empty}
    with pytest.raises(NotImplementedError, match='M4 stage 1'):
        function(*args, **keywords)


@pytest.mark.parametrize('first', ['workmcp.knowledge', 'workmcp.knowledge_query', 'workmcp.tools'])
def test_knowledge_modules_import_in_any_order(first):
    """``knowledge`` and ``knowledge_query`` import each other (module form); a fresh interpreter imports
    either first."""
    code = (f'import {first}\nfrom workmcp import knowledge, knowledge_query\n'
            'assert knowledge.KnowledgeError and knowledge_query.ASSERTION_KEYS\n')
    env = {**os.environ, 'PYTHONPATH': str(ROOT/'src')}
    run = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env, timeout=60)
    assert run.returncode == 0, run.stderr

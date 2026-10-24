"""Knowledge write path (design M4 §2-§4, §12 ③④): the error class and normalisation helpers, source
binding by quote, recording, retract, supersede (with a cut period), conflicts, tasks, the busy store, and
the import cycle with ``knowledge_query``.

Every write in these tests goes through ``KB.write``, which checks the append-only rule around it (design
M4 §13.4): no row of the knowledge tables disappears, row counts only grow, a row's content never changes
(only the transitions the triggers allow: close once, conflict mark/clear, a link resolved once, aliases
added and a first note), a refused write leaves every row as it was, and every stored evidence quote is
the stored unit text at its locator. Synthetic text only."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from fixtures import SENTINEL
from fixtures import bundle as synthetic_bundle
from test_collect import add_root, rewrite, scan
from workmcp import binding, embedder, index, knowledge, knowledge_query, locator, parsers, paths, store

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


@pytest.mark.parametrize('first', ['workmcp.knowledge', 'workmcp.knowledge_query', 'workmcp.tools'])
def test_knowledge_modules_import_in_any_order(first):
    """``knowledge`` and ``knowledge_query`` import each other (module form); a fresh interpreter imports
    either first."""
    code = (f'import {first}\nfrom workmcp import knowledge, knowledge_query\n'
            'assert knowledge.KnowledgeError and knowledge_query.ASSERTION_KEYS\n')
    env = {**os.environ, 'PYTHONPATH': str(ROOT/'src')}
    run = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env, timeout=60)
    assert run.returncode == 0, run.stderr


# ---- a scanned and indexed synthetic home, and the append-only ledger -----------------------------------

NOTES = (
    '합성 과제 31의 계약 상태는 검토 중이다.',                     # 1
    '합성 반복 구절 하나 그리고 합성 반복 구절 둘',                # 2: "합성 반복 구절" twice
    '공백이   여러 개인 합성 줄입니다',                             # 3: a run of spaces
    unicodedata.normalize('NFD', '분해된 합성 문장입니다'),          # 4: an NFD source line
    f'{SENTINEL} 표지 문장은 기록에 남지 않는다',                   # 5
    '두 줄에 걸친 인용의 앞부분',                                  # 6
    '뒷부분이 여기 있습니다',                                      # 7
)
OTHER = ('다른 합성 파일의 첫 줄', '다른 합성 파일의 둘째 줄')
TABLES = ('entities', 'assertions', 'evidence', 'links', 'assertion_events')
BY = 'test-client/record_fact'


def columns(db, table):
    return [r[1] for r in db.execute(f'PRAGMA table_info({table})')]


def snapshot(db) -> dict:
    return {table: {row[0]: row for row in db.execute(f'SELECT * FROM {table}')} for table in TABLES}


def check_append_only(db, before: dict, after: dict) -> None:
    """No row went away; content never changed; only the transitions the triggers allow (design M4 §13.4)."""
    for table in TABLES:
        assert set(before[table]) <= set(after[table]), f'{table}: a row disappeared'
        names = columns(db, table)
        for key, old_row in before[table].items():
            old, new = dict(zip(names, old_row)), dict(zip(names, after[table][key]))
            if table in ('evidence', 'assertion_events'):
                assert new == old, (table, key)
            elif table == 'assertions':
                mutable = {'status', 'recorded_to', 'change_reason'}
                assert {k: v for k, v in new.items() if k not in mutable} == \
                    {k: v for k, v in old.items() if k not in mutable}, key
                if old['recorded_to'] is not None:
                    assert new == old, key
                elif new['recorded_to'] is not None:
                    assert new['status'] in ('withdrawn', 'superseded') and new['change_reason']
                else:
                    assert new['status'] in ('active', 'conflict') and new['change_reason'] == old['change_reason']
            elif table == 'entities':
                fixed = {'id', 'kind', 'name', 'created_at', 'created_by', 'merged_into'}
                assert {k: new[k] for k in fixed} == {k: old[k] for k in fixed}
                aliases = json.loads(old['aliases_json'])
                assert json.loads(new['aliases_json'])[:len(aliases)] == aliases
                assert old['note'] is None or new['note'] == old['note']
            else:
                resolved = {'resolved_at', 'resolved_by_id'}
                assert {k: v for k, v in new.items() if k not in resolved} == \
                    {k: v for k, v in old.items() if k not in resolved}
                if old['resolved_at'] is not None:
                    assert new == old


def check_evidence(db) -> None:
    """Every evidence quote is the stored unit text at its locator; an evidence row has one or more, a note none."""
    for assertion_id, version_id, locator_json, quote, quote_sha in db.execute(
            'SELECT assertion_id, version_id, locator_json, quote, quote_sha256 FROM evidence'):
        obj = json.loads(locator_json)
        text = db.execute('SELECT text FROM units WHERE version_id=? AND locator_json=?',
                          (version_id, parsers.unit_json(obj['unit']))).fetchone()[0]
        assert locator.check_text(obj, text) == quote and hashlib.sha256(quote.encode()).hexdigest() == quote_sha
    counts = db.execute('SELECT a.source_kind, COUNT(e.id) FROM assertions a LEFT JOIN evidence e '
                        'ON e.assertion_id = a.id GROUP BY a.id').fetchall()
    assert all((kind == 'evidence') == (count > 0) for kind, count in counts), counts


class KB:
    """``notes.txt`` and ``other.txt`` scanned and indexed into a fresh home; the store connection is the
    knowledge write connection. ``write`` runs one writer under the append-only ledger."""

    def __init__(self, base: Path):
        self.home = paths.ensure_home(paths.validate_home(str(base/'home'), {}, None))
        self.src = base/'src'
        self.src.mkdir(parents=True)
        (self.src/'notes.txt').write_text('\n'.join(NOTES) + '\n', encoding='utf-8')
        (self.src/'other.txt').write_text('\n'.join(OTHER) + '\n', encoding='utf-8')
        self.db = store.open_home(self.home)
        self.root = add_root(self.db, self.src)
        scan(self.db, self.root)
        index.run_index(self.db, self.home, embedder=embedder.HashEmbedder(16))

    def write(self, function, *args, **kwargs):
        before = snapshot(self.db)
        try:
            out = function(self.db, *args, **kwargs)
        except BaseException:
            assert snapshot(self.db) == before, 'a refused write changed rows'
            assert not self.db.in_transaction
            raise
        check_append_only(self.db, before, snapshot(self.db))
        check_evidence(self.db)
        return out

    def entity(self, name='합성 과제 31', kind='project', **options) -> int:
        out = self.write(knowledge.record_entity, kind=kind, name=name, recorded_by='test-client/record_entity',
                         **options)
        return out['entity']['id']

    def record(self, kind='fact', **fields) -> dict:
        if 'sources' not in fields:
            fields.setdefault('my_note', True)
        return self.write(knowledge.record, kind, recorded_by=BY, call_id='call-1', **fields)

    def fact(self, entity, value='합성 값', predicate='계약 상태', **fields) -> dict:
        return self.record('fact', entity_id=entity, predicate=predicate, value=value, **fields)

    def supersede(self, assertion_id, reason='합성 교정', **fields) -> dict:
        if 'sources' not in fields:
            fields.setdefault('my_note', True)
        return self.write(knowledge.supersede, assertion_id, reason=reason, recorded_by='test-client/supersede',
                          call_id='call-2', **fields)

    def retract(self, assertion_id, reason='합성 철회') -> dict:
        return self.write(knowledge.retract, assertion_id, reason=reason, recorded_by='test-client/retract',
                          call_id='call-3')

    def units(self, name: str) -> list[tuple[int, dict, str, int]]:
        """``(ordinal, unit, text, version_id)`` of the latest version of ``name``."""
        return [(o, json.loads(u), t, v) for o, u, t, v in self.db.execute(
            'SELECT u.ordinal, u.locator_json, u.text, v.id FROM units u JOIN versions v ON v.id = u.version_id '
            'JOIN documents d ON d.id = v.document_id WHERE d.path LIKE ? AND v.superseded_at IS NULL '
            'ORDER BY u.ordinal', (f'%{name}',))]

    def loc(self, name: str, n: int, quote: str | None = None) -> dict:
        """The locator of unit ``n`` (1-based) of ``name``, or of ``quote``'s first place in it."""
        _, unit, text, version_id = self.units(name)[n - 1]
        doc, path, sha = self.db.execute('SELECT v.document_id, d.path, v.sha256 FROM versions v JOIN documents d '
                                         'ON d.id = v.document_id WHERE v.id=?', (version_id,)).fetchone()
        chars = locator.quote_range(text, quote) if quote else [0, len(text)]
        return locator.make(doc, path, sha, unit, text, chars)

    def chunk(self, name: str, n: int) -> int:
        ordinal, _, _, version_id = self.units(name)[n - 1]
        return self.db.execute('SELECT id FROM chunks WHERE version_id=? AND ? BETWEEN unit_first AND unit_last',
                               (version_id, ordinal)).fetchone()[0]

    def row(self, assertion_id) -> dict:
        cur = self.db.execute('SELECT * FROM assertions WHERE id=?', (assertion_id,))
        return dict(zip([d[0] for d in cur.description], cur.fetchone()))

    def events(self, assertion_id) -> list[tuple]:
        return self.db.execute('SELECT kind, reason, related_id, recorded_by, call_id FROM assertion_events '
                               'WHERE assertion_id=? ORDER BY id', (assertion_id,)).fetchall()

    def links(self) -> list[tuple]:
        return self.db.execute('SELECT from_id, to_id, relation, resolved_at IS NOT NULL, resolved_by_id FROM links '
                               'ORDER BY id').fetchall()

    def count(self, table: str) -> int:
        return self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


@pytest.fixture
def kb(tmp_path):
    value = KB(tmp_path)
    yield value
    value.db.close()


def refused(kb, code, function, *args, **kwargs) -> knowledge.KnowledgeError:
    with pytest.raises(knowledge.KnowledgeError) as caught:
        kb.write(function, *args, **kwargs)
    assert caught.value.code == code, caught.value
    assert SENTINEL not in caught.value.message and len(caught.value.message) <= 400
    return caught.value


def record_args(kind='decision', **fields):
    if 'sources' not in fields:
        fields.setdefault('my_note', True)
    if kind == 'decision':
        fields.setdefault('statement', '합성 결정')
    return dict(kind=kind, recorded_by=BY, call_id='call-1', **fields)


# ---- ③ binding (design M4 §3) ------------------------------------------------------------------------

def test_quote_is_rebound_to_exact_chars_and_sha_from_stored_unit(kb):
    entity = kb.entity()
    quote = '계약 상태는 검토 중'
    start = NOTES[0].index(quote)
    by_chunk = kb.fact(entity, '검토 중', sources=[{'chunk_id': kb.chunk('notes.txt', 1), 'quote': quote}])
    sent = {**kb.loc('notes.txt', 1), 'chars': [0, 3], 'text_sha256': '0' * 64}   # the client's are wrong
    by_locator = kb.record('decision', statement='합성 결정', sources=[{'locator': sent, 'quote': quote}])
    for out, chunk_hint in ((by_chunk, kb.chunk('notes.txt', 1)), (by_locator, None)):
        [evidence] = out['assertion']['evidence']
        assert evidence['quote'] == quote and evidence['locator']['chars'] == [start, start + len(quote)]
        assert evidence['locator']['text_sha256'] == locator.text_sha256(quote)
        assert binding.validate_locator(kb.db, evidence['locator'])['text'] == quote
        assert out['assertion']['source_kind'] == 'evidence' and out['assertion']['evidence_count'] == 1
        stored = kb.db.execute('SELECT chunk_id, quote, quote_sha256, locator_json FROM evidence WHERE assertion_id=?',
                               (out['assertion']['id'],)).fetchone()
        assert stored[:3] == (chunk_hint, quote, hashlib.sha256(quote.encode()).hexdigest())
        assert json.loads(stored[3]) == evidence['locator']
    assert by_chunk['notice'] == [] and by_chunk['conflicts'] == [] and by_chunk['duplicates'] == []


@pytest.mark.parametrize('line, sent, stored, flexible', [
    (1, '합성 과제 31의', '합성 과제 31의', False),                                          # exact
    (1, '   합성 과제 31의 \n', '합성 과제 31의', False),                                    # stripped
    (1, unicodedata.normalize('NFD', '합성 과제'), '합성 과제', False),                       # NFC of the quote
    (3, '공백이 여러 개인', '공백이   여러 개인', True),                                       # whitespace runs
    (3, '공백이\n여러   개인', '공백이   여러 개인', True),
    (4, unicodedata.normalize('NFD', '분해된 합성'), unicodedata.normalize('NFD', '분해된 합성'), False),  # NFD source
], ids=['exact', 'strip', 'nfc', 'whitespace', 'newline', 'nfd_source'])
def test_quote_normalisation(kb, line, sent, stored, flexible):
    out = kb.record('decision', statement='합성 결정', sources=[{'locator': kb.loc('notes.txt', line), 'quote': sent}])
    [evidence] = out['assertion']['evidence']
    assert evidence['quote'] == stored
    assert (knowledge.FLEXIBLE_NOTICE.format(1) in out['notice']) is flexible


def test_an_nfc_quote_of_an_nfd_source_is_not_found(kb):
    """A known limit (design M4 §3.2): the stored text is never normalised, so only the quote as sent can
    match an NFD source."""
    refused(kb, 'evidence_quote_not_found', knowledge.record, **record_args(
        sources=[{'locator': kb.loc('notes.txt', 4), 'quote': '분해된 합성'}]))


def test_quote_not_found_and_ambiguous_are_refused_and_nothing_is_written(kb):
    before = (kb.count('assertions'), kb.count('evidence'))
    error = refused(kb, 'evidence_quote_not_found', knowledge.record, **record_args(
        sources=[{'chunk_id': kb.chunk('notes.txt', 1), 'quote': f'없는 {SENTINEL} 문장'}]))
    assert f'({len(SENTINEL) + 6} characters)' in error.message and 'chunk' in error.message
    repeated = '합성 반복 구절'
    places = [i for i in range(len(NOTES[1])) if NOTES[1].startswith(repeated, i)]
    assert len(places) == 2
    error = refused(kb, 'evidence_quote_ambiguous', knowledge.record, **record_args(
        sources=[{'locator': kb.loc('notes.txt', 2), 'quote': repeated}]))
    assert 'occurs 2 times' in error.message
    refused(kb, 'evidence_quote_ambiguous', knowledge.record, **record_args(
        sources=[{'chunk_id': kb.chunk('notes.txt', 2), 'quote': repeated}]))
    refused(kb, 'evidence_quote_ambiguous', knowledge.record, **record_args(   # the quote once in each of two units
        sources=[{'chunk_id': kb.chunk('other.txt', 1), 'quote': '다른 합성 파일의'}]))
    assert (kb.count('assertions'), kb.count('evidence')) == before
    for place in places:                                     # the client's chars name one place
        hinted = {**kb.loc('notes.txt', 2), 'chars': [place, place + len(repeated)]}
        out = kb.record('decision', statement='합성 결정', sources=[{'locator': hinted, 'quote': repeated}])
        assert out['assertion']['evidence'][0]['locator']['chars'] == [place, place + len(repeated)]


def test_quote_across_two_units_is_refused(kb):
    error = refused(kb, 'evidence_quote_not_found', knowledge.record, **record_args(
        sources=[{'chunk_id': kb.chunk('notes.txt', 6), 'quote': '인용의 앞부분 뒷부분이 여기'}]))
    assert knowledge.QUOTE_HINT in error.message
    out = kb.record('decision', statement='합성 결정', sources=[       # two units: two sources
        {'chunk_id': kb.chunk('notes.txt', 6), 'quote': '인용의 앞부분'},
        {'chunk_id': kb.chunk('notes.txt', 7), 'quote': '뒷부분이 여기'},
        {'chunk_id': kb.chunk('notes.txt', 7), 'quote': '뒷부분이 여기'}])   # the same unit and range once
    assert [e['quote'] for e in out['assertion']['evidence']] == ['인용의 앞부분', '뒷부분이 여기']


def test_chunk_gone_and_bad_locators_are_refused_with_codes(kb):
    error = refused(kb, 'chunk_not_found', knowledge.record, **record_args(
        sources=[{'chunk_id': 999_999, 'quote': '합성 과제'}]))
    assert knowledge.CHUNK_GONE_HINT in error.message
    good = kb.loc('notes.txt', 1)
    for obj, code in (({**good, 'v': 9}, 'locator_invalid'), ({**good, 'sha256': 'f' * 64}, 'not_found'),
                      ({**good, 'unit': {'kind': 'line', 'line': 99}}, 'not_found'), ({'doc': 1}, 'locator_invalid')):
        refused(kb, code, knowledge.record, **record_args(sources=[{'locator': obj, 'quote': '합성 과제'}]))
    for quote in ('가', '합성\x00과제', None, '가' * 1001):
        refused(kb, 'argument_invalid', knowledge.record, **record_args(sources=[{'locator': good, 'quote': quote}]))
    for item in ({'quote': '합성 과제'}, {'chunk_id': 1, 'locator': good, 'quote': '합성 과제'},
                 {'locator': good, 'quote': '합성 과제', 'chars': [0, 2]}, '합성 과제', {'chunk_id': 0, 'quote': '합성'}):
        refused(kb, 'argument_invalid', knowledge.record, **record_args(sources=[item]))
    refused(kb, 'argument_invalid', knowledge.record, **record_args(sources=[{'locator': good, 'quote': '합성'}] * 6))


def test_old_version_and_deleted_document_sources_are_accepted_with_notice(kb):
    old = kb.loc('other.txt', 1, '다른 합성 파일')
    rewrite(kb.src/'other.txt', '바뀐 합성 파일의 첫 줄\n'.encode())
    scan(kb.db, kb.root)
    changed = kb.record('decision', statement='합성 결정', sources=[{'locator': old, 'quote': '다른 합성 파일'}])
    assert knowledge.OLD_SOURCE_NOTICE.format(1) in changed['notice']
    assert changed['assertion']['evidence'][0]['source_state'] == 'changed'
    (kb.src/'other.txt').unlink()
    scan(kb.db, kb.root)
    deleted = kb.record('decision', statement='합성 결정 2', sources=[{'locator': old, 'quote': '다른 합성 파일'}])
    assert knowledge.OLD_SOURCE_NOTICE.format(1) in deleted['notice']
    assert deleted['assertion']['evidence'][0]['source_state'] == 'deleted'
    current = kb.record('decision', statement='합성 결정 3', sources=[{'locator': kb.loc('notes.txt', 1),
                                                                    'quote': '합성 과제'}])
    assert current['notice'] == [] and current['assertion']['evidence'][0]['source_state'] == 'current'


@pytest.mark.parametrize('tool', ['record_fact', 'record_decision', 'record_task', 'supersede'])
def test_my_note_is_stored_without_evidence_and_labelled(kb, tool):
    entity = kb.entity()
    if tool == 'record_fact':
        out = kb.fact(entity)['assertion']
    elif tool == 'record_decision':
        out = kb.record('decision', statement='합성 결정')['assertion']
    elif tool == 'record_task':
        out = kb.record('todo', statement='합성 할 일')['assertion']
    else:
        first = kb.fact(entity, sources=[{'locator': kb.loc('notes.txt', 1), 'quote': '합성 과제'}])['assertion']
        out = kb.supersede(first['id'], value='새 합성 값')['new']
    assert (out['source_kind'], out['source_label'], out['evidence'], out['evidence_count']) == \
        ('my_note', knowledge.MY_NOTE_LABEL, [], 0)
    assert kb.db.execute('SELECT COUNT(*) FROM evidence WHERE assertion_id=?', (out['id'],)).fetchone()[0] == 0
    good = [{'locator': kb.loc('notes.txt', 1), 'quote': '합성 과제'}]
    for evidence in ({}, {'sources': good, 'my_note': True}, {'sources': []}, {'sources': None, 'my_note': False}):
        refused(kb, 'evidence_required', knowledge.record, 'decision', recorded_by=BY, call_id='c',
                statement='합성 결정', **evidence)
        refused(kb, 'evidence_required', knowledge.supersede, out['id'], reason='합성', recorded_by=BY,
                call_id='c', **evidence)


def test_every_stored_evidence_rederives_from_the_current_file(tmp_path):
    """The synthetic bundle (txt, md, csv, docx, pptx, xlsx, pdf, hwp, eml ...) scanned: one decision per
    format cites a whole unit; ``binding.verify`` re-reads every file and re-checks every evidence row."""
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    synthetic_bundle.write_bundle(tmp_path/'src')
    db = store.open_home(home)
    try:
        scan(db, add_root(db, tmp_path/'src'))
        units = db.execute(
            "SELECT d.ext, d.id, d.path, v.sha256, u.locator_json, u.text FROM units u JOIN versions v ON v.id = "
            "u.version_id JOIN documents d ON d.id = v.document_id WHERE d.status = 'active' AND v.superseded_at IS "
            'NULL ORDER BY d.ext, d.id, u.ordinal').fetchall()
        chosen = {}
        for ext, doc, path, sha, unit_json, text in units:
            if ext in chosen or not 2 <= len(text) <= knowledge.QUOTE_MAX or text != text.strip() or \
                    any(ch in knowledge._CONTROL for ch in text) or text != unicodedata.normalize('NFC', text):
                continue
            chosen[ext] = locator.make(doc, path, sha, json.loads(unit_json), text, [0, len(text)]), text
        assert len(chosen) >= 6, sorted(chosen)
        for ext, (obj, text) in chosen.items():
            knowledge.record(db, 'decision', recorded_by=BY, call_id='c', statement=f'합성 결정 {ext}',
                             sources=[{'locator': obj, 'quote': text}])
        check_evidence(db)
        report = binding.verify(db)
        assert report['ok'] and report['evidence_checked'] == db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]
        assert report['evidence_checked'] == len(chosen)
    finally:
        db.close()


# ---- ④ retract, supersede, tasks, conflicts (design M4 §4) -------------------------------------------

def test_record_fact_makes_its_statement_and_checks_its_entities(kb):
    project, person = kb.entity(), kb.entity('합성 사람 갑', 'person')
    by_value = kb.fact(project, '3억', unit='원', predicate='예산')['assertion']
    by_object = kb.fact(project, None, predicate='담당자', object_entity_id=person)['assertion']
    given = kb.fact(project, '합성', predicate='메모', statement='  합성 문장  ')['assertion']
    assert (by_value['statement'], by_object['statement'], given['statement']) == \
        ('합성 과제 31 예산: 3억원', '합성 과제 31 담당자 합성 사람 갑', '합성 문장')
    assert by_value['data'] == {'value': '3억', 'unit': '원'} and by_object['object_entity_id'] == person
    assert kb.events(by_value['id']) == [('recorded', None, None, BY, 'call-1')]
    refused(kb, 'entity_not_found', knowledge.record, **record_args('fact', entity_id=999, predicate='p', value='v'))
    refused(kb, 'entity_not_found', knowledge.record,
            **record_args('fact', entity_id=project, predicate='p', object_entity_id=999))
    refused(kb, 'entity_not_found', knowledge.record, **record_args('todo', statement='합성', owner_entity_id=999))
    for fields in ({'entity_id': project, 'predicate': 'p'}, {'entity_id': project, 'predicate': ' ', 'value': 'v'},
                   {'predicate': 'p', 'value': 'v'}, {'entity_id': project, 'predicate': 'p', 'unit': '원'},
                   {'entity_id': project, 'predicate': 'p', 'value': 'v', 'valid_from': '2026-10-02',
                    'valid_to': '2026-10-01'},
                   {'entity_id': project, 'predicate': 'p', 'value': 'v', 'valid_from': '2026/10/01'},
                   {'entity_id': project, 'predicate': 'p', 'value': 'v', 'options': ['A']}):
        refused(kb, 'argument_invalid', knowledge.record, **record_args('fact', **fields))
    refused(kb, 'argument_invalid', knowledge.record, **record_args('decision', statement='합성', options=['A'] * 21))


def test_record_entity_creates_adds_aliases_and_never_merges(kb):
    by = 'test-client/record_entity'
    first = kb.write(knowledge.record_entity, kind='project', name=' 합성 과제 P ', aliases=['P', 'P', '합성 과제 P'],
                     recorded_by=by)
    assert first['created'] is True and first['added_aliases'] == ['P'] and first['alias_of'] == []
    entity = first['entity']
    assert (entity['name'], entity['aliases'], entity['note'], entity['created_by']) == ('합성 과제 P', ['P'], None, by)
    again = kb.write(knowledge.record_entity, kind='project', name='합성 과제 P', aliases=['P', '피'], note='합성 메모',
                     recorded_by=by)
    assert (again['created'], again['added_aliases'], again['entity']['aliases'], again['entity']['note']) == \
        (False, ['피'], ['P', '피'], '합성 메모')
    kept = kb.write(knowledge.record_entity, kind='project', name='합성 과제 P', note='다른 메모', recorded_by=by)
    assert kept['entity']['note'] == '합성 메모' and kept['added_aliases'] == []
    other = kb.write(knowledge.record_entity, kind='org', name='P', recorded_by=by)   # same string, other entity
    assert other['created'] is True and other['alias_of'] == [{'id': entity['id'], 'kind': 'project',
                                                               'name': '합성 과제 P'}]
    assert other['notice'] == [knowledge.ALIAS_OF_NOTICE.format(entity['id'])] and kb.count('entities') == 2
    refused(kb, 'argument_invalid', knowledge.record_entity, kind='team', name='합성', recorded_by=by)
    refused(kb, 'argument_invalid', knowledge.record_entity, kind='project', name='합성 과제 P',
            aliases=[f'별칭 {n}' for n in range(19)], recorded_by=by)   # 2 + 19 > 20


def test_retract_closes_row_adds_event_and_never_edits_content(kb):
    entity = kb.entity()
    fact = kb.fact(entity, sources=[{'locator': kb.loc('notes.txt', 1), 'quote': '합성 과제'}])['assertion']
    before = kb.row(fact['id'])
    out = kb.retract(fact['id'], '  합성 오기  ')
    assert out['assertion']['status'] == 'withdrawn' and out['assertion']['evidence'] is None  # no quote back
    assert out['cleared_conflicts'] == [] and out['notice'] == []
    after = kb.row(fact['id'])
    assert (after['status'], after['change_reason']) == ('withdrawn', '합성 오기') and after['recorded_to']
    assert {k: v for k, v in after.items() if k not in ('status', 'recorded_to', 'change_reason')} == \
        {k: v for k, v in before.items() if k not in ('status', 'recorded_to', 'change_reason')}
    assert kb.events(fact['id']) == [('recorded', None, None, BY, 'call-1'),
                                     ('retracted', '합성 오기', None, 'test-client/retract', 'call-3')]
    refused(kb, 'already_closed', knowledge.retract, fact['id'], reason='다시', recorded_by=BY, call_id='c')
    refused(kb, 'assertion_not_found', knowledge.retract, 999, reason='합성', recorded_by=BY, call_id='c')
    other = kb.fact(entity, '다른 값', predicate='예산')['assertion']
    refused(kb, 'argument_invalid', knowledge.retract, other['id'], reason='  ', recorded_by=BY, call_id='c')
    refused(kb, 'argument_invalid', knowledge.retract, other['id'], reason='가' * 501, recorded_by=BY, call_id='c')


def test_supersede_makes_new_row_closes_old_links_and_events(kb):
    entity = kb.entity()
    old = kb.fact(entity, '검토 중', sources=[{'locator': kb.loc('notes.txt', 1), 'quote': '검토 중'}])['assertion']
    out = kb.supersede(old['id'], reason='합성 갱신', value='승인')
    new = out['new']
    assert out['cut'] is None and out['old']['status'] == 'superseded' and out['old']['evidence'] is None
    assert (new['supersedes_id'], new['entity_id'], new['predicate'], new['data']) == \
        (old['id'], entity, '계약 상태', {'value': '승인', 'unit': None})
    assert new['statement'] == '합성 과제 31 계약 상태: 승인' and new['source_kind'] == 'my_note'  # never inherited
    assert kb.row(old['id'])['change_reason'] == '합성 갱신'
    assert kb.links() == [(new['id'], old['id'], 'supersedes', 0, None)]
    assert kb.events(old['id'])[-1] == ('superseded', '합성 갱신', new['id'], 'test-client/supersede', 'call-2')
    assert kb.events(new['id']) == [('recorded', None, old['id'], 'test-client/supersede', 'call-2')]
    decision = kb.record('decision', statement='합성 결정', options=['A', 'B'], chosen='A')['assertion']
    changed = kb.supersede(decision['id'], chosen='B', rationale='합성 이유')['new']
    assert (changed['statement'], changed['data']) == ('합성 결정', {'options': ['A', 'B'], 'chosen': 'B',
                                                                    'rationale': '합성 이유'})


def test_supersede_with_old_valid_to_cuts_the_period(kb):
    entity = kb.entity()
    source = {'locator': kb.loc('notes.txt', 1), 'quote': '검토 중'}
    old = kb.fact(entity, '검토 중', valid_from='2026-10-01', sources=[source])['assertion']
    out = kb.supersede(old['id'], value='승인', old_valid_to='2026-10-20',
                       sources=[{'chunk_id': kb.chunk('notes.txt', 1), 'quote': '합성 과제 31'}])
    cut, new = out['cut'], out['new']
    assert (cut['valid_from'], cut['valid_to'], cut['data'], cut['supersedes_id'], cut['status']) == \
        ('2026-10-01', '2026-10-20', {'value': '검토 중', 'unit': None}, old['id'], 'active')
    assert (new['valid_from'], new['valid_to'], new['supersedes_id'], new['status']) == \
        ('2026-10-20', None, cut['id'], 'active')
    assert [(e['locator'], e['quote']) for e in cut['evidence']] == \
        [(e['locator'], e['quote']) for e in old['evidence']]          # copied and re-checked
    assert cut['statement'] == old['statement'] and out['conflicts'] == []
    assert kb.links() == [(cut['id'], old['id'], 'supersedes', 0, None), (new['id'], cut['id'], 'supersedes', 0, None)]
    assert kb.events(cut['id']) == [('recorded', None, old['id'], 'test-client/supersede', 'call-2')]
    assert kb.events(old['id'])[-1][:3] == ('superseded', '합성 교정', cut['id'])
    later = kb.fact(entity, '계속', predicate='단계', valid_from='2026-10-01', valid_to='2026-12-01')['assertion']
    for bad in ('2026-10-01', '2026-09-30', '2026-12-01', '2027-01-01', '2026-13-01'):
        refused(kb, 'argument_invalid', knowledge.supersede, later['id'], reason='합성', recorded_by=BY,
                call_id='c', my_note=True, old_valid_to=bad)
    overlap = kb.supersede(later['id'], value='다음', valid_from='2026-11-01', old_valid_to='2026-11-15')
    assert [c['id'] for c in overlap['conflicts']] == [overlap['cut']['id']]   # the new row starts before the cut
    assert overlap['new']['status'] == overlap['cut']['status'] == 'conflict'


def test_supersede_kind_mismatch_and_closed_are_refused(kb):
    entity = kb.entity()
    decision = kb.record('decision', statement='합성 결정')['assertion']
    task = kb.record('todo', statement='합성 할 일')['assertion']
    fact = kb.fact(entity)['assertion']
    for assertion_id, fields in ((decision['id'], {'predicate': '담당'}), (decision['id'], {'due': '2026-11-01'}),
                                 (task['id'], {'value': '값'}), (task['id'], {'old_valid_to': '2026-12-01'}),
                                 (fact['id'], {'state': 'done'}), (fact['id'], {'decided_on': '2026-10-01'})):
        refused(kb, 'kind_mismatch', knowledge.supersede, assertion_id, reason='합성', recorded_by=BY,
                call_id='c', my_note=True, **fields)
    kb.supersede(fact['id'], value='새 값')
    refused(kb, 'already_closed', knowledge.supersede, fact['id'], reason='합성', recorded_by=BY, call_id='c',
            my_note=True, value='또')
    refused(kb, 'assertion_not_found', knowledge.supersede, 999, reason='합성', recorded_by=BY, call_id='c',
            my_note=True)
    refused(kb, 'entity_not_found', knowledge.supersede, decision['id'], reason='합성', recorded_by=BY,
            call_id='c', my_note=True, entity_id=999)


def test_task_state_changes_are_new_rows(kb):
    owner = kb.entity('합성 사람 을', 'person')
    task = kb.record('todo', statement='합성 할 일: 제안서 검토', due='2026-11-01', owner_entity_id=owner)['assertion']
    today = task['valid_from']
    assert task['data'] == {'state': 'open', 'due': '2026-11-01', 'owner_entity_id': owner, 'done_at': None}
    done = kb.supersede(task['id'], reason='완료', state='done')['new']
    assert done['data'] == {'state': 'done', 'due': '2026-11-01', 'owner_entity_id': owner, 'done_at': today}
    assert (done['supersedes_id'], done['valid_from'], done['statement']) == (task['id'], today, task['statement'])
    reopened = kb.supersede(done['id'], reason='다시 열기', state='open')['new']
    assert reopened['data']['state'] == 'open' and reopened['data']['done_at'] is None
    cancelled = kb.supersede(reopened['id'], reason='취소', state='cancelled', done_at='2026-10-30')['new']
    assert cancelled['data']['done_at'] == '2026-10-30'
    open_rows = kb.db.execute("SELECT id FROM assertions WHERE kind='todo' AND recorded_to IS NULL").fetchall()
    assert open_rows == [(cancelled['id'],)] and store.knowledge_counts(kb.db)['tasks_open'] == 0
    finished = kb.record('todo', statement='합성 끝난 일', state='done')['assertion']
    assert finished['data']['done_at'] == today
    refused(kb, 'argument_invalid', knowledge.record, **record_args('todo', statement='합성', done_at='2026-10-01'))
    refused(kb, 'argument_invalid', knowledge.record, **record_args('todo', statement='합성', state='later'))


OVERLAPS = [
    ((None, None), (None, None), True),
    (('2026-01-01', None), (None, None), True),
    (('2026-01-01', '2026-02-01'), ('2026-02-01', None), False),          # adjacent: the end is exclusive
    (('2026-01-01', '2026-03-01'), ('2026-02-01', '2026-02-15'), True),   # contained
    (('2026-01-01', '2026-02-01'), ('2026-03-01', '2026-04-01'), False),
    ((None, '2026-02-01'), ('2026-01-15', None), True),
    ((None, '2026-02-01'), ('2026-02-01', None), False),
]


@pytest.mark.parametrize('first, second, conflict', OVERLAPS)
def test_conflict_is_detected_marked_linked_and_shown(kb, first, second, conflict):
    entity = kb.entity()
    a = kb.fact(entity, '값 A', valid_from=first[0], valid_to=first[1])['assertion']
    out = kb.fact(entity, '값 B', valid_from=second[0], valid_to=second[1])
    b = out['assertion']
    if conflict:
        assert out['conflicts'] == [{'id': a['id'], 'value': '값 A', 'valid_from': first[0], 'valid_to': first[1],
                                     'recorded_from': a['recorded_from']}]
        assert knowledge.CONFLICT_NOTICE.format(a['id']) in out['notice']
        assert (kb.row(a['id'])['status'], b['status']) == ('conflict', 'conflict')
        assert kb.links() == [(b['id'], a['id'], 'conflicts', 0, None)]
        assert kb.events(a['id'])[-1][:3] == ('conflict_marked', None, b['id'])
        assert kb.events(b['id'])[-1][:3] == ('conflict_marked', None, a['id'])
        assert kb.row(a['id'])['change_reason'] is None              # a conflict never uses change_reason
    else:
        assert out['conflicts'] == [] and b['status'] == 'active' and kb.links() == []


def test_only_the_same_subject_predicate_and_another_value_conflict(kb):
    project, other, person = kb.entity(), kb.entity('합성 과제 32'), kb.entity('합성 사람 병', 'person')
    first = kb.fact(project, '값 A')['assertion']
    for out in (kb.fact(project, '값 B', predicate='다른 속성'), kb.fact(other, '값 B'),
                kb.record('decision', statement='합성 결정', entity_id=project)):
        assert out['conflicts'] == []
    same = kb.fact(project, unicodedata.normalize('NFD', '값 A'), sources=[{'locator': kb.loc('notes.txt', 1),
                                                                            'quote': '합성 과제'}])
    assert same['duplicates'] == [first['id']] and same['conflicts'] == []
    assert knowledge.DUPLICATE_NOTICE.format(first['id']) in same['notice']
    by_object = kb.fact(project, None, object_entity_id=person)
    assert sorted(c['id'] for c in by_object['conflicts']) == sorted([first['id'], same['assertion']['id']])
    with_unit = kb.fact(other, '값 B', unit='원')
    assert [c['id'] for c in with_unit['conflicts']] == [kb.db.execute(
        "SELECT id FROM assertions WHERE entity_id=? AND predicate='계약 상태' ORDER BY id", (other,)).fetchone()[0]]


def test_conflict_resolves_on_retract_or_supersede_and_reconflicts_with_new_value(kb):
    entity = kb.entity()
    a = kb.fact(entity, '값 A')['assertion']
    b = kb.fact(entity, '값 B')['assertion']
    c = kb.fact(entity, '값 C')['assertion']               # conflicts with A and B
    assert {(f, t) for f, t, *_ in kb.links()} == {(b['id'], a['id']), (c['id'], a['id']), (c['id'], b['id'])}
    out = kb.retract(b['id'])
    assert out['cleared_conflicts'] == [a['id'], c['id']]
    assert (kb.row(a['id'])['status'], kb.row(c['id'])['status']) == ('conflict', 'conflict')   # A-C still open
    assert [(f, t, resolved, by) for f, t, _, resolved, by in kb.links() if resolved] == \
        [(b['id'], a['id'], 1, b['id']), (c['id'], b['id'], 1, b['id'])]
    moved = kb.supersede(a['id'], value='값 C')                # agrees with C now
    new = moved['new']
    assert moved['cleared_conflicts'] == [c['id']] and moved['duplicates'] == [c['id']] and moved['conflicts'] == []
    assert kb.row(c['id'])['status'] == 'active' and new['status'] == 'active'
    assert kb.events(c['id'])[-1][:3] == ('conflict_cleared', None, a['id'])
    assert all(resolved for *_, resolved, _ in [link for link in kb.links() if link[2] == 'conflicts'])
    again = kb.supersede(new['id'], value='값 D')
    assert [x['id'] for x in again['conflicts']] == [c['id']] and kb.row(c['id'])['status'] == 'conflict'


def test_a_busy_store_answers_store_busy_and_writes_nothing(kb):
    entity = kb.entity()
    other = sqlite3.connect(paths.db_path(kb.home), isolation_level=None)
    kb.db.execute('PRAGMA busy_timeout=100')
    try:
        other.execute('BEGIN IMMEDIATE')
        error = refused(kb, 'store_busy', knowledge.record, **record_args('fact', entity_id=entity, predicate='p',
                                                                        value='v'))
        assert error.retryable is True
        refused(kb, 'store_busy', knowledge.retract, 1, reason='합성', recorded_by=BY, call_id='c')
    finally:
        other.execute('ROLLBACK')
        other.close()
    assert kb.fact(entity)['assertion']['id']             # free again: the write goes through


def test_errors_and_events_carry_no_text(kb):
    entity = kb.entity(f'{SENTINEL} 과제')
    text = f'{SENTINEL} 합성 문장'
    for code, fields in (('evidence_quote_not_found', {'sources': [{'chunk_id': kb.chunk('notes.txt', 1),
                                                                    'quote': text}]}),
                         ('argument_invalid', {'statement': text + '\x01'}),
                         ('argument_invalid', {'statement': text * 100})):
        refused(kb, code, knowledge.record, **record_args('decision', **{'statement': '합성', **fields}))
    fact = kb.fact(entity, text, sources=[{'locator': kb.loc('notes.txt', 5), 'quote': SENTINEL}])['assertion']
    kb.retract(fact['id'], reason=text)
    for row in kb.db.execute('SELECT kind, related_id, recorded_by, call_id FROM assertion_events'):
        assert SENTINEL not in json.dumps(row, ensure_ascii=False)
    assert kb.db.execute('SELECT reason FROM assertion_events WHERE kind=?', ('retracted',)).fetchone()[0] == text

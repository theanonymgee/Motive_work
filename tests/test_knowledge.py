"""Knowledge write path (design M4 §2-§4, §12 ③④): the error class and normalisation helpers, source
binding by quote, recording, retract, supersede (with a cut period), conflicts, tasks, the busy store, and
the import cycle with ``knowledge_query``.

Every write in these tests goes through ``KB.write``, which checks the append-only rule around it (design
M4 §13.4): no row of the knowledge tables disappears, row counts only grow, a row's content never changes
(only the transitions the triggers allow: close once, conflict mark/clear, a link resolved once, aliases
added and a first note), a refused write leaves every row as it was, and every evidence quote the write
stored is the stored unit text at its locator. Synthetic text only."""
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


def check_evidence(db, old_ids=()) -> None:
    """Every evidence quote stored after ``old_ids`` is the stored unit text at its locator (older rows may
    differ after ``scan --reparse``); an evidence row has one or more, a note none."""
    for evidence_id, assertion_id, version_id, locator_json, quote, quote_sha in db.execute(
            'SELECT id, assertion_id, version_id, locator_json, quote, quote_sha256 FROM evidence'):
        if evidence_id in old_ids:
            continue
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
        check_evidence(self.db, set(before['evidence']))
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
    assert kb.events(old['id'])[-1][:3] == ('superseded', '합성 교정', new['id'])
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


# ---- review M4 v1: M1 (reparse in place), M2 (history graph), L3, L8 ----------------------------------

QUOTE = '계약 상태는 검토 중'


def reparse(kb, monkeypatch, change) -> None:
    """A new txt parser version whose output changes line 1 by ``change``, then ``scan --reparse``: the
    units of the same version are refilled in place (the reviewer's reproduction, ``collect.record``)."""
    import dataclasses
    from workmcp.parsers import text as text_parser
    real = text_parser.parse

    def parse(data, limits, budget):
        units, encoding = real(data, limits, budget)
        return [dataclasses.replace(u, text=change(u.text)) if u.unit.get('line') == 1 else u for u in units], encoding
    monkeypatch.setattr(text_parser, 'parse', parse)
    monkeypatch.setattr(text_parser, 'VERSION', 'text-reparse-test')
    versions = kb.count('versions')
    assert scan(kb.db, kb.root, reparse=True)['total']['reparsed'] == 2
    assert kb.count('versions') == versions                   # same versions, units refilled


def test_reparse_in_place_shows_the_source_as_changed_with_a_requote(kb, monkeypatch):
    entity = kb.entity()
    fact = kb.fact(entity, '검토 중', sources=[{'locator': kb.loc('notes.txt', 1), 'quote': QUOTE}])['assertion']
    assert fact['evidence'][0]['source_state'] == 'current'
    reparse(kb, monkeypatch, lambda text: '재파싱된 ' + text)
    row = knowledge_query.assertion_row(kb.db, fact['id'], with_evidence=True)
    assert row['evidence'][0]['source_state'] == row['source_state'] == 'changed'   # same version, other text
    detail = knowledge_query.entity_detail(kb.db, entity, as_of_valid=None, as_of_recorded=None, include_closed=False,
                                           source_kind='all', limit=10)
    [evidence] = detail['facts'][0]['evidence']
    moved = len('재파싱된 ')
    assert evidence['source_state'] == 'changed' and evidence['requote']['found'] is True
    assert evidence['requote']['locator']['chars'] == [c + moved for c in fact['evidence'][0]['locator']['chars']]
    assert binding.validate_locator(kb.db, evidence['requote']['locator'])['text'] == QUOTE
    assert knowledge_query.SOURCE_NOTICE.format(1) in detail['notice']
    history = knowledge_query.history(kb.db, fact['id'])
    assert history['chain'][0]['evidence'][0]['requote']['found'] is True
    listed = knowledge_query.list_assertions(kb.db, {'entity_id': entity}, 10, 0)
    assert listed['items'][0]['source_state'] == 'changed'


def forge_evidence(kb, recorded_from: str, quote: str, quote_sha: str | None = None) -> int:
    """A source written with SQL (no writer does this): a decision citing line 1 of notes.txt with ``quote``."""
    assertion = kb.db.execute(
        "INSERT INTO assertions(kind, statement, recorded_from, status, source_kind, recorded_by) "
        "VALUES('decision', '합성 위조', ?, 'active', 'evidence', 'forger')", (recorded_from,)).lastrowid
    obj = kb.loc('notes.txt', 1)
    version = kb.units('notes.txt')[0][3]
    kb.db.execute('INSERT INTO evidence(assertion_id, version_id, locator_json, quote, quote_sha256) VALUES(?,?,?,?,?)',
                  (assertion, version, json.dumps(obj, ensure_ascii=False), quote,
                   quote_sha or hashlib.sha256(quote.encode()).hexdigest()))
    return assertion


def test_verify_counts_sources_stale_after_a_reparse_apart_and_still_fails_forged_ones(kb, monkeypatch):
    """Review M4 v2 N2: a source recorded before its version was reparsed is a warning, not a failure (the
    unit text it cited is gone by design); a forged or damaged source still fails ``verify``."""
    from workmcp import cli
    entity = kb.entity()
    kb.fact(entity, '검토 중', sources=[{'locator': kb.loc('notes.txt', 1), 'quote': QUOTE}])
    assert binding.verify(kb.db)['ok']
    reparse(kb, monkeypatch, lambda text: '재파싱된 ' + text)
    report = binding.verify(kb.db)
    assert report['ok'] and (report['evidence_checked'], report['evidence_stale_after_reparse']) == (1, 1)
    assert binding.REPARSE_STALE_NOTICE.format(1) in report['notice']
    lines = cli.verify_lines(report)
    assert any(line.endswith('evidence 1 (stale after reparse 1)') for line in lines) and lines[-1] == 'binding ok'
    kb.fact(entity, '승인', predicate='단계', sources=[{'locator': kb.loc('notes.txt', 1), 'quote': QUOTE}])
    assert binding.verify(kb.db)['evidence_stale_after_reparse'] == 1        # a source bound after it matches
    forge_evidence(kb, '2999-01-01T00:00:00+09:00', '합성 과제 31의')          # recorded after the reparse
    later = binding.verify(kb.db)
    assert not later['ok'] and [f['problem'] for f in later['failures']] == ['evidence:evidence_quote_mismatch']
    assert later['evidence_stale_after_reparse'] == 1


def test_verify_still_fails_a_damaged_source_of_a_reparsed_version(kb, monkeypatch):
    entity = kb.entity()
    kb.fact(entity, '검토 중', sources=[{'locator': kb.loc('notes.txt', 1), 'quote': QUOTE}])
    reparse(kb, monkeypatch, lambda text: '재파싱된 ' + text)
    forge_evidence(kb, '2000-01-01T00:00:00+09:00', QUOTE, quote_sha='0' * 64)   # before the reparse, bad hash
    report = binding.verify(kb.db)
    assert not report['ok'] and [f['problem'] for f in report['failures']] == ['evidence:evidence_quote_sha256_mismatch']
    assert report['evidence_stale_after_reparse'] == 1


def test_cut_supersede_rebinds_a_reparsed_source_found_once(kb, monkeypatch):
    entity = kb.entity()
    old = kb.fact(entity, '검토 중', valid_from='2026-10-01',
                  sources=[{'locator': kb.loc('notes.txt', 1), 'quote': QUOTE}])['assertion']
    reparse(kb, monkeypatch, lambda text: '재파싱된 ' + text)
    out = kb.supersede(old['id'], value='승인', old_valid_to='2026-10-20')   # the ledger re-checks the copy
    [evidence] = out['cut']['evidence']
    assert evidence['quote'] == QUOTE and evidence['source_state'] == 'current'
    assert evidence['locator']['chars'] == [c + len('재파싱된 ') for c in old['evidence'][0]['locator']['chars']]
    assert knowledge.REBOUND_NOTICE.format(1) in out['notice']


@pytest.mark.parametrize('change', [lambda text: '완전히 다른 합성 문장', lambda text: f'{QUOTE} {QUOTE}'],
                         ids=['gone', 'twice'])
def test_cut_supersede_of_a_source_not_found_once_is_evidence_stale(kb, monkeypatch, change):
    entity = kb.entity()
    old = kb.fact(entity, '검토 중', valid_from='2026-10-01',
                  sources=[{'locator': kb.loc('notes.txt', 1), 'quote': QUOTE}])['assertion']
    reparse(kb, monkeypatch, change)
    error = refused(kb, 'evidence_stale', knowledge.supersede, old['id'], reason='합성', recorded_by=BY, call_id='c',
                    my_note=True, value='승인', old_valid_to='2026-10-20')
    assert knowledge.STALE_HINT in error.message and QUOTE not in error.message
    assert kb.row(old['id'])['recorded_to'] is None           # nothing written
    plain = kb.supersede(old['id'], value='승인')               # without a cut nothing is copied
    assert plain['new']['supersedes_id'] == old['id']


def test_superseding_the_open_cut_row_again_branches_the_history(kb):
    entity = kb.entity()
    old = kb.fact(entity, '검토 중', valid_from='2026-10-01')['assertion']
    first = kb.supersede(old['id'], value='승인', old_valid_to='2026-10-20')
    cut, new = first['cut']['id'], first['new']['id']
    again = kb.supersede(cut, reason='옛 기간 교정', value='보류')    # the earlier period corrected
    fixed = again['new']['id']
    assert again['new']['supersedes_id'] == cut and again['new']['valid_to'] == '2026-10-20'
    for start in (old['id'], cut, new, fixed):
        history = knowledge_query.history(kb.db, start)
        assert [row['id'] for row in history['chain']] == [old['id'], cut, new, fixed]
        assert history['branches'] == [{'id': cut, 'children': [new, fixed]}]
        assert history['notice'][0] == knowledge_query.HISTORY_BRANCH_NOTICE.format(cut)
    rows = {row['id']: row for row in knowledge_query.history(kb.db, new)['chain']}
    assert {i: (r['status'], r['superseded_by_id'], r['continued_by_ids']) for i, r in rows.items()} == {
        old['id']: ('superseded', new, [cut]), cut: ('superseded', fixed, [new]), new: ('active', None, []),
        fixed: ('active', None, [])}
    assert kb.events(cut)[-1][:3] == ('superseded', '옛 기간 교정', fixed)
    values = lambda day: [(r['id'], r['data']['value']) for r in knowledge_query.list_assertions(  # noqa: E731
        kb.db, {'entity_id': entity, 'as_of_valid': day}, 10, 0)['items']]
    assert values('2026-10-10') == [(fixed, '보류')] and values('2026-10-25') == [(new, '승인')]


def test_a_history_larger_than_the_walk_keeps_the_rows_nearest_the_asked_one(kb, monkeypatch):
    """Review M4 v2 L-b, with the walk limit made small: a complete graph shows its latest rows; a graph
    the walk could not finish shows the rows nearest the asked one (it included) and says so exactly."""
    monkeypatch.setattr(knowledge_query, 'HISTORY_GRAPH_MAX', 60)
    entity = kb.entity()
    ids = [kb.fact(entity, '값 0')['assertion']['id']]
    for n in range(1, 60):                                          # 60 rows: the walk sees all of them
        ids.append(knowledge.supersede(kb.db, ids[-1], reason='합성', recorded_by=BY, call_id='c', my_note=True,
                                       value=f'값 {n}')['new']['id'])
    whole = knowledge_query.history(kb.db, ids[0])
    assert whole['truncated'] and [r['id'] for r in whole['chain']] == ids[-50:]
    assert whole['notice'][0] == knowledge_query.HISTORY_TRUNCATED_NOTICE
    for n in range(60, 70):                                         # 70 rows: no longer
        ids.append(knowledge.supersede(kb.db, ids[-1], reason='합성', recorded_by=BY, call_id='c', my_note=True,
                                       value=f'값 {n}')['new']['id'])
    for asked, expected in ((ids[0], ids[:50]), (ids[-1], ids[-50:]), (ids[35], ids[10:60])):
        part = knowledge_query.history(kb.db, asked)
        assert part['truncated'] and [r['id'] for r in part['chain']] == expected, asked
        assert part['notice'][0] == knowledge_query.HISTORY_PARTIAL_NOTICE.format(asked=asked, walked=60, kept=50)


def test_conflict_lists_are_capped_with_a_count_of_the_rest(kb):
    entity = kb.entity()
    first = kb.fact(entity, '값 0')['assertion']
    for n in range(1, knowledge_query.CONFLICTS_MAX + 1):
        kb.fact(entity, f'값 {n}')
    out = kb.fact(entity, '값 마지막')
    limit = knowledge_query.CONFLICTS_MAX
    assert (len(out['conflicts']), out['conflicts_more']) == (limit, 1)
    assert (len(out['assertion']['conflicts']), out['assertion']['conflicts_more']) == (limit, 1)
    assert any('외 1건' in line for line in out['notice'])
    row = knowledge_query.assertion_row(kb.db, first['id'], with_evidence=False)
    assert (len(row['conflicts']), row['conflicts_more']) == (limit, 1)


def test_a_failed_answer_after_the_commit_names_the_stored_ids(kb, monkeypatch):
    entity = kb.entity()

    def broken(*args, **kwargs):
        raise RuntimeError('합성 고장')
    monkeypatch.setattr(knowledge_query, 'assertion_row', broken)
    with pytest.raises(knowledge.KnowledgeError) as caught:
        knowledge.record(kb.db, 'fact', recorded_by=BY, call_id='c', entity_id=entity, predicate='p', value='v',
                         my_note=True)
    stored = kb.db.execute('SELECT MAX(id) FROM assertions').fetchone()[0]
    assert caught.value.code == 'internal_error' and f'stored (id {stored})' in caught.value.message
    assert 'RuntimeError' in caught.value.message and '합성 고장' not in caught.value.message
    assert 'do not repeat' in caught.value.message and not kb.db.in_transaction


# ---- review M4 v2 N1: source checks on a large document stay indexed --------------------------------

def test_reads_with_many_sources_in_a_large_document_stay_fast(tmp_path):
    """The reviewer's probe made smaller (40,000 units, 150 sources): every read re-checks each source
    against its stored unit, which is one indexed lookup (migration v4) instead of a scan of the version."""
    import time
    lines = [f'합성 대형 문서 {n}번째 줄' for n in range(1, 40_001)]
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    (tmp_path/'src').mkdir()
    (tmp_path/'src'/'big.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    db = store.open_home(home)
    try:
        scan(db, add_root(db, tmp_path/'src'))
        version, doc, path, sha = db.execute('SELECT v.id, d.id, d.path, v.sha256 FROM versions v JOIN documents d '
                                             'ON d.id = v.document_id').fetchone()
        units = {o: (json.loads(u), t) for o, u, t in db.execute(
            'SELECT ordinal, locator_json, text FROM units WHERE version_id=?', (version,))}
        assert len(units) == 40_000
        entity = knowledge.record_entity(db, kind='project', name='합성 대형', recorded_by=BY)['entity']['id']
        for n in range(50):
            sources = []
            for k in range(3):
                unit, text = units[40_000 - (n * 3 + k) * 97]
                sources.append({'locator': locator.make(doc, path, sha, unit, text, [0, len(text)]), 'quote': text})
            knowledge.record(db, 'fact', recorded_by=BY, call_id='c', entity_id=entity, predicate=f'속성 {n}',
                             value='합성', sources=sources)
        assert db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] == 150
        started = time.perf_counter()
        listed = knowledge_query.list_assertions(db, {'entity_id': entity}, 50, 0)
        detail = knowledge_query.entity_detail(db, entity, as_of_valid=None, as_of_recorded=None,
                                               include_closed=False, source_kind='all', limit=50)
        elapsed = time.perf_counter() - started
        assert len(listed['items']) == 50 and len(detail['facts']) == 50
        assert all(row['source_state'] == 'current' for row in listed['items'])
        assert elapsed < 1.0, elapsed       # indexed: well under 0.1 s here; without the index 1.9 s
    finally:
        db.close()

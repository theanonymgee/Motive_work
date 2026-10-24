"""Knowledge read path (design M4 §3.4, §5, §6.3, §12): one assertion and one entity representation, the two
time axes, entity search and detail, lists, history, and the state of stored sources.

Rows are put in with SQL that keeps the v1/v3 triggers (insert; close once; mark or clear a conflict;
resolve a link once), so these tests do not depend on the writers in ``knowledge``. The source-state
transitions also run through real scans of synthetic text files. Synthetic text only.
"""
import json

import pytest

from fixtures import SENTINEL
from workmcp import knowledge, knowledge_query as kq, locator, parsers, paths, store

T1 = '2026-10-01T09:00:00+09:00'
T2 = '2026-10-02T09:00:00+09:00'
T3 = '2026-10-03T09:00:00+09:00'
T4 = '2026-10-04T09:00:00+09:00'
T5 = '2026-10-05T09:00:00+09:00'
BY = 'test-client/record_fact'


@pytest.fixture
def db(workmcp_home):
    home = paths.ensure_home(paths.resolve_home())
    connection = store.open_home(home)
    yield connection
    connection.close()


# ---- rows through SQL that the triggers allow ----------------------------------------------------------

def add_entity(db, kind='project', name='합성 과제', aliases=(), note=None, at=T1):
    return db.execute('INSERT INTO entities(kind, name, aliases_json, note, created_at, created_by) '
                      'VALUES (?, ?, ?, ?, ?, ?)',
                      (kind, name, json.dumps(list(aliases), ensure_ascii=False), note, at,
                       'test-client/record_entity')).lastrowid


def add(db, kind='fact', *, entity=None, predicate=None, value=None, unit=None, obj=None, statement='합성 문장',
        data=None, valid=(None, None), at=T1, source_kind='my_note', supersedes=None):
    if data is None:
        data = {'fact': {'value': value, 'unit': unit},
                'decision': {'options': [], 'chosen': None, 'rationale': None},
                'todo': {'state': 'open', 'due': None, 'owner_entity_id': None, 'done_at': None}}[kind]
    return db.execute(
        'INSERT INTO assertions(kind, entity_id, predicate, object_entity_id, statement, data_json, valid_from, '
        'valid_to, recorded_from, status, supersedes_id, source_kind, recorded_by) '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
        (kind, entity, predicate, obj, statement, json.dumps(data, ensure_ascii=False, sort_keys=True),
         valid[0], valid[1], at, supersedes, source_kind, BY)).lastrowid


def event(db, assertion, kind, at, related=None, reason=None):
    db.execute('INSERT INTO assertion_events(assertion_id, at, kind, reason, related_id, recorded_by, call_id) '
               'VALUES (?, ?, ?, ?, ?, ?, ?)', (assertion, at, kind, reason, related, BY, 'call-1'))


def close(db, assertion, status, at, reason='합성 사유'):
    db.execute('UPDATE assertions SET status=?, recorded_to=?, change_reason=? WHERE id=?',
               (status, at, reason, assertion))
    event(db, assertion, 'retracted' if status == 'withdrawn' else 'superseded', at, reason=reason)


def link(db, from_id, to_id, relation, at):
    return db.execute('INSERT INTO links(from_id, to_id, relation, recorded_at, recorded_by) VALUES (?, ?, ?, ?, ?)',
                      (from_id, to_id, relation, at, BY)).lastrowid


def mark_conflict(db, a, b, at):
    db.execute("UPDATE assertions SET status='conflict' WHERE id IN (?, ?) AND status='active'", (a, b))
    event(db, a, 'conflict_marked', at, related=b)
    event(db, b, 'conflict_marked', at, related=a)
    return link(db, a, b, 'conflicts', at)


def resolve(db, link_id, at, by_id, clear=()):
    db.execute('UPDATE links SET resolved_at=?, resolved_by_id=? WHERE id=?', (at, by_id, link_id))
    for other in clear:
        db.execute("UPDATE assertions SET status='active' WHERE id=? AND status='conflict'", (other,))
        event(db, other, 'conflict_cleared', at, related=by_id)


def supersede(db, old, new_kwargs, at, reason='합성 교정'):
    """New row superseding ``old`` (same time), old closed, link and events: as ``knowledge.supersede``."""
    new = add(db, supersedes=old, at=at, **new_kwargs)
    close(db, old, 'superseded', at, reason)
    link(db, new, old, 'supersedes', at)
    event(db, new, 'recorded', at, related=old)
    return new


def add_document(db, name='합성.txt', lines=('첫 줄 합성 문장', '둘째 줄 합성 인용 문장'), status='active'):
    """A document with one ok version of line units; returns ``(doc_id, version_id)``."""
    root = db.execute('SELECT id FROM source_roots').fetchone()
    root_id = root[0] if root else db.execute("INSERT INTO source_roots(path, added_at) VALUES ('C:\\합성', ?)",
                                              (T1,)).lastrowid
    path = f'C:\\합성\\{name}'
    doc = db.execute('INSERT INTO documents(root_id, path, path_key, ext, status, first_seen_at, last_seen_at) '
                     'VALUES (?, ?, ?, ?, ?, ?, ?)', (root_id, path, path.lower(), '.txt', status, T1, T1)).lastrowid
    return doc, add_version(db, doc, lines)


def add_version(db, doc, lines):
    seq = db.execute('SELECT COALESCE(MAX(seq), 0) + 1 FROM versions WHERE document_id=?', (doc,)).fetchone()[0]
    db.execute('UPDATE versions SET superseded_at=? WHERE document_id=? AND superseded_at IS NULL', (T2, doc))
    sha = locator.text_sha256('\n'.join(lines) + str(seq))
    version = db.execute("INSERT INTO versions(document_id, seq, sha256, size, mtime_ns, observed_at, parse_status) "
                         "VALUES (?, ?, ?, 1, 1, ?, 'ok')", (doc, seq, sha, T1)).lastrowid
    for number, text in enumerate(lines, 1):
        db.execute('INSERT INTO units(version_id, ordinal, kind, locator_json, text, text_sha256) '
                   "VALUES (?, ?, 'line', ?, ?, ?)",
                   (version, number, parsers.unit_json({'kind': 'line', 'line': number}), text,
                    locator.text_sha256(text)))
    return version


def add_evidence(db, assertion, version, line, quote):
    doc, sha, path = db.execute('SELECT d.id, v.sha256, d.path FROM versions v JOIN documents d ON '
                                'd.id = v.document_id WHERE v.id=?', (version,)).fetchone()
    text = db.execute('SELECT text FROM units WHERE version_id=? AND ordinal=?', (version, line)).fetchone()[0]
    start = text.index(quote)
    obj = locator.make(doc, path, sha, {'kind': 'line', 'line': line}, text, [start, start + len(quote)])
    db.execute('INSERT INTO evidence(assertion_id, version_id, chunk_id, locator_json, quote, quote_sha256) '
               'VALUES (?, ?, NULL, ?, ?, ?)', (assertion, version, json.dumps(obj, ensure_ascii=False), quote,
                                                 locator.text_sha256(quote)))
    return obj


def ids(rows):
    return [row['id'] for row in rows]


# ---- assertion_row / entity_row (the one representation; the writers use them too) --------------------

def test_assertion_row_has_the_fixed_keys_names_and_data(db):
    customer = add_entity(db, 'customer', '합성 고객')
    person = add_entity(db, 'person', '합성 사람')
    fact = add(db, entity=customer, predicate='담당자', obj=person, statement='합성 고객 담당자 합성 사람',
               valid=('2026-09-01', None))
    row = kq.assertion_row(db, fact, with_evidence=True)
    assert set(row) == kq.ASSERTION_KEYS
    assert row['entity'] == {'kind': 'customer', 'name': '합성 고객'} and set(row['entity']) == kq.ENTITY_REF_KEYS
    assert row['object'] == {'kind': 'person', 'name': '합성 사람'}
    assert (row['kind'], row['predicate'], row['object_entity_id'], row['valid_from'], row['valid_to']) == (
        'fact', '담당자', person, '2026-09-01', None)
    assert row['data'] == {'value': None, 'unit': None} and row['recorded_from'] == T1 and row['recorded_by'] == BY
    assert (row['status'], row['status_as_of'], row['recorded_to']) == ('active', 'open', None)
    assert (row['source_kind'], row['source_label'], row['source_state']) == ('my_note', '내 메모', None)
    assert (row['evidence'], row['evidence_count'], row['conflicts']) == ([], 0, [])
    assert (row['supersedes_id'], row['superseded_by_id']) == (None, None)
    decision = add(db, 'decision', statement='합성 결정', data={'options': ['A', 'B'], 'chosen': 'A', 'rationale': None})
    row = kq.assertion_row(db, decision, with_evidence=False)
    assert (row['entity'], row['object'], row['predicate'], row['evidence']) == (None, None, None, None)
    assert row['data'] == {'options': ['A', 'B'], 'chosen': 'A', 'rationale': None}


def test_assertion_row_evidence_carries_locator_quote_and_state(db):
    entity = add_entity(db)
    _, version = add_document(db)
    fact = add(db, entity=entity, predicate='상태', value='합성 값', source_kind='evidence')
    first = add_evidence(db, fact, version, 2, '합성 인용')
    second = add_evidence(db, fact, version, 1, '첫 줄')
    row = kq.assertion_row(db, fact, with_evidence=True)
    assert (row['source_label'], row['source_state'], row['evidence_count']) == ('근거 2건', 'current', 2)
    assert [item['locator'] for item in row['evidence']] == [first, second]
    assert all(set(item) == kq.EVIDENCE_KEYS for item in row['evidence'])
    assert [(item['quote'], item['quote_chars'], item['source_state'], item['requote']) for item in row['evidence']] \
        == [('합성 인용', 5, 'current', None), ('첫 줄', 3, 'current', None)]
    listed = kq.assertion_row(db, fact, with_evidence=False)
    assert (listed['evidence'], listed['evidence_count'], listed['source_state']) == (None, 2, 'current')


@pytest.mark.parametrize('change, state', [
    ('none', 'current'), ('new_version', 'changed'), ('deleted', 'deleted'), ('skipped', 'skipped'),
    ('deleted_after_change', 'deleted'),
])
def test_source_state_of_a_stored_source(db, change, state):
    entity = add_entity(db)
    doc, version = add_document(db)
    fact = add(db, entity=entity, predicate='상태', value='합성 값', source_kind='evidence')
    add_evidence(db, fact, version, 2, '합성 인용')
    if change in ('new_version', 'deleted_after_change'):
        add_version(db, doc, ('바뀐 첫 줄', '둘째 줄 합성 인용 문장'))
    if change in ('deleted', 'deleted_after_change'):
        db.execute("UPDATE documents SET status='deleted', deleted_at=? WHERE id=?", (T3, doc))
    if change == 'skipped':
        db.execute("UPDATE documents SET status='skipped', skip_reason='cloud_placeholder' WHERE id=?", (doc,))
    row = kq.assertion_row(db, fact, with_evidence=True)
    assert row['source_state'] == row['evidence'][0]['source_state'] == state
    assert row['evidence'][0]['requote'] is None   # write responses: no suggestion


def test_source_state_of_a_row_is_its_worst_source(db):
    entity = add_entity(db)
    doc_a, version_a = add_document(db, 'a.txt')
    doc_b, version_b = add_document(db, 'b.txt')
    _, version_c = add_document(db, 'c.txt')
    fact = add(db, entity=entity, predicate='상태', value='합성 값', source_kind='evidence')
    for version in (version_a, version_b, version_c):
        add_evidence(db, fact, version, 1, '첫 줄')
    assert kq.assertion_row(db, fact, with_evidence=False)['source_state'] == 'current'
    db.execute("UPDATE documents SET status='skipped' WHERE id=?", (doc_b,))
    assert kq.assertion_row(db, fact, with_evidence=False)['source_state'] == 'skipped'
    add_version(db, doc_a, ('첫 줄 바뀜',))
    assert kq.assertion_row(db, fact, with_evidence=False)['source_state'] == 'changed'
    db.execute("UPDATE documents SET status='deleted' WHERE id=?", (doc_a,))
    assert kq.assertion_row(db, fact, with_evidence=False)['source_state'] == 'deleted'
    assert kq.worst_state([]) is None and kq.SOURCE_STATE_ORDER[-1] == 'current'


def test_assertion_row_status_and_conflicts_at_a_recorded_time(db):
    entity = add_entity(db)
    a = add(db, entity=entity, predicate='금액', value='합성 1', at=T1, valid=('2026-09-01', None))
    b = add(db, entity=entity, predicate='금액', value='합성 2', at=T2)
    conflict = mark_conflict(db, b, a, T2)
    now_a = kq.assertion_row(db, a, with_evidence=False)
    assert (now_a['status'], now_a['status_as_of']) == ('conflict', 'open')
    assert now_a['conflicts'] == [{'id': b, 'value': '합성 2', 'valid_from': None, 'valid_to': None,
                                   'recorded_from': T2}]
    assert set(now_a['conflicts'][0]) == kq.CONFLICT_KEYS
    assert kq.assertion_row(db, a, with_evidence=False, as_of_recorded=T1)['conflicts'] == []   # B not yet
    close(db, b, 'withdrawn', T3)
    resolve(db, conflict, T3, b, clear=[a])
    assert kq.assertion_row(db, a, with_evidence=False)['conflicts'] == []
    then = kq.assertion_row(db, a, with_evidence=False, as_of_recorded='2026-10-02T12:00:00+09:00')
    assert [item['id'] for item in then['conflicts']] == [b]
    closed = kq.assertion_row(db, b, with_evidence=False)
    assert (closed['status'], closed['status_as_of'], closed['recorded_to'], closed['conflicts']) == (
        'withdrawn', 'withdrawn', T3, [])
    before = kq.assertion_row(db, b, with_evidence=False, as_of_recorded='2026-10-02T12:00:00+09:00')
    assert (before['status'], before['status_as_of'], [c['id'] for c in before['conflicts']]) == (
        'withdrawn', 'open', [a])
    at_close = kq.assertion_row(db, b, with_evidence=False, as_of_recorded=T3)   # [from, to): closed at T3
    assert at_close['status_as_of'] == 'withdrawn'


def test_assertion_row_links_the_supersede_chain_in_both_directions(db):
    entity = add_entity(db)
    old = add(db, entity=entity, predicate='금액', value='합성 1', at=T1)
    new = supersede(db, old, {'entity': entity, 'predicate': '금액', 'value': '합성 2'}, T3)
    row = kq.assertion_row(db, old, with_evidence=False)
    assert (row['superseded_by_id'], row['status'], row['status_as_of']) == (new, 'superseded', 'superseded')
    assert kq.assertion_row(db, new, with_evidence=False)['supersedes_id'] == old
    earlier = kq.assertion_row(db, old, with_evidence=False, as_of_recorded=T2)
    assert (earlier['superseded_by_id'], earlier['status_as_of']) == (None, 'open')


def test_assertion_row_time_accepts_dates_and_naive_times_and_refuses_others(db):
    entity = add_entity(db)
    fact = add(db, entity=entity, predicate='금액', value='합성 1', at=T1)
    close(db, fact, 'withdrawn', T3)
    assert kq.assertion_row(db, fact, with_evidence=False, as_of_recorded='2026-10-02')['status_as_of'] == 'open'
    assert kq.recorded_instant('2026-10-02T09:00:00+09:00') == '2026-10-02T09:00:00+09:00'
    assert kq.recorded_instant('2026-10-02T09:00:00').endswith(kq.recorded_instant('2026-10-02T09:00:00')[-6:])
    assert kq.recorded_instant(None) is None
    for bad in ('어제', '2026-13-01', '', 20261002, 'x' * 65):
        with pytest.raises(knowledge.KnowledgeError) as caught:
            kq.assertion_row(db, fact, with_evidence=False, as_of_recorded=bad)
        assert caught.value.code == 'argument_invalid' and 'as_of_recorded' in caught.value.message


def test_assertion_row_unknown_id_is_assertion_not_found(db):
    with pytest.raises(knowledge.KnowledgeError) as caught:
        kq.assertion_row(db, 999, with_evidence=True)
    assert caught.value.code == 'assertion_not_found' and caught.value.message == 'no assertion 999'


def test_entity_row_has_the_fixed_keys_aliases_and_open_counts(db):
    entity = add_entity(db, 'project', '합성 과제 31', aliases=['과제31', 'P31'], note='합성 메모')
    a = add(db, entity=entity, predicate='금액', value='합성 1')
    b = add(db, entity=entity, predicate='금액', value='합성 2')
    mark_conflict(db, a, b, T1)
    gone = add(db, entity=entity, predicate='단계', value='합성 단계')
    close(db, gone, 'withdrawn', T2)
    add(db, 'decision', entity=entity, statement='합성 결정')
    add(db, 'todo', entity=entity, statement='합성 할 일')
    add(db, 'todo', entity=entity, statement='합성 끝난 일',
        data={'state': 'done', 'due': None, 'owner_entity_id': None, 'done_at': '2026-10-02'})
    row = kq.entity_row(db, entity)
    assert set(row) == kq.ENTITY_KEYS and set(row['counts']) == kq.ENTITY_COUNT_KEYS
    assert (row['kind'], row['name'], row['aliases'], row['note'], row['merged_into']) == (
        'project', '합성 과제 31', ['과제31', 'P31'], '합성 메모', None)
    assert (row['created_at'], row['created_by']) == (T1, 'test-client/record_entity')
    assert row['counts'] == {'facts_open': 2, 'decisions_open': 1, 'tasks_open': 1, 'conflicts': 2}
    empty = kq.entity_row(db, add_entity(db, 'person', '합성 사람'))
    assert empty['counts'] == {'facts_open': 0, 'decisions_open': 0, 'tasks_open': 0, 'conflicts': 0}
    with pytest.raises(knowledge.KnowledgeError) as caught:
        kq.entity_row(db, 999)
    assert caught.value.code == 'entity_not_found'


def test_representation_never_puts_text_into_errors(db):
    """Error messages name ids and arguments only (design M4 §8.3)."""
    entity = add_entity(db, name=SENTINEL)
    add(db, entity=entity, predicate=SENTINEL, value=SENTINEL, statement=SENTINEL)
    for call in (lambda: kq.assertion_row(db, 999, with_evidence=True), lambda: kq.entity_row(db, 999),
                 lambda: kq.assertion_row(db, 1, with_evidence=True, as_of_recorded=SENTINEL)):
        with pytest.raises(knowledge.KnowledgeError) as caught:
            call()
        assert SENTINEL not in str(caught.value)

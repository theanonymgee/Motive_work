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


def close(db, assertion, status, at, reason='합성 사유', related=None):
    db.execute('UPDATE assertions SET status=?, recorded_to=?, change_reason=? WHERE id=?',
               (status, at, reason, assertion))
    event(db, assertion, 'retracted' if status == 'withdrawn' else 'superseded', at, related=related, reason=reason)


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
    close(db, old, 'superseded', at, reason, related=new)
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


def add_evidence(db, assertion, version, ordinal, quote):
    """An evidence row as ``knowledge.bind_sources`` stores it: unit ``ordinal`` of ``version``."""
    doc, sha, path = db.execute('SELECT d.id, v.sha256, d.path FROM versions v JOIN documents d ON '
                                'd.id = v.document_id WHERE v.id=?', (version,)).fetchone()
    unit_json, text = db.execute('SELECT locator_json, text FROM units WHERE version_id=? AND ordinal=?',
                                 (version, ordinal)).fetchone()
    start = text.index(quote)
    obj = locator.make(doc, path, sha, json.loads(unit_json), text, [start, start + len(quote)])
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


# ---- visible_sql: the two time axes as SQL (design M4 §5.1) -----------------------------------------------

def visible(db, valid_at=None, recorded_at=None, include_closed=False, where='1'):
    sql, params = kq.visible_sql(valid_at, recorded_at, include_closed)
    return [row[0] for row in db.execute(f'SELECT a.id FROM assertions a WHERE ({sql}) AND {where} ORDER BY a.id',
                                         params)]


def test_visible_sql_is_a_bare_expression_with_its_parameters():
    assert kq.visible_sql(None, None, False) == ('a.recorded_to IS NULL', [])
    assert kq.visible_sql(None, None, True) == ('1', [])
    sql, params = kq.visible_sql('2026-10-01', '2026-10-02T09:00:00+09:00', False)
    assert not sql.lstrip().upper().startswith('AND') and sql.count('?') == len(params) == 4
    assert params == ['2026-10-02T09:00:00+09:00'] * 2 + ['2026-10-01'] * 2
    for bad in (('2026-10-1', None, False), ('2026-10-01', 'nope', False)):
        with pytest.raises(knowledge.KnowledgeError) as caught:
            kq.visible_sql(*bad)
        assert caught.value.code == 'argument_invalid'


def retract_scenario(db):
    """B recorded at T2 and retracted at T4; A recorded at T1 and still open."""
    entity = add_entity(db, name='합성 철회 과제')
    a = add(db, entity=entity, predicate='금액', value='합성 1', at=T1)
    b = add(db, entity=entity, predicate='단계', value='합성 2', at=T2)
    close(db, b, 'withdrawn', T4)
    return a, b


RECORDED_CASES = [  # (as_of_recorded, include_closed, expected names) for retract_scenario
    ('2026-09-30T09:00:00+09:00', False, []),                 # before anything was recorded
    ('2026-10-01T08:59:59+09:00', False, []),                 # a second before A
    (T1, False, ['a']),                                       # recorded_from is inclusive
    ('2026-10-02T08:59:59+09:00', False, ['a']),              # before B
    (T2, False, ['a', 'b']),
    (T3, False, ['a', 'b']),                                  # between record and retract
    ('2026-10-04T08:59:59+09:00', False, ['a', 'b']),         # a second before the retraction
    (T4, False, ['a']),                                       # recorded_to is exclusive: retracted at T4
    (T5, False, ['a']),                                       # after the retraction
    (None, False, ['a']),                                     # now
    (T1, True, ['a']),                                        # include_closed: recorded by then
    (T3, True, ['a', 'b']),
    (T5, True, ['a', 'b']),
    (None, True, ['a', 'b']),
    ('2026-10-04T00:00:00Z', False, ['a']),                   # another offset: 09:00+09:00 == 00:00Z
    ('2026-10-03T23:59:59Z', False, ['a', 'b']),
]


def recorded_mismatches(db) -> list:
    a, b = retract_scenario(db)
    names = {a: 'a', b: 'b'}
    return [(at, closed, got) for at, closed, expected in RECORDED_CASES
            if (got := [names[i] for i in visible(db, recorded_at=at, include_closed=closed) if i in names]) != expected]


def test_as_of_recorded_replays_what_was_known_before_between_and_after_a_retraction(db):
    assert recorded_mismatches(db) == []


def cut_chain(db):
    """A (valid from 09-01) recorded at T1, superseded at T3 with old_valid_to 10-20: the cut row C (old value,
    09-01..10-20) and the new row N (from 10-20), both open; N supersedes C, C supersedes A (design §4.3)."""
    entity = add_entity(db, name='합성 대체 과제')
    a = add(db, entity=entity, predicate='금액', value='옛 값', at=T1, valid=('2026-09-01', None))
    c = add(db, entity=entity, predicate='금액', value='옛 값', at=T3, valid=('2026-09-01', '2026-10-20'),
            supersedes=a)
    n = add(db, entity=entity, predicate='금액', value='새 값', at=T3, valid=('2026-10-20', None), supersedes=c)
    close(db, a, 'superseded', T3, related=n)              # the old row's event names N (knowledge.supersede)
    link(db, c, a, 'supersedes', T3)
    link(db, n, c, 'supersedes', T3)
    for row, related in ((c, a), (n, c)):
        event(db, row, 'recorded', T3, related=related)
    return entity, a, c, n


VALID_CASES = [  # (as_of_valid, as_of_recorded, expected names) for cut_chain
    (None, None, ['c', 'n']),                    # every period, now
    ('2026-08-31', None, []),                    # before valid_from
    ('2026-09-01', None, ['c']),                 # valid_from is inclusive
    ('2026-10-10', None, ['c']),
    ('2026-10-19', None, ['c']),
    ('2026-10-20', None, ['n']),                 # valid_to is exclusive; N starts there
    ('2099-01-01', None, ['n']),                 # open end
    ('2026-10-25', T2, ['a']),                   # what we knew at T2 (before the supersede)
    ('2026-08-31', T2, []),
    ('2026-10-10', T3, ['c']),                   # at the supersede: A closed, C and N recorded
    ('2026-10-25', T3, ['n']),
    (None, '2026-10-03T08:59:59+09:00', ['a']),  # a second before the supersede
    (None, T3, ['c', 'n']),
]


def valid_mismatches(db) -> list:
    _, a, c, n = cut_chain(db)
    names = {a: 'a', c: 'c', n: 'n'}
    return [(on, at, got) for on, at, expected in VALID_CASES
            if (got := [names[i] for i in visible(db, valid_at=on, recorded_at=at) if i in names]) != expected]


def test_as_of_valid_selects_the_period_of_a_cut_chain_and_combines_with_as_of_recorded(db):
    assert valid_mismatches(db) == []


def test_rows_without_a_period_are_valid_at_every_date(db):
    entity = add_entity(db)
    always = add(db, entity=entity, predicate='정의', value='합성 정의')
    until = add(db, entity=entity, predicate='단계', value='합성 단계', valid=(None, '2026-10-01'))
    since = add(db, entity=entity, predicate='담당', value='합성 담당', valid=('2026-10-01', None))
    assert visible(db, valid_at='1900-01-01') == [always, until]
    assert visible(db, valid_at='2026-09-30') == [always, until]
    assert visible(db, valid_at='2026-10-01') == [always, since]
    assert visible(db, valid_at='9999-12-31') == [always, since]


def test_mutation_checks(db, monkeypatch):
    """The boundary tests catch a missing ``recorded_to`` condition, an inclusive ``recorded_to``, an
    inclusive ``valid_to`` and a missing ``valid_from`` condition (design M4 §12, M3 §9.2)."""
    real = kq.visible_sql
    mutants = {
        'no recorded_to': lambda sql: sql.replace('(a.recorded_to IS NULL OR julianday(?) < julianday(a.recorded_to))',
                                                  '(1 OR ? IS NULL)').replace('a.recorded_to IS NULL', '1'),
        'recorded_to inclusive': lambda sql: sql.replace('julianday(?) < julianday(a.recorded_to)',
                                                         'julianday(?) <= julianday(a.recorded_to)'),
        'valid_to inclusive': lambda sql: sql.replace('? < a.valid_to', '? <= a.valid_to'),
        'no valid_from': lambda sql: sql.replace('a.valid_from <= ?', '(1 OR ? IS NULL)'),
    }
    for name, mutate in mutants.items():
        monkeypatch.setattr(kq, 'visible_sql', lambda *args, mutate=mutate: (mutate(real(*args)[0]), real(*args)[1]))
        db.execute('BEGIN')
        caught = recorded_mismatches(db) + valid_mismatches(db)
        db.execute('ROLLBACK')
        assert caught, name
    monkeypatch.setattr(kq, 'visible_sql', real)
    assert recorded_mismatches(db) == [] and valid_mismatches(db) == []


# ---- find_entity / entity_by_name ----------------------------------------------------------------------

def test_find_entities_ranks_exact_name_then_exact_alias_then_partial_then_name(db):
    partial = add_entity(db, 'project', '합성 과제 P31 확장')
    alias = add_entity(db, 'project', '합성 과제 31', aliases=['P31', '삼일'])
    exact = add_entity(db, 'project', 'p31')
    person = add_entity(db, 'person', '합성 사람', aliases=['담당 p31 대리'])
    add_entity(db, 'customer', '무관한 고객')
    found = kq.find_entities(db, 'P31', None, 20)
    assert set(found) == kq.FIND_RESULT_KEYS | {'notice'} and found['notice'] == []
    assert ids(found['entities']) == [exact, alias, partial, person] and found['total'] == 4
    assert all(set(item) == kq.ENTITY_KEYS for item in found['entities'])
    assert ids(kq.find_entities(db, 'p31', 'project', 20)['entities']) == [exact, alias, partial]
    assert ids(kq.find_entities(db, '삼일', None, 20)['entities']) == [alias]
    page = kq.find_entities(db, 'p31', None, 2)
    assert (ids(page['entities']), page['total'], page['limit'], page['has_more']) == ([exact, alias], 4, 2, True)
    nfd = kq.find_entities(db, '합성', None, 20)   # '합성' decomposed
    assert set(ids(nfd['entities'])) == {partial, alias, person}
    empty = kq.find_entities(db, '없는 이름', None, 20)
    assert (empty['entities'], empty['total'], empty['has_more'], empty['notice']) == ([], 0, False,
                                                                                         [kq.FIND_EMPTY_NOTICE])
    for bad in (('', None), ('   ', None), ('x' * 201, None), (3, None), ('p31', 'team')):
        with pytest.raises(knowledge.KnowledgeError) as caught:
            kq.find_entities(db, bad[0], bad[1], 20)
        assert caught.value.code == 'argument_invalid'


def test_entity_by_name_is_exact_and_points_aliases_to_find_entity(db):
    entity = add_entity(db, 'project', '합성 과제', aliases=['과제A'])
    assert kq.entity_by_name(db, 'project', '  합성 과제 ') == entity
    assert kq.entity_by_name(db, 'project', '합성 과제') == entity   # NFD
    for kind, name, hint in (('project', '과제a', 'alias'), ('person', '합성 과제', 'find_entity searches'),
                             ('project', SENTINEL, 'find_entity searches')):
        with pytest.raises(knowledge.KnowledgeError) as caught:
            kq.entity_by_name(db, kind, name)
        assert caught.value.code == 'entity_not_found' and hint in caught.value.message
        assert SENTINEL not in caught.value.message and '과제' not in caught.value.message
    with pytest.raises(knowledge.KnowledgeError) as caught:
        kq.entity_by_name(db, 'team', '합성 과제')
    assert caught.value.code == 'argument_invalid'


# ---- get_entity ------------------------------------------------------------------------------------------

def test_entity_detail_lists_facts_related_decisions_tasks_with_quotes(db):
    customer = add_entity(db, 'customer', '합성 고객')
    person = add_entity(db, 'person', '합성 사람')
    _, version = add_document(db)
    budget = add(db, entity=customer, predicate='예산', value='3억', unit='원', valid=('2026-09-01', None),
                 source_kind='evidence', at=T1)
    add_evidence(db, budget, version, 2, '합성 인용')
    owner = add(db, entity=customer, predicate='담당자', obj=person, at=T2)
    early = add(db, entity=customer, predicate='예산', value='2억', unit='원', valid=(None, '2026-09-01'), at=T2)
    decision_1 = add(db, 'decision', entity=customer, statement='합성 결정 1', at=T1)
    decision_2 = add(db, 'decision', entity=customer, statement='합성 결정 2', at=T2)
    later = add(db, 'todo', entity=customer, statement='합성 할 일 늦음', at=T1,
                data={'state': 'open', 'due': '2026-12-01', 'owner_entity_id': person, 'done_at': None})
    undated = add(db, 'todo', entity=customer, statement='합성 할 일 기한 없음', at=T2)
    sooner = add(db, 'todo', entity=customer, statement='합성 할 일 이름', at=T3,
                 data={'state': 'open', 'due': '2026-11-01', 'owner_entity_id': None, 'done_at': None})
    add(db, entity=person, predicate='소속', value='합성 부서')   # another entity's fact
    detail = kq.entity_detail(db, customer, as_of_valid=None, as_of_recorded=None, include_closed=False,
                              source_kind='all', limit=50)
    assert set(detail) == kq.ENTITY_RESULT_KEYS | {'notice'} and detail['notice'] == []
    assert detail['entity'] == kq.entity_row(db, customer)
    assert ids(detail['facts']) == [owner, early, budget]       # by predicate, then period start (none first)
    assert ids(detail['decisions']) == [decision_2, decision_1]
    assert ids(detail['tasks']) == [sooner, later, undated]     # by due, none last
    assert ids(kq.entity_detail(db, person, as_of_valid=None, as_of_recorded=None, include_closed=False,
                                source_kind='all', limit=50)['related']) == [owner]
    assert detail['related'] == [] and detail['predicates_in_use'] == ['담당자', '예산']
    assert detail['has_more'] == dict.fromkeys(kq.ENTITY_LISTS, False)
    assert set(detail['as_of']) == kq.AS_OF_KEYS and detail['as_of']['valid'] is None
    quoted = next(row for row in detail['facts'] if row['id'] == budget)
    assert [item['quote'] for item in quoted['evidence']] == ['합성 인용']
    assert all(row['evidence'] == [] for row in detail['facts'] if row['id'] != budget)
    narrow = kq.entity_detail(db, customer, as_of_valid='2026-08-15', as_of_recorded=None, include_closed=False,
                              source_kind='evidence', limit=1)
    assert ids(narrow['facts']) == [] and narrow['predicates_in_use'] == [] and narrow['as_of']['valid'] == '2026-08-15'
    small = kq.entity_detail(db, customer, as_of_valid=None, as_of_recorded=None, include_closed=False,
                             source_kind='my_note', limit=1)
    assert ids(small['facts']) == [owner] and small['has_more'] == {'facts': True, 'related': False,
                                                                     'decisions': True, 'tasks': True}


def test_entity_detail_include_closed_and_past_recorded_time(db):
    entity, a, c, n = cut_chain(db)
    gone = add(db, entity=entity, predicate='단계', value='합성 단계', at=T2)
    close(db, gone, 'withdrawn', T4)
    def facts(**kwargs):
        args = {'as_of_valid': None, 'as_of_recorded': None, 'include_closed': False, 'source_kind': 'all',
                'limit': 50, **kwargs}
        return [(row['id'], row['status_as_of']) for row in kq.entity_detail(db, entity, **args)['facts']]
    assert facts() == [(c, 'open'), (n, 'open')]
    assert facts(include_closed=True) == [(a, 'superseded'), (c, 'open'), (n, 'open'), (gone, 'withdrawn')]
    assert facts(as_of_recorded=T2) == [(a, 'open'), (gone, 'open')]
    assert facts(as_of_recorded=T3, include_closed=True) == [(a, 'superseded'), (c, 'open'), (n, 'open'),
                                                             (gone, 'open')]
    detail = kq.entity_detail(db, entity, as_of_valid=None, as_of_recorded=T2, include_closed=False,
                              source_kind='all', limit=50)
    assert detail['as_of'] == {'valid': None, 'recorded': T2}
    with pytest.raises(knowledge.KnowledgeError) as caught:
        kq.entity_detail(db, 999, as_of_valid=None, as_of_recorded=None, include_closed=False, source_kind='all',
                         limit=50)
    assert caught.value.code == 'entity_not_found'
    with pytest.raises(knowledge.KnowledgeError):
        kq.entity_detail(db, entity, as_of_valid=None, as_of_recorded=None, include_closed=False,
                         source_kind='모두', limit=50)


def test_entity_detail_marks_conflicts_and_changed_sources_in_notice(db):
    entity = add_entity(db)
    doc, version = add_document(db)
    a = add(db, entity=entity, predicate='금액', value='합성 1', source_kind='evidence')
    add_evidence(db, a, version, 2, '합성 인용')
    b = add(db, entity=entity, predicate='금액', value='합성 2')
    mark_conflict(db, b, a, T1)
    add_version(db, doc, ('바뀐 첫 줄', '둘째 줄 합성 인용 문장'))
    detail = kq.entity_detail(db, entity, as_of_valid=None, as_of_recorded=None, include_closed=False,
                              source_kind='all', limit=50)
    assert detail['notice'] == [kq.SOURCE_NOTICE.format(1), kq.CONFLICT_NOTICE.format(2)]
    evidence = next(row for row in detail['facts'] if row['id'] == a)['evidence'][0]
    assert evidence['source_state'] == 'changed' and evidence['requote']['found'] is True


# ---- list_knowledge ----------------------------------------------------------------------------------------

def knowledge_rows(db):
    e1, e2 = add_entity(db, name='합성 과제 1'), add_entity(db, name='합성 과제 2')
    _, version = add_document(db)
    rows = {'f1': add(db, entity=e1, predicate='금액', value='합성 1', at=T1, source_kind='evidence'),
            'f2': add(db, entity=e1, predicate='금액', value='합성 2', at=T2),
            'f3': add(db, entity=e2, predicate='단계', value='합성 단계', at=T2, valid=('2026-11-01', None)),
            'd1': add(db, 'decision', entity=e1, statement='합성 결정', at=T3),
            't1': add(db, 'todo', entity=e1, statement='합성 할 일 1', at=T3,
                      data={'state': 'open', 'due': '2026-11-15', 'owner_entity_id': None, 'done_at': None}),
            't2': add(db, 'todo', statement='합성 할 일 2', at=T4,
                      data={'state': 'done', 'due': '2026-11-01', 'owner_entity_id': None, 'done_at': '2026-10-04'}),
            't3': add(db, 'todo', statement='합성 할 일 3', at=T4)}
    add_evidence(db, rows['f1'], version, 2, '합성 인용')
    link_id = mark_conflict(db, rows['f2'], rows['f1'], T2)
    close(db, rows['f2'], 'withdrawn', T4)
    resolve(db, link_id, T4, rows['f2'], clear=[rows['f1']])
    rows['e1'], rows['e2'] = e1, e2
    return rows


def listed(db, limit=50, offset=0, **filters):
    return kq.list_assertions(db, filters, limit, offset)


def names_of(rows, result):
    back = {value: key for key, value in rows.items() if key[0] in 'fdt'}
    return [back[item['id']] for item in result['items']]


@pytest.mark.parametrize('filters, expected', [
    ({}, ['t3', 't2', 't1', 'd1', 'f3', 'f1']),                       # open now, newest recorded first
    ({'kind': 'fact'}, ['f3', 'f1']),
    ({'kind': 'todo'}, ['t2', 't1', 't3']),                           # tasks by due, none last
    ({'kind': 'todo', 'state': 'open'}, ['t1', 't3']),
    ({'state': 'done'}, ['t2']),
    ({'due_before': '2026-11-15'}, ['t2']),                           # due < due_before
    ({'entity_id': 'e1'}, ['t1', 'd1', 'f1']),
    ({'predicate': ' 금액 '}, ['f1']),
    ({'status': 'closed'}, ['f2']),
    ({'status': 'all', 'kind': 'fact'}, ['f3', 'f2', 'f1']),
    ({'status': 'conflict'}, []),                                     # resolved at T4
    ({'status': 'conflict', 'as_of_recorded': T3}, ['f2', 'f1']),     # open conflict link then
    ({'status': 'closed', 'as_of_recorded': T3}, []),
    ({'status': 'closed', 'as_of_recorded': T4}, ['f2']),
    ({'as_of_recorded': T2}, ['f3', 'f2', 'f1']),
    ({'as_of_valid': '2026-10-15', 'kind': 'fact'}, ['f1']),
    ({'source_kind': 'evidence'}, ['f1']),
    ({'source_kind': 'my_note', 'kind': 'fact'}, ['f3']),
    ({'recorded_since': T3}, ['t3', 't2', 't1', 'd1']),
    ({'recorded_since': '2026-10-04'}, ['t3', 't2']),                 # a date: midnight local time
])
def test_list_assertions_filters(db, filters, expected):
    rows = knowledge_rows(db)
    if 'entity_id' in filters:
        filters = {**filters, 'entity_id': rows[filters['entity_id']]}
    result = listed(db, **filters)
    assert names_of(rows, result) == expected and result['total'] == len(expected)


def test_list_assertions_rows_pages_and_carries_no_quotes(db):
    rows = knowledge_rows(db)
    first = listed(db, limit=2)
    assert set(first) == kq.LIST_RESULT_KEYS | {'notice'}
    assert (names_of(rows, first), first['total'], first['limit'], first['offset'], first['has_more']) == (
        ['t3', 't2'], 6, 2, 0, True)
    last = listed(db, limit=2, offset=4)
    assert (names_of(rows, last), last['has_more']) == (['f3', 'f1'], False)
    beyond = listed(db, limit=2, offset=10)
    assert (beyond['items'], beyond['total'], beyond['has_more'], beyond['notice']) == ([], 6, False, [])
    f1 = next(item for item in listed(db, kind='fact')['items'] if item['id'] == rows['f1'])
    assert set(f1) == kq.ASSERTION_KEYS and (f1['evidence'], f1['evidence_count'], f1['source_state']) == (
        None, 1, 'current')
    assert '합성 인용' not in json.dumps(listed(db, status='all'), ensure_ascii=False)
    assert listed(db, kind='decision', entity_id=rows['e2'])['notice'] == [kq.LIST_EMPTY_NOTICE]
    then = listed(db, status='all', as_of_recorded=T3, kind='fact')
    assert [(item['id'], item['status_as_of'], item['status']) for item in then['items']] == [
        (rows['f3'], 'open', 'active'), (rows['f2'], 'open', 'withdrawn'), (rows['f1'], 'open', 'active')]
    assert then['notice'] == [kq.CONFLICT_NOTICE.format(2)] and then['as_of'] == {'valid': None, 'recorded': T3}


@pytest.mark.parametrize('filters', [
    {'kind': 'term'}, {'status': 'withdrawn'}, {'source_kind': 'none'}, {'state': 'late'}, {'due_before': '2026-1-1'},
    {'as_of_valid': '2026/10/01'}, {'as_of_recorded': '어제'}, {'recorded_since': 'x'}, {'predicate': ''},
    {'colour': 'red'},
])
def test_list_assertions_refuses_bad_filters(db, filters):
    with pytest.raises(knowledge.KnowledgeError) as caught:
        listed(db, **filters)
    assert caught.value.code == 'argument_invalid'


# ---- get_history -------------------------------------------------------------------------------------------

def test_history_chain_root_to_latest_with_events_links_and_evidence(db):
    entity, a, c, n = cut_chain(db)
    _, version = add_document(db)
    other = add(db, entity=entity, predicate='금액', value='합성 다른 값', at=T4, source_kind='evidence')
    add_evidence(db, other, version, 2, '합성 인용')
    conflict = mark_conflict(db, other, n, T4)
    close(db, other, 'withdrawn', T5, reason='합성 오기')
    resolve(db, conflict, T5, other, clear=[n])
    for start in (a, c, n):
        result = kq.history(db, start)
        assert set(result) == kq.HISTORY_RESULT_KEYS | {'notice'} and result['truncated'] is False
        assert ids(result['chain']) == [a, c, n]
    chain = kq.history(db, n)['chain']
    assert all(set(row) == kq.ASSERTION_KEYS | kq.HISTORY_ROW_EXTRA_KEYS for row in chain)
    assert [row['status'] for row in chain] == ['superseded', 'active', 'active']
    # the row that closed each one (review M4 v1 M2); the open cut row C is continued by N
    assert [row['superseded_by_id'] for row in chain] == [n, None, None]
    assert [row['continued_by_ids'] for row in chain] == [[c], [n], []]
    assert kq.history(db, n)['branches'] == []
    assert [[e['kind'] for e in row['events']] for row in chain] == [
        ['superseded'], ['recorded'], ['recorded', 'conflict_marked', 'conflict_cleared']]
    assert all(set(e) == kq.EVENT_KEYS for row in chain for e in row['events'])
    assert chain[0]['events'][0]['reason'] == '합성 사유'
    assert [(l['relation'], l['from_id'], l['to_id'], l['resolved_at']) for l in chain[2]['links']] == [
        ('supersedes', n, c, None), ('conflicts', other, n, T5)]
    assert all(set(l) == kq.LINK_KEYS for row in chain for l in row['links'])
    single = kq.history(db, other)
    assert ids(single['chain']) == [other] and single['chain'][0]['status_as_of'] == 'withdrawn'
    assert single['chain'][0]['evidence'][0]['quote'] == '합성 인용' and single['notice'] == []
    with pytest.raises(knowledge.KnowledgeError) as caught:
        kq.history(db, 999)
    assert caught.value.code == 'assertion_not_found'


def test_history_keeps_the_latest_fifty_rows(db):
    entity = add_entity(db)
    row = add(db, entity=entity, predicate='금액', value='합성 0', at=T1)
    rows = [row]
    for step in range(1, kq.HISTORY_MAX + 2):
        row = supersede(db, row, {'entity': entity, 'predicate': '금액', 'value': f'합성 {step}'},
                        f'2026-10-01T{9 + step // 60:02d}:{step % 60:02d}:00+09:00')
        rows.append(row)
    result = kq.history(db, rows[3])
    assert result['truncated'] is True and ids(result['chain']) == rows[-kq.HISTORY_MAX:]
    assert result['notice'][0] == kq.HISTORY_TRUNCATED_NOTICE


# ---- source state through real scans (design M4 §3.4, §12) -------------------------------------------------

from test_collect import add_root, rewrite, scan  # noqa: E402 - helpers of the collector tests
from workmcp import binding, collect  # noqa: E402

LINES = ('합성 회의록 첫 줄입니다.', '담당자 갑이 가상 거래처 A에 견적을 보냈다.', '다음 회의는 금요일이다.')
QUOTE = '가상 거래처 A'


@pytest.fixture
def scanned(db, tmp_path):
    """One synthetic text file scanned, a fact citing its second line."""
    root = add_root(db, tmp_path/'src')
    path = tmp_path/'src'/'a.txt'
    path.write_bytes('\n'.join(LINES).encode('utf-8'))
    scan(db, root)
    version = db.execute('SELECT id FROM versions WHERE superseded_at IS NULL').fetchone()[0]
    fact = add(db, entity=add_entity(db), predicate='거래처', value='가상 거래처 A', source_kind='evidence')
    add_evidence(db, fact, version, 2, QUOTE)
    return root, path, fact


def detail_evidence(db, fact):
    entity = db.execute('SELECT entity_id FROM assertions WHERE id=?', (fact,)).fetchone()[0]
    detail = kq.entity_detail(db, entity, as_of_valid=None, as_of_recorded=None, include_closed=False,
                              source_kind='all', limit=50)
    history = kq.history(db, fact)['chain'][0]['evidence'][0]
    from_detail = detail['facts'][0]['evidence'][0]
    assert from_detail == history
    return from_detail


def test_source_state_follows_scan_changed_with_requote(db, scanned):
    root, path, fact = scanned
    assert detail_evidence(db, fact)['source_state'] == 'current'
    rewrite(path, '\n'.join(('바뀐 첫 줄입니다.', *LINES[1:])).encode('utf-8'))
    scan(db, root)
    evidence = detail_evidence(db, fact)
    assert evidence['source_state'] == 'changed' and evidence['requote']['found'] is True
    suggested = evidence['requote']['locator']
    assert suggested['sha256'] != evidence['locator']['sha256'] and suggested['chars'] == evidence['locator']['chars']
    assert binding.validate_locator(db, suggested)['text'] == QUOTE     # a real locator of the latest version
    assert kq.assertion_row(db, fact, with_evidence=True)['evidence'][0]['requote'] is None
    assert kq.list_assertions(db, {}, 50, 0)['items'][0]['source_state'] == 'changed'


def test_requote_is_not_found_when_the_quote_left_or_repeats_in_the_unit(db, scanned):
    root, path, fact = scanned
    rewrite(path, '\n'.join((LINES[0], '담당자 갑이 견적을 보냈다.', LINES[2])).encode('utf-8'))
    scan(db, root)
    assert detail_evidence(db, fact)['requote'] == {'found': False}
    rewrite(path, '\n'.join((LINES[0], f'{QUOTE}와 {QUOTE}', LINES[2])).encode('utf-8'))
    scan(db, root)
    assert detail_evidence(db, fact)['requote'] == {'found': False}
    rewrite(path, '\n'.join((LINES[0], LINES[2], LINES[1])).encode('utf-8'))   # moved to another line
    scan(db, root)
    assert detail_evidence(db, fact)['requote'] == {'found': False}            # same unit only (design §3.4)


def test_source_state_follows_scan_deleted_restored_and_skipped(db, scanned, monkeypatch):
    root, path, fact = scanned
    data = path.read_bytes()
    path.unlink()
    scan(db, root)
    evidence = detail_evidence(db, fact)
    assert (evidence['source_state'], evidence['requote']) == ('deleted', None)
    path.write_bytes(data)
    scan(db, root)                                                     # restored: a new version, same text
    evidence = detail_evidence(db, fact)
    assert evidence['source_state'] == 'changed' and evidence['requote']['found'] is True
    assert binding.validate_locator(db, evidence['requote']['locator'])['text'] == QUOTE
    path.unlink()
    scan(db, root)
    path.write_bytes(data)
    real = collect.file_attributes
    monkeypatch.setattr(collect, 'file_attributes',
                        lambda info: real(info) | collect.FILE_ATTRIBUTE_REPARSE_POINT
                        | paths.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    scan(db, root)                                                     # back as an online-only placeholder
    assert db.execute('SELECT status FROM documents').fetchone()[0] == 'skipped'
    evidence = detail_evidence(db, fact)
    assert (evidence['source_state'], evidence['requote']) == ('skipped', None)
    assert kq.assertion_row(db, fact, with_evidence=False)['source_state'] == 'skipped'


def test_reading_never_writes_or_opens_files(db, scanned, monkeypatch):
    """The read path changes no row and opens no file (design M4 §8.1)."""
    import builtins
    _, _, fact = scanned
    before = {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
              for table in ('assertions', 'evidence', 'links', 'entities', 'assertion_events')}
    changes = db.total_changes

    def refuse(*args, **kwargs):
        raise AssertionError('a file was opened')
    monkeypatch.setattr(builtins, 'open', refuse)
    entity = db.execute('SELECT entity_id FROM assertions WHERE id=?', (fact,)).fetchone()[0]
    kq.entity_detail(db, entity, as_of_valid=None, as_of_recorded=None, include_closed=True, source_kind='all',
                     limit=50)
    kq.list_assertions(db, {'status': 'all'}, 50, 0)
    kq.history(db, fact)
    kq.find_entities(db, '합성', None, 20)
    monkeypatch.undo()
    assert db.total_changes == changes
    assert before == {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in before}

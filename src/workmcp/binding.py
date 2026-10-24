"""Binding check: stored units, chunks and evidence quotes re-derive from the current files (design M1 §6).

Scope ported from Motive_vari ``services/kakao_evidence/binding.py`` (tag kakao-p1-complete-20261022):
the check proves that what is stored can be rebuilt from the original with the same parser
version; it does not prove that the parser read the original correctly. A version whose file
changed since it was observed cannot be re-checked and is counted as ``stale``, not as a failure.

Files are read through ``collect.read_source`` only (mode ``'rb'``), once per document, and a
cloud placeholder (online-only file) is not read unless ``read_cloud_placeholders`` is set: reading
it would make the sync client download it (design §2.4). Failure entries carry the path, short
hash, unit coordinates and a fixed problem code, never unit text.
"""
from __future__ import annotations

import hashlib
import json
import os

from . import chunks as chunk_rules
from . import collect, locator, parsers, store

SCOPE = ('결속 100% = parse_status가 ok인 버전 중 현재 파일의 sha256이 그 버전과 같은 것 전부에 대해, 저장된 '
         '단위(순서·종류·좌표·텍스트·해시)·청크·근거 인용이 같은 파서 버전으로 현재 파일에서 다시 만들어진 값과 '
         '같음. 파서가 원문을 올바르게 읽었는지는 보이지 않음. 파일이 바뀐 옛 버전은 재검증 불가(stale)로 따로 셈.')
MAX_FAILURES = 20
UNVERIFIABLE = ('stale', 'missing', 'locked', 'unstable', 'parser_version_mismatch', 'skipped_not_ok', 'placeholder',
                'timeout')
# Mismatches a later ``scan --reparse`` of the cited version explains (review M4 v2 N2); a quote whose own
# hash is wrong, a bad shape or another version is never one of them.
REPARSE_STALE_CODES = frozenset({'unit_not_found', 'coordinate_out_of_range', 'locator_text_mismatch',
                                 'evidence_quote_mismatch'})
REPARSE_STALE_NOTICE = ('warning: 근거 {}건은 인용 뒤 그 버전이 다시 파싱(scan --reparse)돼 원문과 맞지 않습니다 - '
                        '실패가 아님; 조회 도구가 changed와 requote로 보이며, 필요하면 sources로 다시 인용해 supersede')


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _read(path: str, size: int, read_cloud_placeholders: bool = False):
    """``('ok', data, sha)`` or ``(result, None, None)`` for a file that cannot be re-checked."""
    try:
        before = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return 'missing', None, None
    except OSError:
        return 'locked', None, None
    if collect.file_attributes(before) & collect.RECALL_ATTRIBUTES and not read_cloud_placeholders:
        return 'placeholder', None, None  # online-only: opening it would download it
    if before.st_size != size:
        return 'stale', None, None  # different size: different content, no need to read it
    try:
        data, sha, after = collect.read_source(path)
    except collect.SourceLocked:
        return 'locked', None, None
    except FileNotFoundError:
        return 'missing', None, None
    except OSError:
        return 'locked', None, None
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        return 'unstable', None, None
    return 'ok', data, sha


def verify_version(db, version_id: int, cache: dict, limits: parsers.Limits | None = None, files: dict | None = None,
                   *, read_cloud_placeholders: bool = False) -> dict:
    """Re-derive one version (design §6.2). ``cache`` keeps parse results by (sha256, parser version)
    and ``files`` keeps one read per document.

    A version that is not ``ok`` but still has units is a failure (``units_on_not_ok_version``): the
    store claims text the parser status says was never read."""
    limits = limits or parsers.Limits()
    files = {} if files is None else files
    row = db.execute('SELECT v.document_id, v.sha256, v.size, v.parse_status, v.parser_version, d.path, d.ext, '
                     '(SELECT MAX(seq) FROM versions WHERE document_id = v.document_id) = v.seq '
                     'FROM versions v JOIN documents d ON d.id = v.document_id WHERE v.id = ?', (version_id,)).fetchone()
    if row is None:
        raise ValueError(f'no version {version_id}')
    document_id, sha256, size, parse_status, parser_version, path, ext, is_latest = row
    report = {'version_id': version_id, 'document_id': document_id, 'path': path, 'sha12': sha256[:12],
              'latest': bool(is_latest), 'result': None, 'units_total': 0, 'units_bound': 0, 'chunks_checked': 0,
              'chunks_other_version': 0, 'evidence_checked': 0, 'evidence_stale_after_reparse': 0,
              'failures': []}
    if parse_status != 'ok':
        stored = db.execute('SELECT COUNT(*) FROM units WHERE version_id=?', (version_id,)).fetchone()[0]
        if stored:
            report.update(result='failed', units_total=stored)
            report['failures'].append({'version_id': version_id, 'ordinal': None, 'problem': 'units_on_not_ok_version',
                                       'path': path, 'sha12': sha256[:12], 'unit_label': '-'})
        else:
            report['result'] = 'skipped_not_ok'
        return report
    if document_id not in files or files[document_id][0] == 'stale':
        files[document_id] = _read(path, size, read_cloud_placeholders)
    outcome, data, current_sha = files[document_id]
    if outcome == 'ok' and current_sha != sha256:
        outcome = 'stale'
    if outcome != 'ok':
        report['result'] = outcome
        if outcome == 'stale' and is_latest:
            report['note'] = 'file changed since last scan; run scan'
        return report
    if parser_version != parsers.parser_for(ext).version:
        report['result'] = 'parser_version_mismatch'
        report['note'] = 'stored with another parser version; run scan --reparse'
        return report
    key = (sha256, parser_version)
    if key not in cache:
        cache[key] = parsers.parse(data, ext, limits)
    result = cache[key]
    if (result.status, result.error) == ('too_large', 'parse_time'):
        # The only limit that depends on the machine: running out of time again is not a mismatch.
        report['result'] = 'timeout'
        report['note'] = 'parsing again took longer than parse_seconds'
        return report

    failures = report['failures']

    def fail(ordinal, problem, unit=None):
        if len(failures) < MAX_FAILURES:
            failures.append({'version_id': version_id, 'ordinal': ordinal, 'problem': problem, 'path': path,
                             'sha12': sha256[:12], 'unit_label': _label(unit)})

    stored = db.execute('SELECT ordinal, kind, locator_json, text, text_sha256 FROM units WHERE version_id=? '
                        'ORDER BY ordinal', (version_id,)).fetchall()
    report['units_total'] = len(stored)
    if result.status != 'ok':
        fail(None, f'reparse_failed:{result.error}')
        report['result'] = 'failed'
        return report
    derived = {unit.ordinal: unit for unit in result.units}
    if len(stored) != len(result.units):
        fail(None, 'unit_count_mismatch')
    bound = 0
    for ordinal, kind, locator_json, text, text_sha256 in stored:
        unit = derived.get(ordinal)
        problem = None
        if unit is None:
            problem = 'unit_not_derived'
        elif kind != unit.kind:
            problem = 'unit_kind_mismatch'
        elif locator_json != parsers.unit_json(unit.unit):
            problem = 'unit_locator_mismatch'
        elif text != unit.text:
            problem = 'unit_text_mismatch'
        elif text_sha256 != _sha(text):
            problem = 'unit_text_sha256_mismatch'
        if problem:
            fail(ordinal, problem, _unit_or_none(locator_json))
        else:
            bound += 1
    report['units_bound'] = bound

    units = list(result.units)
    current = set()  # rows of the current chunk rules, compared with the chunks derived again (design M2 §2.7)
    for chunk_version, ordinal, first, last, start, end, text, text_sha256 in db.execute(
            'SELECT chunk_version, ordinal, unit_first, unit_last, char_start, char_end, text, text_sha256 FROM chunks '
            'WHERE version_id=? ORDER BY chunk_version, ordinal', (version_id,)):
        report['chunks_checked'] += 1
        if chunk_version == chunk_rules.CHUNK_VERSION:
            current.add((ordinal, first, last, start, end, text_sha256))
        else:
            report['chunks_other_version'] += 1
        if not 1 <= first <= last <= len(units) or not 0 <= start <= end:
            fail(None, 'chunk_range_invalid')
            continue
        joined = '\n'.join(unit.text for unit in units[first - 1:last])
        if joined[start:end] != text:
            fail(None, 'chunk_text_mismatch')
        elif text_sha256 != _sha(text):
            fail(None, 'chunk_text_sha256_mismatch')
    if current:
        derived_chunks = {(c.ordinal, c.unit_first, c.unit_last, c.char_start, c.char_end, c.text_sha256())
                          for c in chunk_rules.build_chunks([(u.ordinal, u.unit, u.text) for u in units])}
        if derived_chunks != current:
            fail(None, 'chunk_set_mismatch')

    by_unit = {parsers.unit_json(unit.unit): unit for unit in units}
    # Review M4 v2 N2: ``scan --reparse`` refills the units of a version in place, so a source recorded before
    # that no longer matches the re-derived text. Such a source (a reparsed event of this version at or after
    # its assertion was recorded) is counted as ``evidence_stale_after_reparse``, not a failure; the read
    # tools show it as ``changed`` with a requote. Any other mismatch (forged, damaged) still fails.
    for locator_json, quote, quote_sha256, reparsed_after in db.execute(
            'SELECT e.locator_json, e.quote, e.quote_sha256, EXISTS(SELECT 1 FROM document_events ev '
            "WHERE ev.version_id = e.version_id AND ev.kind = 'reparsed' "
            'AND julianday(ev.at) >= julianday(a.recorded_from)) '
            'FROM evidence e JOIN assertions a ON a.id = e.assertion_id WHERE e.version_id=? ORDER BY e.id',
            (version_id,)):
        report['evidence_checked'] += 1
        try:
            if _sha(quote) != quote_sha256:
                raise locator.LocatorError('evidence_quote_sha256_mismatch')
            obj = locator.check_shape(json.loads(locator_json))
            if obj['doc'] != document_id or obj['sha256'] != sha256:
                raise locator.LocatorError('evidence_version_mismatch')
            unit = by_unit.get(parsers.unit_json(obj['unit']))
            if unit is None:
                raise locator.LocatorError('unit_not_found')
            if locator.check_text(obj, unit.text) != quote:
                raise locator.LocatorError('evidence_quote_mismatch')
        except (ValueError, TypeError) as exc:  # LocatorError is a ValueError
            code = getattr(exc, 'code', 'evidence_locator_invalid')
            if reparsed_after and code in REPARSE_STALE_CODES:
                report['evidence_stale_after_reparse'] += 1
            else:
                fail(None, f'evidence:{code}')
    report['result'] = 'failed' if failures else 'ok'
    return report


def _unit_or_none(locator_json):
    try:
        return json.loads(locator_json)
    except (TypeError, ValueError):
        return None


def _label(unit) -> str:
    try:
        return locator.unit_label(locator.check_unit(unit))
    except (locator.LocatorError, KeyError, TypeError):
        return '-'


def verify(db, root_ids=None, document_ids=None, limits: parsers.Limits | None = None, *,
           read_cloud_placeholders: bool = False) -> dict:
    """Every version in scope (all, some roots, or some documents), aggregated (design §6.3).

    ``ok`` is "no failure among the checked versions"; ``versions.checked == 0`` means nothing was
    verified (the CLI says so)."""
    sql = 'SELECT v.id FROM versions v JOIN documents d ON d.id = v.document_id'
    clauses, args = [], []
    if root_ids is not None:
        clauses.append(f"d.root_id IN ({','.join('?' * len(root_ids))})")
        args += list(root_ids)
    if document_ids is not None:
        clauses.append(f"d.id IN ({','.join('?' * len(document_ids))})")
        args += list(document_ids)
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    ids = [row[0] for row in db.execute(sql + ' ORDER BY v.document_id, v.seq', args)]
    cache, files = {}, {}
    versions = {'total': len(ids), 'checked': 0, 'ok': 0, 'failed': 0, 'unverifiable': dict.fromkeys(UNVERIFIABLE, 0)}
    units = {'total': 0, 'bound': 0}
    chunks = other_chunks = evidence = stale_evidence = 0
    failures, stale_latest = [], set()
    for version_id in ids:
        report = verify_version(db, version_id, cache, limits, files, read_cloud_placeholders=read_cloud_placeholders)
        result = report['result']
        if result in ('ok', 'failed'):
            versions['checked'] += 1
            versions[result] += 1
            units['total'] += report['units_total']
            units['bound'] += report['units_bound']
            chunks += report['chunks_checked']
            other_chunks += report['chunks_other_version']
            evidence += report['evidence_checked']
            stale_evidence += report['evidence_stale_after_reparse']
            failures += report['failures']
        else:
            versions['unverifiable'][result] += 1
            if result == 'stale' and report['latest']:
                stale_latest.add(report['document_id'])
    units['rate'] = units['bound'] / units['total'] if units['total'] else 0.0
    notice = [f'{len(stale_latest)}개 문서가 마지막 스캔 이후 바뀜 - `workmcp scan`을 먼저 실행'] if stale_latest else []
    if mismatched := versions['unverifiable']['parser_version_mismatch']:
        notice.append(f'{mismatched}개 버전이 다른 파서 버전으로 저장됨 - `workmcp scan --reparse`로 다시 파싱')
    if stale_evidence:
        notice.append(REPARSE_STALE_NOTICE.format(stale_evidence))
    return {'scope': SCOPE, 'parser_versions': {ext: parsers.parser_for(ext).version
                                                for ext in sorted(parsers.SUPPORTED_EXTENSIONS)},
            'checked_at': store.now(), 'versions': versions, 'units': units, 'chunks_checked': chunks,
            'chunks_other_version': other_chunks, 'evidence_checked': evidence,
            'evidence_stale_after_reparse': stale_evidence, 'ok': versions['failed'] == 0, 'failures': failures[:MAX_FAILURES],
            'notice': notice}


def validate_locator(db, obj) -> dict:
    """``{version_id, unit_id, text}`` of a locator checked against the store (design §6.3)."""
    locator.check_shape(obj)
    row = db.execute('SELECT id FROM versions WHERE document_id=? AND sha256=? ORDER BY id DESC LIMIT 1',
                     (obj['doc'], obj['sha256'])).fetchone()
    if row is None:
        raise locator.LocatorError('version_not_found')
    unit = db.execute('SELECT id, text FROM units WHERE version_id=? AND locator_json=?',
                      (row[0], parsers.unit_json(obj['unit']))).fetchone()
    if unit is None:
        raise locator.LocatorError('unit_not_found')
    return {'version_id': row[0], 'unit_id': unit[0], 'text': locator.check_text(obj, unit[1])}

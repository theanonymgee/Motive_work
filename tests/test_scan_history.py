"""``tools/scan_history.py`` (design M6 §8.1): every category is found in the history of a throw-away repository,
a value removed later still counts in history but not in the current files, and no matched value is ever printed.

Every planted value is built at run time, so this source holds none of them (the real history scan reads it too).
"""
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1]/'tools'/'scan_history.py'
GIT = shutil.which('git')
pytestmark = pytest.mark.skipif(GIT is None, reason='no git')


def _git(repo: Path, *args: str) -> str:
    config = ['-c', 'user.name=t', '-c', 'user.email=t' + '@' + 'example.com', '-c', 'commit.gpgsign=false',
              '-c', 'core.autocrlf=false', '-c', 'core.hooksPath=no-hooks', '-c', 'init.defaultBranch=main']
    return subprocess.run([GIT, *config, *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', message)
    return _git(repo, 'log', '-1', '--format=%h')


def _run(*args: str) -> subprocess.CompletedProcess:
    result = subprocess.run([sys.executable, '-I', str(TOOL), *args], capture_output=True)
    return subprocess.CompletedProcess(result.args, result.returncode,
                                       result.stdout.decode('utf-8').replace('\r\n', '\n'),
                                       result.stderr.decode('utf-8', errors='replace'))


def _token() -> str:
    while True:
        value = secrets.token_urlsafe(32)
        if (len(value) == 43 and re.search(r'[A-Z]', value) and re.search(r'[a-z]', value) and re.search(r'\d', value)
                and value[0].isalnum() and value[-1].isalnum()):
            return value


def _section(output: str, title: str) -> str:
    start = output.index('== ' + title)
    end = output.find('\n== ', start + 1)
    return output[start:end if end != -1 else len(output)]


def _hits(section: str, category: str) -> int:
    match = re.search(r'^\[' + re.escape(category) + r'\] (\d+)', section, re.MULTILINE)
    assert match, category
    return int(match.group(1))


@pytest.fixture
def planted(tmp_path):
    repo = tmp_path/'repo'
    repo.mkdir()
    _git(repo, 'init', '-q')
    values = {
        'token': _token(),
        'bearer': _token(),
        'key': 'gh' + 'p_' + 'A1b2C3d4' * 5,
        'mail': 'someone.real' + '@' + 'corp-mail.co.kr',
        'ip': '.'.join(str(n) for n in (8, 8, 4, 4)),
        'user': 'kim' + 'chulsoo',
        'term': 'zebra' + 'corp',
    }
    header = '-----BEGIN ' + 'OPENSSH PRIVATE KEY' + '-----'
    (repo/'notes.md').write_text('\n'.join([
        'token = "' + values['token'] + '"',
        'Authorization: Bearer ' + values['bearer'],
        'key ' + values['key'],
        header,
        'mail ' + values['mail'],
        'server ' + values['ip'] + ' and allowed 127.0.0.1 10.1.2.3 203.0.113.5',
        'path C:\\Users\\' + values['user'] + '\\Project and C:\\Users\\<USER>\\Motive_work',
        'customer ' + values['term'].upper() + ' contract',
        'placeholder Bearer ${WORKMCP_MCP_TOKEN} and Bearer <MCP_TOKEN>',
        '',
    ]), encoding='utf-8')
    for name in ('push.token', '.env', 'keys/id_ed25519', 'cert/server.pem', 'reports/q3.docx',
                 'tests/fixtures/sample.pdf'):
        (repo/name).parent.mkdir(parents=True, exist_ok=True)
        (repo/name).write_bytes(b'x\x00\x01')
    first = _commit(repo, 'add notes')
    (repo/'notes.md').write_text('clean: C:\\Users\\<USER>\\x, <VPS_IP>, owner.gap@example.com\n', encoding='utf-8')
    for name in ('push.token', '.env', 'keys/id_ed25519', 'cert/server.pem', 'reports/q3.docx',
                 'tests/fixtures/sample.pdf'):
        (repo/name).unlink()
    second = _commit(repo, 'clean notes\n\nreported by ' + values['mail'])
    terms = tmp_path/'terms.txt'
    terms.write_text('# outside the repository\n' + values['term'].capitalize() + '\n', encoding='utf-8')
    return repo, terms, values, first, second


def test_every_category_is_found_in_history_and_no_value_is_printed(planted):
    repo, terms, values, first, second = planted
    result = _run('--repo', str(repo), '--terms', str(terms))
    assert result.returncode == 1, result.stderr
    history = _section(result.stdout, 'history')
    expected = {'token-shaped': 2, 'bearer value': 1, 'key material': 2, 'secret file name': 4, 'mail address': 2,
                'public IPv4': 1, 'windows user path': 1, 'term': 1, 'document binary': 1,
                'document binary (synthetic fixture path)': 1}
    assert {category: _hits(history, category) for category in expected} == expected
    for path in ('notes.md', 'push.token', '.env', 'keys/id_ed25519', 'cert/server.pem', 'reports/q3.docx',
                 'tests/fixtures/sample.pdf'):
        assert re.search(r'^  ' + re.escape(first) + r'\s+' + re.escape(path) + r'  \(\d+\)$', history, re.MULTILINE), path
    assert re.search(r'^  ' + re.escape(second) + r'\s+\(commit message\)  \(1\)$', history, re.MULTILINE)
    printed = result.stdout + result.stderr
    for value in values.values():
        assert value.lower() not in printed.lower()
    assert 'PRIVATE KEY' not in printed and 'kimchulsoo' not in printed.lower()


def test_removed_values_stay_in_history_but_not_in_the_current_files(planted):
    repo, terms, values, first, second = planted
    result = _run('--repo', str(repo), '--terms', str(terms))
    now = _section(result.stdout, 'current tracked files')
    assert re.findall(r'^\[[^\]]+\] (\d+)', now, re.MULTILINE) == ['0'] * 10
    assert re.search(r'^result: 10 categories with hits in history, 0 in the current tracked files$', result.stdout,
                     re.MULTILINE)


def test_placeholders_and_reserved_values_are_clean(tmp_path):
    repo = tmp_path/'clean'
    repo.mkdir()
    _git(repo, 'init', '-q')
    (repo/'README.md').write_text('\n'.join([
        'home C:\\Users\\<USER>\\Motive_work, C:\\Users\\Public\\x, %USERPROFILE%\\Motive_work',
        'server https://<VPS_HOST>/workmcp/mcp at <VPS_IP>, loopback 127.0.0.1, docs 198.51.100.7, lan 192.168.0.10',
        'Authorization: Bearer ${WORKMCP_MCP_TOKEN}', 'Authorization: Bearer <MCP_TOKEN>',
        'mail owner.gap' + '@' + 'example.com, noreply' + '@' + 'anthropic.com',
        'hash ' + 'ab12' * 16, '.env.example is fine',
        '',
    ]), encoding='utf-8')
    (repo/'.env.example').write_text('WORKMCP_HOME=\n', encoding='utf-8')
    _commit(repo, 'clean\n\nCo-Authored-By: Someone <noreply' + '@' + 'example.com>')
    result = _run('--repo', str(repo))
    assert result.returncode == 0, result.stdout
    assert 'result: 0 categories with hits in history, 0 in the current tracked files' in result.stdout


def test_not_a_repository_or_missing_terms_file_exits_2(tmp_path):
    result = _run('--repo', str(tmp_path))
    assert result.returncode == 2 and 'git' in result.stderr
    result = _run('--repo', str(tmp_path), '--terms', str(tmp_path/'missing.txt'))
    assert result.returncode == 2 and 'terms' in result.stderr

"""Synthetic-content rules of the document bundle (design M1 §7.1, §7.2), checked on the sources.

These run over every file in ``tests/``, so the generators added later (``make_*.py``) are held
to the same rules.
"""
import re
from pathlib import Path

import pytest

import fixtures

TESTS = Path(__file__).resolve().parent
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)')


def test_tests_folder_holds_python_sources_only():
    """No committed document, archive or other binary fixture: documents are generated at test time."""
    others = [p.relative_to(TESTS).as_posix() for p in TESTS.rglob('*')
              if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.py']
    assert others == []


def test_mail_addresses_in_test_sources_use_reserved_domains():
    found = {}
    for path in TESTS.rglob('*.py'):
        for match in EMAIL.finditer(path.read_text(encoding='utf-8')):
            domain = match.group(1).lower()
            if domain not in fixtures.RESERVED_DOMAINS and not domain.endswith(fixtures.RESERVED_TLD):
                found.setdefault(path.name, []).append(match.group(0))
    assert found == {}


def test_shared_values():
    assert fixtures.SENTINEL in fixtures.LINES_KO[0]
    assert fixtures.SENTINEL_ASCII in fixtures.LINES_EN[0] and fixtures.SENTINEL_ASCII.isascii()
    assert all(line.isascii() for line in fixtures.LINES_EN)
    assert len(set(fixtures.LINES_KO)) == len(fixtures.LINES_KO) and len(set(fixtures.LINES_EN)) == len(fixtures.LINES_EN)
    assert fixtures.mail_address('담당자 갑') == 'owner.gap@example.com'
    assert fixtures.mail_address('담당자 을', 'mail.invalid') == 'owner.eul@mail.invalid'
    with pytest.raises(ValueError):
        fixtures.mail_address('담당자 병', 'gmail.com')
    expected = fixtures.Expected('docs/a.txt', 'text', 'ok', unit_count=1,
                                 units_sample={1: ('line', {'kind': 'line', 'line': 1}, fixtures.LINES_KO[0])})
    assert expected.error is None and expected.encoding is None


def test_client_configs_are_never_tracked():
    """``.mcp.json`` and ``.codex/config.toml`` hold PC-specific paths and the data home; they stay
    git-ignored (design M3 §11.2, review L4). ``git`` runs here, in test code, never in the package."""
    import shutil
    import subprocess
    git = shutil.which('git')
    repo = TESTS.parent
    if git is None or not (repo/'.git').exists():
        pytest.skip('no git checkout')
    tracked = subprocess.run([git, 'ls-files', '--', '.mcp.json', '.codex', '**/.mcp.json', '**/.codex/*'],
                             cwd=repo, capture_output=True, text=True, check=True).stdout.split()
    assert tracked == []
    ignored = subprocess.run([git, 'check-ignore', '--', '.mcp.json', '.codex/config.toml'], cwd=repo,
                             capture_output=True, text=True).stdout.split()
    assert ignored == ['.mcp.json', '.codex/config.toml']


# ---- deployment pieces (design M5 remote §9, §10): no secret, no real token, no personal path ----------

REPO = TESTS.parent
DEPLOY_FILES = sorted((REPO/'tools'/'deploy').glob('*')) + [REPO/'tools'/'vps_survey.sh']
EXAMPLE_FILES = [p for p in sorted((REPO/'doc'/'examples').glob('*')) if p.is_file()]
GUIDE_FILES = sorted((REPO/'doc'/'guides').glob('*.md'))
SHIPPED_FILES = DEPLOY_FILES + EXAMPLE_FILES + GUIDE_FILES + [REPO/'README.md']   # README: design M6 §4.1, M6-14

SECRET_PATTERNS = {
    'private key block': re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    'provider key (sk-/ghp_/hf_/xox/AKIA)': re.compile(r'\b(?:sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|hf_[A-Za-z0-9]{30,}'
                                                       r'|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b'),
    'password-like assignment with a value': re.compile(r'(?i)\b(?:password|passwd|secret|api[_-]?key)\b\s*[=:]\s*'
                                                        r'(?!<|\$|\{|\*|%|"?<)[^\s<$#"\'`)]{6,}'),
}
# a urlsafe token as `secrets.token_urlsafe(32)` makes it: 43 characters with upper, lower and digits
TOKEN_SHAPE = re.compile(r'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])')
BEARER_VALUE = re.compile(r'Bearer\s+(\S+)')
# placeholders only: `<NAME>`, `${VAR}`, `$VAR` and `$env:VAR` (PowerShell expands a user variable), `%s`; review M6
# v1 M6-14 took back `$(cat`, which no tracked file uses any more
BEARER_PLACEHOLDER = re.compile(r'^(?:<[^>]+>|\$\{[A-Za-z_]+\}|\$[A-Za-z_]+|%s|invalid-test-value|\.\.\.)')
PERSONAL_PATTERNS = {
    'Windows user path with a real name': re.compile(r'(?i)[A-Z]:\\Users\\(?!<USER>|Public\b|Default\b)[^\\\s<]+'),
    'macOS user path': re.compile(r'/Users/[^/\s<]+'),
    'Linux home of another user': re.compile(r'/home/(?!workmcp\b|<)[A-Za-z0-9_.-]+'),
}
EMAIL_ANY = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+')
IPV4 = re.compile(r'(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])')
IPV4_ALLOWED = {'127.0.0.1', '127.0.0.0', '0.0.0.0'}


def _text(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def test_deployment_pieces_exist():
    names = {p.name for p in DEPLOY_FILES}
    assert {'workmcp.service', 'nginx-workmcp.conf', 'caddy-workmcp.txt', 'backup.sh', 'backup.timer',
            'workmcp-backup.service', 'vps_survey.sh'} <= names
    assert {p.name for p in EXAMPLE_FILES} >= {'mcp-remote.json.example', 'README.md'}
    assert {p.name for p in GUIDE_FILES} >= {'2026-10-26_VPS-배포절차서_v1.md', '2026-10-26_서버보안점검표_v1.md'}


def test_shipped_pieces_hold_no_secret_pattern():
    found = {}
    for path in SHIPPED_FILES:
        text = _text(path)
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                found.setdefault(path.name, []).append(label)
    assert found == {}


def test_shipped_pieces_hold_no_real_token():
    """No token-shaped string (43 urlsafe characters with upper, lower and a digit); every ``Bearer`` value is a placeholder."""
    found = {}
    for path in SHIPPED_FILES:
        text = _text(path)
        for match in TOKEN_SHAPE.finditer(text):
            value = match.group(0)
            if re.search(r'[A-Z]', value) and re.search(r'[a-z]', value) and re.search(r'\d', value):
                found.setdefault(path.name, []).append('token-shaped: ' + value[:6] + '...')
        for match in BEARER_VALUE.finditer(text):
            if not BEARER_PLACEHOLDER.match(match.group(1)):
                found.setdefault(path.name, []).append('Bearer value: ' + match.group(1)[:8])
    assert found == {}


def test_shipped_pieces_hold_no_personal_path_mail_or_foreign_address():
    found = {}
    for path in SHIPPED_FILES:
        text = _text(path)
        for label, pattern in PERSONAL_PATTERNS.items():
            if pattern.search(text):
                found.setdefault(path.name, []).append(label)
        if EMAIL_ANY.search(text):
            found.setdefault(path.name, []).append('mail address')
        for match in IPV4.finditer(text):
            if match.group(1) not in IPV4_ALLOWED:
                found.setdefault(path.name, []).append('IPv4 ' + match.group(1))
    assert found == {}


# ---- public text (design M6 §8.2): README, AGENTS/CLAUDE, doc/ and docs/ may be published; no personal path or mail ----

PUBLIC_TEXT_FILES = sorted({REPO/'README.md', REPO/'AGENTS.md', REPO/'CLAUDE.md'}
                           | {p for folder in ('doc', 'docs') for p in (REPO/folder).rglob('*') if p.is_file()})
# a user folder name after `Users\` (also `\\`, `/` and Git Bash `/c/Users/`); a placeholder starts with `<`, `%`, `$`
# or `{`, so it never matches the name class; the generic profile folders and one-letter synthetic names (`C:\Users\a`)
# are allowed
USER_PATH = re.compile(r'(?i)(?:\b[A-Z]:|(?<![A-Za-z0-9])/[A-Z]|(?<![A-Za-z0-9:]))(?:\\{1,2}|/)Users(?:\\{1,2}|/)'
                       r'([A-Za-z0-9._-]+)')
USER_PATH_ALLOWED = frozenset({'public', 'default', 'all', 'shared'})
MAIL_ALLOWED_DOMAINS = frozenset(fixtures.RESERVED_DOMAINS) | {'users.noreply.github.com'}
# well-known public resolver named in the server survey; everything else must be private, loopback or documentation
PUBLIC_IPV4_ALLOWED = frozenset({'1.1.1.1'})
# profile folder names that say nothing about a person
GENERIC_PROFILE_NAMES = frozenset({'user', 'users', 'admin', 'administrator', 'owner', 'public', 'default', 'runner',
                                   'test', 'tester', 'workmcp', 'root', 'home', 'vagrant', 'ubuntu', 'developer'})


def personal_findings(text: str) -> list[str]:
    """Labels (never values) of personal data in a public text."""
    import ipaddress
    found = []
    if any(len(m.group(1)) > 1 and m.group(1).lower() not in USER_PATH_ALLOWED for m in USER_PATH.finditer(text)):
        found.append('user folder path with a real name')
    if PERSONAL_PATTERNS['Linux home of another user'].search(text):
        found.append('Linux home of another user')
    for match in EMAIL_ANY.finditer(text):
        local, domain = match.group(0).rsplit('@', 1)
        domain = domain.lower()
        if not (domain in MAIL_ALLOWED_DOMAINS or domain.endswith(fixtures.RESERVED_TLD) or local.lower() == 'noreply'):
            found.append('mail address')
            break
    for match in IPV4.finditer(text):
        try:
            address = ipaddress.IPv4Address(match.group(1))
        except ValueError:
            continue
        if address.is_global and match.group(1) not in PUBLIC_IPV4_ALLOWED:
            found.append('public IPv4')
            break
    return found


def test_public_text_holds_no_personal_path_mail_or_public_address():
    assert REPO/'README.md' in PUBLIC_TEXT_FILES and any(p.parent.name == 'validation' for p in PUBLIC_TEXT_FILES)
    found = {}
    for path in PUBLIC_TEXT_FILES:
        try:
            text = _text(path)
        except UnicodeDecodeError:
            found[path.relative_to(REPO).as_posix()] = ['not UTF-8 text']
            continue
        if labels := personal_findings(text):
            found[path.relative_to(REPO).as_posix()] = labels
    assert found == {}


def test_public_text_and_sources_hold_not_the_profile_folder_name():
    """The name of the profile folder this runs under (read at run time, never written here) appears in no public
    text, deployment piece or source: it would be a user name or part of a mail handle."""
    name = Path.home().name.lower()
    if len(name) < 5 or name in GENERIC_PROFILE_NAMES:
        pytest.skip('profile folder name too short or generic to search for')
    sources = [p for folder in ('src', 'tools', 'tests') for p in (REPO/folder).rglob('*')
               if p.is_file() and p.suffix in ('.py', '.sh', '.toml', '.json', '.md', '.txt', '.conf', '.service',
                                               '.timer', '.example') and '__pycache__' not in p.parts]
    offenders = sorted({p.relative_to(REPO).as_posix() for p in set(PUBLIC_TEXT_FILES) | set(SHIPPED_FILES) | set(sources)
                        if name in p.read_text(encoding='utf-8', errors='replace').lower()})
    assert offenders == []


def test_public_text_rules_catch_known_bad_values():
    """Positive and negative controls for ``personal_findings`` (values built here, not real ones)."""
    someone = 'kim' + 'chulsoo'
    assert personal_findings('C:\\Users\\' + someone + '\\Motive_work') == ['user folder path with a real name']
    assert personal_findings('C:\\\\Users\\\\' + someone + '\\\\x') == ['user folder path with a real name']
    assert personal_findings('/c/Users/' + someone + '/Project') == ['user folder path with a real name']
    assert personal_findings('C:/Users/' + someone + '/Project') == ['user folder path with a real name']
    for placeholder in ('C:\\Users\\<USER>\\Motive_work', 'C:\\Users\\<user>\\x', 'c:\\users\\<사용자명>\\project',
                        'C:\\Users\\%USERNAME%\\x', 'C:\\Users\\Public\\Documents', '`C:\\Users\\`, `/c/Users/`'):
        assert personal_findings(placeholder) == [], placeholder
    assert personal_findings(someone + '@' + 'corp-mail.co.kr') == ['mail address']
    allowed = 'owner.gap@example.com ' + 'noreply' + '@' + 'anthropic.com 1+acct' + '@' + 'users.noreply.github.com'
    assert personal_findings(allowed) == []
    assert personal_findings('server ' + '.'.join(['8', '8', '4', '4'])) == ['public IPv4']
    assert personal_findings('127.0.0.1 10.0.0.2 192.168.1.1 203.0.113.9 1.1.1.1 <VPS_IP>') == []
    assert personal_findings('/home/' + someone + '/app') == ['Linux home of another user']
    assert personal_findings('/home/workmcp/app /home/<user>/x') == []


def test_secret_checks_catch_known_bad_values():
    """Positive controls: the patterns above are not vacuous."""
    token = 'Abcdefghijklmnopqrstuvwxyz0123456789-_ABCDE'
    assert len(token) == 43 and TOKEN_SHAPE.search(token)
    assert BEARER_VALUE.search('Authorization: Bearer ' + token).group(1) == token
    assert not BEARER_PLACEHOLDER.match(token)
    assert BEARER_PLACEHOLDER.match('<MCP_TOKEN>') and BEARER_PLACEHOLDER.match('${WORKMCP_MCP_TOKEN}')
    assert BEARER_PLACEHOLDER.match('$env:WORKMCP_MCP_TOKEN"')
    assert not BEARER_PLACEHOLDER.match('$(cat')                               # review M6 v1 M6-14: taken back
    assert SECRET_PATTERNS['password-like assignment with a value'].search('password = hunter2hunter2')
    assert not SECRET_PATTERNS['password-like assignment with a value'].search('password: <redacted>')
    assert SECRET_PATTERNS['provider key (sk-/ghp_/hf_/xox/AKIA)'].search('hf_' + 'a' * 34)
    assert SECRET_PATTERNS['private key block'].search('-----BEGIN OPENSSH PRIVATE KEY-----')
    assert PERSONAL_PATTERNS['Windows user path with a real name'].search('C:\\Users\\someone\\Motive_work')
    assert not PERSONAL_PATTERNS['Windows user path with a real name'].search('C:\\Users\\<USER>\\Motive_work')
    assert PERSONAL_PATTERNS['Linux home of another user'].search('/home/someone/app')
    assert not PERSONAL_PATTERNS['Linux home of another user'].search('/home/workmcp/app')
    assert EMAIL_ANY.search('a.b@example.com')
    assert [m.group(1) for m in IPV4.finditer('x 203.0.113.9 y 127.0.0.1')] == ['203.0.113.9', '127.0.0.1']


def test_remote_example_is_json_with_placeholder_token_only():
    import json
    data = json.loads(_text(REPO/'doc'/'examples'/'mcp-remote.json.example'))
    server = data['mcpServers']['workmcp']
    assert server['type'] == 'http' and server['url'].startswith('https://') and server['url'].endswith('/workmcp/mcp')
    assert server['headers']['Authorization'] == 'Bearer ${WORKMCP_MCP_TOKEN}'


def test_unit_files_have_no_trailing_comments_and_stay_on_loopback():
    """systemd takes ``# ...`` after a value as part of the value: comments must be on their own lines."""
    deploy = REPO/'tools'/'deploy'
    for name in ('workmcp.service', 'workmcp-backup.service', 'backup.timer'):
        for number, line in enumerate(_text(deploy/name).splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or (stripped.startswith('[') and stripped.endswith(']')):
                continue
            assert re.match(r'^[A-Za-z]+=', stripped), f'{name}:{number} is not a Key=Value line'
            assert ' #' not in stripped, f'{name}:{number} has a trailing comment'
    service = _text(deploy/'workmcp.service')
    assert 'ExecStart=/home/workmcp/app/.venv/bin/python -I -m workmcp serve --http 127.0.0.1:8765' in service
    assert 'IPAddressDeny=any' in service and 'User=workmcp' in service
    for name in ('workmcp.service', 'nginx-workmcp.conf', 'caddy-workmcp.txt'):
        text = _text(deploy/name)
        assert '0.0.0.0' not in text and '[::]' not in text
    assert 'proxy_pass http://127.0.0.1:8765;' in _text(deploy/'nginx-workmcp.conf')
    assert 'reverse_proxy 127.0.0.1:8765' in _text(deploy/'caddy-workmcp.txt')


def _script_code(text: str) -> str:
    """A shell script without comments, heredoc bodies and single-quoted strings (for the command checks)."""
    out, heredoc_end = [], None
    for line in text.splitlines():
        if heredoc_end is not None:
            if line.strip() == heredoc_end:
                heredoc_end = None
            continue
        marker = re.search(r"<<-?\s*'?\"?([A-Za-z_]+)'?\"?", line)
        if marker:
            heredoc_end = marker.group(1)
        if line.lstrip().startswith('#'):
            continue
        out.append(re.sub(r"'[^']*'", "''", line))
    return '\n'.join(out)


SURVEY_FORBIDDEN = ('rm', 'mv', 'cp', 'tee', 'chmod', 'chown', 'chgrp', 'ln', 'mkdir', 'touch', 'truncate', 'dd', 'install',
                    'useradd', 'usermod', 'userdel', 'groupadd', 'apt', 'apt-get', 'dpkg', 'pip', 'pip3', 'curl', 'wget',
                    'scp', 'rsync', 'nc', 'ssh', 'kill', 'pkill', 'reboot', 'shutdown', 'passwd', 'mount', 'umount')
SYSTEMCTL_READ_ONLY = {'list-units', 'list-timers', 'is-active', '--failed', '--version', 'show', 'status'}


def test_survey_script_only_reads():
    code = _script_code(_text(REPO/'tools'/'vps_survey.sh'))
    position = r'(?:^|[|;&]|\$\(|\bthen\b|\bdo\b|\belse\b)\s*(?:sudo\s+-n\s+|\$SUDO\s+|nice\s+|exec\s+)?'
    offenders = [word for word in SURVEY_FORBIDDEN
                 if re.search(position + re.escape(word) + r'(?![\w-])', code, re.MULTILINE)]
    assert offenders == []
    subcommands = {m.group(1) for m in re.finditer(r'systemctl\s+(?:--no-pager\s+|--plain\s+)*(\S+)', code)}
    assert subcommands <= SYSTEMCTL_READ_ONLY
    assert not re.search(r'sed\s+(?:-[A-Za-z]*i|--in-place)', code)
    assert not re.search(r'(?:^|[|;&]|&&|\$\()\s*sudo(?!\s+-n)\b', code, re.MULTILINE)
    # redirections only to /dev/null, to another descriptor, or from /dev/null
    stripped = re.sub(r'"[^"]*"', '""', code)
    assert not re.search(r'(?<![0-9&<=-])>{1,2}(?!\s*(?:/dev/null|&))', stripped)


def test_survey_script_scrubs_credential_lines():
    text = _text(REPO/'tools'/'vps_survey.sh')
    assert 'scrub()' in text and '<redacted>' in text
    assert 'credential' in text and 'authorization' in text and 'Bearer' in text
    assert 'sudo -n' in text and 'nothing was changed' in text


def test_survey_script_scrub_behaviour():
    """Run the script's own ``scrub`` on strings (needs bash; skipped where there is none)."""
    import shutil
    import subprocess
    bash = shutil.which('bash')
    if bash is None:
        pytest.skip('no bash')
    text = _text(REPO/'tools'/'vps_survey.sh')
    start = text.index('scrub() {')
    function = text[start:text.index('\n}\n', start) + 3]
    sample = 'token=abc123\nAuthorization: Bearer abcdefghijkl\nX-Api-Key: zzzzzzzz\nplain line\n'
    result = subprocess.run([bash, '-c', function + '\nscrub'], input=sample, capture_output=True, text=True, check=True)
    assert 'abc123' not in result.stdout and 'abcdefghijkl' not in result.stdout and 'zzzzzzzz' not in result.stdout
    assert 'plain line' in result.stdout and '<redacted>' in result.stdout


def test_shell_scripts_have_valid_syntax():
    import shutil
    import subprocess
    bash = shutil.which('bash')
    if bash is None:
        pytest.skip('no bash')
    for path in (REPO/'tools'/'vps_survey.sh', REPO/'tools'/'deploy'/'backup.sh'):
        result = subprocess.run([bash, '-n', str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


# ---- line endings: files that run on the Linux server are LF in the repository, whatever the exporting PC's autocrlf ----

_LF_ONLY_SUFFIXES = ('.sh', '.service', '.timer', '.conf')


def _lf_only_files():
    deploy = sorted(p for p in (REPO/'tools'/'deploy').iterdir() if p.is_file())
    scripts = sorted(p for p in (REPO/'tools').glob('*.sh'))
    return [p.relative_to(REPO).as_posix() for p in deploy + scripts]


def test_gitattributes_forces_lf_for_scripts_and_deploy_pieces():
    """``git archive`` honours ``eol=lf``; without it a Windows export (``core.autocrlf=true``) ships CRLF to the server."""
    rules = {}
    for line in (REPO/'.gitattributes').read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            pattern, *attributes = line.split()
            rules[pattern] = attributes
    for pattern in ('*.sh', '*.service', '*.timer', '*.conf', 'tools/deploy/*'):
        assert 'eol=lf' in rules.get(pattern, []) and 'text' in rules[pattern], pattern
    import fnmatch
    for relative in _lf_only_files():
        name = relative.rsplit('/', 1)[-1]
        assert any('eol=lf' in attributes and fnmatch.fnmatchcase(relative if '/' in pattern else name, pattern)
                   for pattern, attributes in rules.items()), relative
    assert any(relative.endswith('.sh') for relative in _lf_only_files())


def test_scripts_and_deploy_pieces_are_lf_in_the_repository():
    import shutil
    import subprocess
    files = _lf_only_files()
    assert any(relative.endswith(_LF_ONLY_SUFFIXES) for relative in files)
    for relative in files:
        assert bytes([13]) not in (REPO/relative).read_bytes(), relative
    git = shutil.which('git')
    if git is None or not (REPO/'.git').exists():
        return
    result = subprocess.run([git, 'ls-files', '--eol', '--'] + files, cwd=REPO, capture_output=True, text=True)
    if result.returncode != 0:
        return
    for line in result.stdout.splitlines():
        index_eol = line.split()[0]
        assert index_eol in ('i/lf', 'i/none'), line


# ---- review v1 (L6-L10, L12): side-effect-free survey, read-only code path, ^~ location, real test names ----

def test_survey_script_avoids_commands_that_write_as_a_side_effect():
    """``nginx -T``/``apache2ctl -S`` may create log files, ``certbot`` writes its log on every run, ``ufw status`` is a
    python program, and python writes bytecode unless ``-B``: the survey reads files or uses ``-B -I`` instead."""
    code = _script_code(_text(REPO/'tools'/'vps_survey.sh'))
    position = r'(?:^|[|;&]|\$\(|\bthen\b|\bdo\b|\belse\b)\s*(?:sudo\s+-n\s+|\$SUDO\s+|nice\s+|exec\s+)?'
    for command in (r'nginx\s+-T', r'certbot\b', r'apache2ctl\s+-S', r'ufw\s+status', r'caddy\s+(?:run|start|reload|adapt)'):
        assert not re.search(position + command, code, re.MULTILINE), command
    interpreters = re.findall(r'(?m)^\s*"\$py"[^\n<]*', code) + re.findall(r'(?m)^\s*try python3[^\n]*', code)
    assert len(interpreters) == 2 and all(' -B ' in text + ' ' for text in interpreters), interpreters
    assert 'PYTHONDONTWRITEBYTECODE=1' in code
    assert 'openssl x509 -noout -enddate' in code


def test_service_code_path_is_read_only_and_only_the_data_home_is_writable():
    deploy = REPO/'tools'/'deploy'
    for name in ('workmcp.service', 'workmcp-backup.service'):
        text = _text(deploy/name)
        assert 'ReadWritePaths=/home/workmcp/Motive_work\n' in text, name
        assert 'ReadOnlyPaths=/home/workmcp/app\n' in text, name
        assert not re.search(r'(?m)^ReadWritePaths=/home/workmcp\s*$', text), name
    assert 'PYTHONDONTWRITEBYTECODE=1' in _text(deploy/'workmcp.service')


def test_nginx_location_wins_over_regex_locations_and_caddy_note_covers_order():
    nginx = _text(REPO/'tools'/'deploy'/'nginx-workmcp.conf')
    assert re.search(r'(?m)^location \^~ /workmcp/ \{', nginx)
    assert not re.search(r'(?m)^location /workmcp/ \{', nginx)
    caddy = _text(REPO/'tools'/'deploy'/'caddy-workmcp.txt')
    assert 'handle /workmcp/*' in caddy and 'caddy adapt' in caddy and 'basic_auth' in caddy


def test_guides_name_only_tests_that_exist():
    """Every ``tests/<file>.py::<name>`` the guides point at is a real test function (a missing name makes pytest exit 5)."""
    found = {}
    pattern = re.compile(r'(tests/[A-Za-z0-9_]+\.py)::([A-Za-z0-9_]+)')
    for path in GUIDE_FILES:
        for file, name in pattern.findall(_text(path)):
            source = REPO/file
            if not source.exists() or not re.search(rf'(?m)^(?:async )?def {name}\(', _text(source)):
                found.setdefault(path.name, []).append(f'{file}::{name}')
    assert found == {}
    assert any(pattern.search(_text(path)) for path in GUIDE_FILES)


def test_guides_do_not_point_at_pytest_selectors_that_select_nothing():
    """``-k route_table`` selected no test (review L12): the checklist names tests by their full ids."""
    for path in GUIDE_FILES:
        assert 'route_table' not in _text(path), path.name


def test_deployment_guide_covers_the_synthetic_collector_home_and_restore_follow_ups():
    guide = _text(REPO/'doc'/'guides'/'2026-10-26_VPS-배포절차서_v1.md')
    for needle in ('WORKMCP_HOME', '[push]', 'url = "https://', 'PYTHONPATH', '--dry-run', 'workmcp token list',
                   'workmcp-deploy', '다시 폐기'):
        assert needle in guide or needle.replace('workmcp token list', 'wm token list') in guide, needle
    assert 'survey-before-' in guide and 'Motive_work_synthetic' in guide
    assert 'git에 넣지 않는다' in guide

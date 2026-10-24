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

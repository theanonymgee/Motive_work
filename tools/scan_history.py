"""Pre-publication check of the whole git history (design M6 §8.1). Standard library only; it never imports
``workmcp`` (``tests/test_tools_dir.py``) and runs ``git`` as a child process, locally (no remote is contacted).

    python -I tools/scan_history.py [--repo PATH] [--terms FILE] [--limit N]

It reads every line ever added in any commit reachable from any ref (``git log --all -p``, merges contribute
through their parents), every commit message, every path ever added (``--diff-filter=A``) and, for comparison,
the tracked files as they are now in the working tree. Categories:

    token-shaped        43 urlsafe characters with upper, lower and a digit (``secrets.token_urlsafe(32)``)
    bearer value        ``Bearer`` followed by a value that is not a placeholder (``<...>``, ``$VAR``, ...)
    key material        provider key prefixes (sk-, ghp_, hf_, xox, AKIA) or a private key header line
    secret file name    a path named like a secret (``.env``, ``*.token``, ``auth.json``, ``*.pem``, ``id_rsa`` ...)
    mail address        an address outside the reserved and noreply domains
    public IPv4         an IPv4 address that is globally routable (private, loopback, documentation ranges excluded)
    windows user path   ``C:\\Users\\<name>`` (also ``C:/Users/``, ``/c/Users/``) where the name is not a placeholder
    term                a case-insensitive term from ``--terms FILE`` (one per line; keep the file outside the
                        repository: it holds company, people, host or user names)
    document binary     a path with a document extension (.docx .xlsx .pptx .pdf .hwp ...)
    document binary (synthetic fixture path)   the same under a fixture/synthetic path, listed apart

The output names only the category, counts, the short commit hash and the file path. A matched value is never
printed: values are numbered in order of first appearance (``value #n``) with their length, so equal values can be
recognised without showing them. Exit codes: 0 nothing found, 1 something found, 2 not a git repository or git failed.
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path, PurePosixPath

TOKEN_SHAPE = re.compile(r'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])')
BEARER = re.compile(r'Bearer\s+([A-Za-z0-9._~+/=$%{}()<>:-]+)')
BEARER_PLACEHOLDER = re.compile(r'^(?:<[^>]*>?|\{|\$\{?[A-Za-z_]|\$\(|%s|\.\.\.|…|invalid-test-value)')
KEY_PREFIX = re.compile(r'\b(?:sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|hf_[A-Za-z0-9]{30,}'
                        r'|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b')
KEY_HEADER = re.compile(r'^\s*-----BEGIN [A-Z ]*PRIVATE KEY-----\s*$')
MAIL = re.compile(r'(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)')
MAIL_ALLOWED_DOMAINS = ('example.com', 'example.org', 'example.net', 'users.noreply.github.com')
MAIL_ALLOWED_SUFFIXES = ('.example', '.invalid', '.test', '.localhost')
IPV4 = re.compile(r'(?<!\d)(?<!\d\.)(\d{1,3}(?:\.\d{1,3}){3})(?!\d|\.\d)')
WIN_USER = re.compile(r'(?i)(?:\b[A-Z]:|(?<![A-Za-z0-9])/[A-Z])(?:\\{1,2}|/)Users(?:\\{1,2}|/)([A-Za-z0-9._-]+)')
WIN_USER_GENERIC = frozenset({'public', 'default', 'all', 'default.migrated'})
SECRET_NAME = re.compile(r'(?i)^(?:\.env(?:\..+)?|.+\.token|auth\.json|credentials\.json|.+\.pem|.+\.key|.+\.p12|.+\.pfx'
                         r'|id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?|\.netrc|\.pypirc|\.git-credentials)$')
SECRET_NAME_ALLOWED = re.compile(r'(?i)^\.env\.example$')
DOCUMENT_SUFFIXES = frozenset({'.docx', '.doc', '.xlsx', '.xls', '.pptx', '.ppt', '.pdf', '.hwp', '.hwpx', '.msg'})
FIXTURE_PART = re.compile(r'(?i)(?:^|/)(?:tests?/fixtures?|fixtures?|synthetic)(?:/|$)')

CATEGORIES = ('token-shaped', 'bearer value', 'key material', 'secret file name', 'mail address', 'public IPv4',
              'windows user path', 'term', 'document binary', 'document binary (synthetic fixture path)')


class Findings:
    """Hits per category: (where, file) -> count, plus a numbering of distinct values that never stores them
    beyond this run."""

    def __init__(self):
        self.hits = {name: defaultdict(int) for name in CATEGORIES}
        self.values = {name: {} for name in CATEGORIES}

    def add(self, category: str, where: str, file: str, value: str) -> None:
        self.hits[category][(where, file)] += 1
        self.values[category].setdefault(value, (len(self.values[category]) + 1, len(value)))

    def total(self, category: str) -> int:
        return sum(self.hits[category].values())


def _has_mixed_classes(value: str) -> bool:
    return bool(re.search(r'[A-Z]', value) and re.search(r'[a-z]', value) and re.search(r'\d', value))


def _mail_allowed(local: str, domain: str) -> bool:
    domain = domain.lower()
    return (local.lower() in ('noreply', 'no-reply', 'git') or domain in MAIL_ALLOWED_DOMAINS
            or domain.endswith(MAIL_ALLOWED_SUFFIXES))


def scan_text(line: str, where: str, file: str, findings: Findings, terms: list[str]) -> None:
    """Apply the text categories to one line."""
    for match in TOKEN_SHAPE.finditer(line):
        if _has_mixed_classes(match.group(0)):
            findings.add('token-shaped', where, file, match.group(0))
    for match in BEARER.finditer(line):
        value = match.group(1)
        classes = sum(bool(re.search(c, value)) for c in (r'[A-Z]', r'[a-z]', r'\d'))
        if len(value) >= 12 and classes >= 2 and not BEARER_PLACEHOLDER.match(value):
            findings.add('bearer value', where, file, value)
    for match in KEY_PREFIX.finditer(line):
        findings.add('key material', where, file, match.group(0))
    if KEY_HEADER.match(line):
        findings.add('key material', where, file, line.strip())
    for match in MAIL.finditer(line):
        if not _mail_allowed(match.group(1), match.group(2)):
            findings.add('mail address', where, file, match.group(0))
    for match in IPV4.finditer(line):
        try:
            address = ipaddress.IPv4Address(match.group(1))
        except ValueError:
            continue
        if address.is_global:
            findings.add('public IPv4', where, file, match.group(1))
    for match in WIN_USER.finditer(line):
        if match.group(1).lower() not in WIN_USER_GENERIC:
            findings.add('windows user path', where, file, match.group(1).lower())
    if terms:
        lowered = line.lower()
        for term in terms:
            if term in lowered:
                findings.add('term', where, file, term)


def scan_path(path: str, where: str, findings: Findings, terms: list[str]) -> None:
    """Apply the path categories to one path that was added (a term in a path counts too)."""
    name = PurePosixPath(path).name
    if SECRET_NAME.match(name) and not SECRET_NAME_ALLOWED.match(name):
        findings.add('secret file name', where, path, path)
    if PurePosixPath(path).suffix.lower() in DOCUMENT_SUFFIXES:
        category = 'document binary (synthetic fixture path)' if FIXTURE_PART.search(path) else 'document binary'
        findings.add(category, where, path, path)
    lowered = path.lower()
    for term in terms:
        if term in lowered:
            findings.add('term', where, path, term)


def _git(repo: Path, *args: str) -> str:
    git = shutil.which('git')
    if git is None:
        raise RuntimeError('git not found')
    result = subprocess.run([git, '-C', str(repo), '-c', 'core.quotepath=off', *args], capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f'git {args[0]} failed ({result.returncode})')
    return result.stdout.decode('utf-8', errors='replace')


def _unquote(path: str) -> str:
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        return path[1:-1].encode('latin-1', errors='replace').decode('unicode_escape', errors='replace')
    return path


def scan_history(repo: Path, findings: Findings, terms: list[str]) -> dict:
    """Added lines, commit messages and added paths of every commit reachable from any ref."""
    commits = [c for c in _git(repo, 'rev-list', '--all').split() if c]
    added_lines = 0
    log = _git(repo, 'log', '--all', '-p', '-U0', '--no-color', '--no-ext-diff', '--no-textconv', '--no-renames',
               '--format=%x00commit %h')
    commit, file, in_hunk = '?', '?', False
    for line in log.split('\n'):
        if line.startswith('\x00commit '):
            commit, file, in_hunk = line.split(' ', 1)[1].strip(), '?', False
        elif line.startswith('diff --git '):
            file, in_hunk = '?', False
        elif not in_hunk and line.startswith('+++ '):
            target = _unquote(line[4:].rstrip('\t'))
            file = target[2:] if target.startswith('b/') else target
        elif line.startswith('@@'):
            in_hunk = True
        elif in_hunk and line.startswith('+'):
            added_lines += 1
            scan_text(line[1:], commit, file, findings, terms)
    messages = _git(repo, 'log', '--all', '--format=%x00%h%x00%B')
    parts = messages.split('\x00')[1:]
    for short, body in zip(parts[0::2], parts[1::2]):
        for line in body.splitlines():
            scan_text(line, short.strip(), '(commit message)', findings, terms)
    names = _git(repo, 'log', '--all', '--diff-filter=A', '--no-renames', '--name-only', '--format=%x00%h')
    paths = set()
    for block in names.split('\x00')[1:]:
        lines = block.split('\n')
        short = lines[0].strip()
        for path in lines[1:]:
            path = _unquote(path.strip())
            if path:
                paths.add(path)
                scan_path(path, short, findings, terms)
    return {'commits': len(commits), 'added_lines': added_lines, 'paths': len(paths)}


def scan_worktree(repo: Path, findings: Findings, terms: list[str]) -> int:
    """The tracked files as they are now on disk (what the next commit would publish)."""
    files = [f for f in _git(repo, 'ls-files', '-z').split('\x00') if f]
    for relative in files:
        scan_path(relative, 'worktree', findings, terms)
        path = repo/relative
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b'\x00' in data[:8000]:
            continue
        for line in data.decode('utf-8', errors='replace').splitlines():
            scan_text(line, 'worktree', relative, findings, terms)
    return len(files)


def load_terms(path: Path | None) -> list[str]:
    if path is None:
        return []
    terms = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            terms.append(line.lower())
    return terms


def report(title: str, findings: Findings, limit: int, out) -> int:
    """Print one block per category; return the number of categories with hits."""
    print(f'== {title}', file=out)
    found = 0
    for category in CATEGORIES:
        total = findings.total(category)
        if not total:
            print(f'[{category}] 0', file=out)
            continue
        found += 1
        values = findings.values[category]
        print(f'[{category}] {total} hits, {len(values)} distinct values '
              f'(lengths {sorted({length for _, length in values.values()})})', file=out)
        rows = sorted(findings.hits[category].items(), key=lambda item: (item[0][1], item[0][0]))
        for (where, file), count in rows[:limit]:
            print(f'  {where:<9} {file}  ({count})', file=out)
        if len(rows) > limit:
            print(f'  ... {len(rows) - limit} more (commit, file) rows; --limit to show them', file=out)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Pre-publication scan of the whole git history (values never printed).')
    parser.add_argument('--repo', type=Path, default=Path('.'), help='repository folder (default: current folder)')
    parser.add_argument('--terms', type=Path, help='file of extra terms, one per line (keep it outside the repository)')
    parser.add_argument('--limit', type=int, default=40, help='(commit, file) rows shown per category (default 40)')
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    try:
        terms = load_terms(args.terms)
    except OSError as exc:
        print(f'scan_history: cannot read the terms file ({exc.__class__.__name__})', file=sys.stderr)
        return 2
    history, worktree = Findings(), Findings()
    try:
        _git(repo, 'rev-parse', '--git-dir')
        counts = scan_history(repo, history, terms)
        tracked = scan_worktree(repo, worktree, terms)
    except RuntimeError as exc:
        print(f'scan_history: {exc}', file=sys.stderr)
        return 2
    out = sys.stdout
    if not out.isatty():
        out.reconfigure(encoding='utf-8', errors='replace')
    print(f'scan_history: {counts["commits"]} commits (all refs), {counts["added_lines"]} added lines, '
          f'{counts["paths"]} paths ever added; {tracked} tracked files now; {len(terms)} terms', file=out)
    in_history = report('history (every commit, every ref; added lines, messages, added paths)', history,
                        args.limit, out)
    now = report('current tracked files (working tree)', worktree, args.limit, out)
    print(f'result: {in_history} categories with hits in history, {now} in the current tracked files', file=out)
    return 1 if in_history or now else 0


if __name__ == '__main__':
    raise SystemExit(main())

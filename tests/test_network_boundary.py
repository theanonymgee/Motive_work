"""No-egress guards for the package source (design §7).

Static layer: an import **allowlist** per module over every ``src/workmcp`` file, plus bans that
hold whatever the import (dynamic code loading, ``sys.modules``, ``os`` process calls through
aliases or ``getattr``, computed ``getattr``, non-stdio transports, models network functions).
Each check has positive controls, including the 29 bypasses found in review
``doc/validation/2026-10-23_M0-검토_v1.md`` §1.7.

Runtime layer: ``tests/_audited_server.py`` runs the package under an audit hook. Boundary:
no socket connect/bind/send to an address other than loopback (127.0.0.1, ::1), no name
resolution and no process creation; loopback is allowed only for the asyncio event loop's
internal socket pair.

Ported in part from Motive_vari ``services/kakao_evidence/tests/test_kakao_isolation.py``
(tag kakao-p1-complete-20261022).
"""
import ast
import importlib.machinery
import json
import os
import re
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

import pytest

import _audited_server

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT/'src'/'workmcp'
WRAPPER = Path(__file__).resolve().parent/'_audited_server.py'

# Allowlists. An entry allows that dotted name and everything under it. Each entry must be used
# by the real sources (``test_allowlists_match_the_sources_exactly``), so the lists never grow
# wider than the code needs.
COMMON_IMPORTS = frozenset({'__future__', 'argparse', 'collections.abc', 'dataclasses', 'datetime', 'hashlib',
                            'importlib.metadata', 'io', 'json', 'os', 'pathlib', 're', 'sqlite3', 'sys', 'typing'})
# M1 parsers: the exact import lines of design M1 §8.1 (zip/XML/OLE/PDF/mail only where they are read).
MODULE_IMPORTS = {
    'server.py': frozenset({'mcp.server.MCPServer'}),
    'paths.py': frozenset({'unicodedata'}),  # NFC before comparing sync folder names (M0 review v2 N-L8)
    'config.py': frozenset({'tomllib'}),
    'collect.py': frozenset({'fnmatch'}),
    'cli.py': frozenset({'logging'}),
    'parsers/_container.py': frozenset({'zipfile', 'xml.parsers.expat', 'olefile', 'time'}),
    'parsers/text.py': frozenset({'csv'}),
    'parsers/eml.py': frozenset({'email.policy', 'email.parser.BytesParser', 'html.parser.HTMLParser'}),
    'parsers/pdf.py': frozenset({'logging', 'pypdf.PdfReader', 'pypdf.errors.PdfReadError', 'pypdf.errors.LimitReachedError'}),
    'parsers/hwp.py': frozenset({'struct', 'zlib', 'olefile'}),
}
FUNCTION_IMPORTS = {'models.py': frozenset({'urllib.error', 'urllib.request'})}  # inside function bodies only

MODELS_DOTTED = 'workmcp.models'
BANNED_NAMES = frozenset({'exec', 'eval', 'compile', '__import__', '__builtins__', 'globals', 'locals', 'vars',
                          'breakpoint'})
DYNAMIC_LOADERS = frozenset({'import_module', 'resolve_name', 'run_module', 'run_path', 'find_spec',
                             'module_from_spec', 'exec_module', 'spec_from_file_location', 'spec_from_loader',
                             'load_module', 'entry_points', 'enable_load_extension', 'load_extension'})
ESCAPE_ATTRS = frozenset({'__dict__', '__globals__', '__builtins__', '__subclasses__', '__loader__', '__import__'})
OS_PROCESS_CALLS = ('system', 'popen', 'startfile', 'fork', 'forkpty', 'exec', 'spawn', 'posix_spawn')
NON_STDIO_RUNNERS = frozenset({'run_sse_async', 'run_streamable_http_async', 'sse_app', 'streamable_http_app'})
# Network parts of an allowlisted package (``logging`` is allowed for log files and the pypdf logger only).
NETWORK_SUBMODULES = (('logging', 'handlers'), ('logging', 'config'))
# Process pools start processes; banned even if ``concurrent.futures`` is allowlisted one day (review v2 N-L7).
PROCESS_POOLS = frozenset({'ProcessPoolExecutor'})
GUARDED_MODULES = ('os', 'sys')  # may not be passed around or rebound (aliasing hides later calls)
NETWORK_CLIENT_PACKAGES = {'httpx', 'httpx2', 'requests', 'aiohttp', 'huggingface_hub', 'urllib3'}


def package_sources():
    return sorted(PACKAGE.rglob('*.py'))


def importable_non_sources(folder: Path) -> list[str]:
    """Files Python could import that the ``*.py`` checks never read (review v2 N-L3).

    ``.pyw`` (a Windows source suffix), extension modules (``.pyd``) and sourceless ``.pyc``
    outside ``__pycache__`` (a ``.pyc`` inside ``__pycache__`` is only used next to its ``.py``).
    """
    suffixes = tuple(sorted(set(importlib.machinery.all_suffixes()) - {'.py'}))
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob('*')
                  if path.is_file() and path.name.endswith(suffixes) and '__pycache__' not in path.parts)


def rel(path: Path) -> str:
    return path.relative_to(PACKAGE).as_posix()


def covered(dotted: str, entries) -> bool:
    return any(dotted == e or dotted.startswith(e + '.') for e in entries)


def walk_with_scope(tree):
    """``(node, parent, in_function)`` for every node."""
    stack = [(tree, None, False)]
    while stack:
        node, parent, in_function = stack.pop()
        yield node, parent, in_function
        inner = in_function or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        stack.extend((child, node, inner) for child in ast.iter_child_nodes(node))


def absolute_imports(tree):
    """``(dotted name, node, in_function)`` for every absolute import (``from a import b`` -> ``a.b``)."""
    for node, _, in_function in walk_with_scope(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node, in_function
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                yield f'{node.module}.{alias.name}', node, in_function


def bindings(tree) -> dict[str, str]:
    """Local name -> what it refers to (``import os as o`` -> ``o: os``; relative -> ``.models``)."""
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    root = alias.name.split('.')[0]
                    bound[root] = root
        elif isinstance(node, ast.ImportFrom):
            base = '.' * node.level + (node.module or '')
            for alias in node.names:
                sep = '' if base.endswith('.') or not base else '.'
                bound[alias.asname or alias.name] = f'{base}{sep}{alias.name}'
    return bound


def qualify(node, bound) -> str | None:
    """Dotted name of a ``Name``/``Attribute`` chain with import aliases resolved, else None."""
    if isinstance(node, ast.Name):
        return bound.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = qualify(node.value, bound)
        return f'{base}.{node.attr}' if base else None
    return None


def models_aliases(tree) -> set[str]:
    """Local names that refer to the models module.

    Any name ``models`` imported from inside the package counts, whichever module it is taken from
    (``from .cli import models`` is the same module object as ``from . import models``, review v2 N-L2).
    """
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == MODELS_DOTTED and a.asname}
            aliases |= {'workmcp' for a in node.names if a.name == MODELS_DOTTED and not a.asname}
        elif isinstance(node, ast.ImportFrom) and (node.level >= 1 or (node.module or '').split('.')[0] == 'workmcp'):
            aliases |= {a.asname or a.name for a in node.names if a.name == 'models'}
    return aliases


def is_package_name(dotted: str) -> bool:
    """A qualified name inside the package (relative import or ``workmcp.``)."""
    return dotted.startswith('.') or dotted.split('.')[0] == 'workmcp'


def banned_qualified(dotted: str) -> str | None:
    parts = dotted.split('.')
    if parts[0] == 'os' and len(parts) > 1 and parts[1].startswith(OS_PROCESS_CALLS):
        return f'os process call {dotted}'
    if parts[:2] == ['sys', 'modules']:
        return 'sys.modules access'
    if tuple(parts[:2]) in NETWORK_SUBMODULES:
        return f'network submodule {dotted}'
    if PROCESS_POOLS & set(parts) or parts[:3] == ['concurrent', 'futures', 'process']:
        return f'process pool {dotted}'
    if parts[-1] in DYNAMIC_LOADERS:
        return f'dynamic code loading {dotted}'
    if parts[-1] in ESCAPE_ATTRS:
        return f'namespace escape {dotted}'
    if parts[-1] in NON_STDIO_RUNNERS:
        return f'non-stdio transport {dotted}'
    return None


def is_stdio_run(call: ast.Call) -> bool:
    """Only ``<x>.run(transport="stdio")``: no positional, no ``**``, exactly that literal keyword."""
    return (not call.args and len(call.keywords) == 1 and call.keywords[0].arg == 'transport'
            and isinstance(call.keywords[0].value, ast.Constant) and call.keywords[0].value.value == 'stdio')


def violations(name: str, source: str, models_network: set[str] = frozenset()) -> list[str]:
    """``name`` is the path relative to the package (``models.py``, ``parsers/__init__.py``)."""
    tree = ast.parse(source)
    found = []
    bound = bindings(tree)
    module_allowed = COMMON_IMPORTS | MODULE_IMPORTS.get(name, frozenset())
    function_allowed = module_allowed | FUNCTION_IMPORTS.get(name, frozenset())
    max_level = name.count('/') + 1
    models = models_aliases(tree) if name != 'models.py' else set()
    guarded = {local for local, target in bound.items() if target in GUARDED_MODULES} | set(GUARDED_MODULES)

    for dotted, node, in_function in absolute_imports(tree):
        if dotted.endswith('.*') or any(a.name == '*' for a in node.names):
            found.append(f'star import {dotted}')
        elif not covered(dotted, function_allowed if in_function else module_allowed):
            found.append(f'import not allowed here: {dotted}')
        elif reason := banned_qualified(dotted):
            found.append(reason)

    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node, parent, _ in walk_with_scope(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            names = {a.name for a in node.names}
            if node.level > max_level:
                found.append(f'relative import outside the package (level {node.level})')
            if '*' in names:
                found.append('star import')
            if name != 'models.py' and node.module == 'models' and names & models_network:
                found.append(f'imports models network function {sorted(names & models_network)}')
        elif isinstance(node, ast.ImportFrom) and node.module == MODELS_DOTTED and name != 'models.py':
            if {a.name for a in node.names} & (models_network | {'*'}):
                found.append('imports models network function')
        elif isinstance(node, ast.Name):
            if node.id in BANNED_NAMES:
                found.append(f'banned builtin {node.id}')
            if node.id in {'getattr', 'setattr'} and id(node) not in call_funcs:
                found.append(f'{node.id} used as a value')
            bare = not (isinstance(parent, ast.Attribute) and parent.value is node)
            first_getattr_arg = (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
                                 and parent.func.id == 'getattr' and parent.args and parent.args[0] is node)
            if bare and not first_getattr_arg and (node.id in models or node.id in guarded):
                found.append(f'module {node.id} used as a value')
        elif isinstance(node, ast.Attribute):
            dotted = qualify(node, bound)
            if dotted and (reason := banned_qualified(dotted)):
                found.append(reason)
            elif node.attr in DYNAMIC_LOADERS | ESCAPE_ATTRS | NON_STDIO_RUNNERS:
                found.append(f'banned attribute {node.attr}')
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            # A chain rooted at an imported absolute module stays inside the allowlist
            # (``import importlib.metadata`` does not open ``importlib.util``).
            if (dotted and isinstance(root, ast.Name) and root.id in bound and not bound[root.id].startswith('.')
                    and not covered(dotted, function_allowed) and not any(e.startswith(dotted + '.') for e in function_allowed)):
                found.append(f'attribute outside the allowlist {dotted}')
            if name != 'models.py' and isinstance(node.value, ast.Name) and node.value.id in models \
                    and node.attr in models_network:
                found.append(f'models network function {node.value.id}.{node.attr}')
            if name != 'models.py' and dotted == f'{MODELS_DOTTED}' and isinstance(parent, ast.Attribute) \
                    and parent.attr in models_network:
                found.append(f'models network function {MODELS_DOTTED}.{parent.attr}')
            # Through another package module: ``cli.models.fetch``, ``workmcp.tools.models.download_file``.
            parts = dotted.split('.') if dotted else []
            if name != 'models.py' and len(parts) >= 2 and parts[-2] == 'models' and parts[-1] in models_network:
                found.append(f'models network function through {dotted}')
            if (name != 'models.py' and node.attr == 'models' and dotted and is_package_name(dotted)
                    and not (isinstance(parent, ast.Attribute) and parent.value is node)):
                found.append(f'module {dotted} used as a value')
            if node.attr == 'run' and id(node) not in call_funcs:
                found.append('run used as a value')
        elif isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == 'run') or (isinstance(func, ast.Name) and func.id == 'run'):
                if not is_stdio_run(node):
                    found.append('run(...) other than run(transport="stdio")')
            if isinstance(func, ast.Name) and func.id in {'getattr', 'setattr'}:
                key = node.args[1] if len(node.args) > 1 else None
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    found.append(f'{func.id} with a computed attribute name')
                else:
                    target = qualify(node.args[0], bound) or '?'
                    if reason := banned_qualified(f'{target}.{key.value}'):
                        found.append(f'{func.id}: {reason}')
                    if key.value in models_network:
                        found.append('getattr on models network function')
                    if key.value == 'models' and name != 'models.py':
                        found.append('getattr on the models module')
    return found


def network_functions(models_source: str) -> set[str]:
    """Top-level functions of models.py that import outside the common allowlist, or call one that does."""
    functions = {n.name: n for n in ast.parse(models_source).body if isinstance(n, ast.FunctionDef)}
    tainted = {name for name, fn in functions.items()
               if any(not covered(dotted, COMMON_IMPORTS) for dotted, _, _ in absolute_imports(fn))}
    while True:
        more = {name for name, fn in functions.items() if name not in tainted and any(
            isinstance(n, ast.Name) and n.id in tainted for n in ast.walk(fn))}
        if not more:
            return tainted
        tainted |= more


def models_network():
    return network_functions((PACKAGE/'models.py').read_text(encoding='utf-8'))


def test_package_has_sources():
    assert {rel(p) for p in package_sources()} >= {
        '__init__.py', '__main__.py', 'cli.py', 'paths.py', 'store.py', 'locator.py', 'models.py', 'tools.py',
        'server.py', 'parsers/__init__.py', 'parsers/_container.py', 'parsers/text.py', 'parsers/eml.py',
        'parsers/docx.py', 'parsers/pptx.py', 'parsers/xlsx.py', 'parsers/pdf.py', 'parsers/hwp.py',
        'config.py', 'collect.py', 'binding.py'}


def test_package_has_no_importable_file_other_than_py():
    assert importable_non_sources(PACKAGE) == []


def test_importable_non_source_check_flags_every_form(tmp_path):
    for rel_path in ('a.pyw', 'sub/b.pyd', 'c.pyc', 'sub/d.cp312-win_amd64.pyd', '__pycache__/e.cpython-312.pyc',
                     'f.py', 'g.txt', 'sub/__pycache__/h.cpython-312.pyc'):
        (tmp_path/rel_path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path/rel_path).write_bytes(b'')
    assert importable_non_sources(tmp_path) == ['a.pyw', 'c.pyc', 'sub/b.pyd', 'sub/d.cp312-win_amd64.pyd']
    assert {'.pyw', '.pyc'} <= set(importlib.machinery.all_suffixes()) or os.name != 'nt'


# 1
def test_package_sources_have_no_violations():
    network = models_network()
    offenders = {rel(p): v for p in package_sources() if (v := violations(rel(p), p.read_text(encoding='utf-8'), network))}
    assert offenders == {}


def test_allowlists_match_the_sources_exactly():
    """Every allowlist entry is needed by the real sources, and each extra only where it is listed."""
    used = {rel(p): list(absolute_imports(ast.parse(p.read_text(encoding='utf-8')))) for p in package_sources()}
    common_used = {e for imports in used.values() for dotted, _, _ in imports for e in COMMON_IMPORTS if covered(dotted, {e})}
    assert common_used == COMMON_IMPORTS
    for module, extra in MODULE_IMPORTS.items():
        names = {dotted for dotted, _, _ in used[module] if not covered(dotted, COMMON_IMPORTS)}
        assert names == extra
    for module, extra in FUNCTION_IMPORTS.items():
        names = {dotted for dotted, _, in_function in used[module] if not covered(dotted, COMMON_IMPORTS)}
        assert names == extra
        assert all(in_function for dotted, _, in_function in used[module] if covered(dotted, extra))
    special = {e for extras in (*MODULE_IMPORTS.values(), *FUNCTION_IMPORTS.values()) for e in extras}
    for module, imports in used.items():
        if module not in MODULE_IMPORTS and module not in FUNCTION_IMPORTS:
            assert not {dotted for dotted, _, _ in imports if covered(dotted, special)}


# 2
def test_models_network_functions_are_transitive():
    source = ('def download(u):\n    import urllib.request\n    return urllib.request.urlopen(u)\n'
              'def fetch(n):\n    return download(n)\n'
              'def main():\n    fetch(1)\n'
              'def verify(n):\n    return n\n')
    assert network_functions(source) == {'download', 'fetch', 'main'}
    assert {'download_file', 'fetch', 'main'} <= models_network()
    assert not models_network() & {'entry', 'verify', 'quick_status', 'identity', 'sha256_file', 'require_verified'}


# 3 Positive controls carried over from the first M0 version (checked as an ordinary module).
EARLIER_CONTROLS = [
    'import socket',
    'import subprocess\nsubprocess.run(["curl", "x"])',
    'import asyncio',
    'from multiprocessing.connection import Client',
    'import os\nos.system("curl x")',
    'import os\nos.popen("x")',
    'from os import system',
    '__import__("ht" + "tpx")',
    'import importlib\nimportlib.import_module("x")',
    'from .models import fetch',
    'from . import models\nmodels.download_file("u", None, None, 0, "")',
    'from workmcp.models import *',
    'from . import models as md\nmd.fetch("m")',
    'import workmcp.models as mm\nmm.fetch("m")',
    'from workmcp import models as m\nm.fetch("m")',
    'import workmcp.models\nworkmcp.models.fetch("m")',
    'from . import models\ngetattr(models, "fe" + "tch")("m")',
    'from . import models as md\ngetattr(md, "fetch")("m")',
    'getattr(object(), "download_file")',
    'from . import models\nmodels.__dict__["fetch"]("m")',
    'from . import models as md\nvars(md)["fetch"]("m")',
    'from mcp.server import MCPServer',
    'server.run(transport="sse")',
    'server.run("streamable-http")',
    'import httpx2',
]

# The 29 bypasses of review §1.7 (27 were missed by the denylist version, 2 were caught).
REVIEW_BYPASSES = [
    'import os as o\no.system("curl x")',
    'getattr(os, "system")("curl x")',
    'import nt\nnt.system("curl x")',
    'import _winapi\n_winapi.CreateProcess(None, "curl x", None, None, 0, 0, None, None, None)',
    'import _socket',
    'import ctypes',
    'import _overlapped',
    'from importlib import import_module as im\nim("socket")',
    'exec("import socket")',
    'eval("__imp" + "ort__(\'socket\')")',
    '__builtins__["__import__"]("socket")',
    'pkgutil.resolve_name("socket:create_connection")',
    'runpy.run_module("http.server")',
    'spec = importlib.util.find_spec("socket")\nmod = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(mod)',
    'sys.modules["socket"]',
    'import uvicorn\nuvicorn.run(app, host="0.0.0.0", port=8000)',
    'from starlette.applications import Starlette',
    'import socketserver',
    'from wsgiref.simple_server import make_server',
    'import nntplib',
    'from concurrent.futures import ProcessPoolExecutor\nProcessPoolExecutor().submit(print, "x")',
    'import win32com.client\nwin32com.client.Dispatch("MSXML2.XMLHTTP")',
    'from . import models\nm = models\nm.fetch("x")',
    'sys.modules["workmcp.models"].fetch("x")',
    't = "sse"\nserver.run(transport=t)',
    'server.run(**{"transport": "sse"})',
    'getattr(server, "run_sse" + "_async")',
    'from .. import models',
    'from . import models\nf = models.fetch',
]

# Further forms of the same holes (installed packages, aliases, allowlisted parents).
MORE_BYPASSES = [
    'import httpcore2',
    'import sse_starlette',
    'import _ssl',
    'import pkgutil\npkgutil.resolve_name("socket:create_connection")',
    'import runpy\nrunpy.run_path("x.py")',
    'import importlib.util\nimportlib.util.find_spec("socket")',
    'import importlib.metadata\nimportlib.import_module("socket")',
    'import importlib.metadata\nimportlib.util.find_spec("socket")',
    'import importlib.metadata\nimportlib.metadata.entry_points()',
    'import importlib.metadata\nimportlib.__import__("socket")',
    'import sys\nsys.modules["socket"]',
    'import sys as s\ns.modules["socket"]',
    'from sys import modules',
    'import os\no2 = os\no2.system("x")',
    'from os import system as run_it',
    'from os import *',
    'import os\ngetattr(os, "sys" + "tem")("x")',
    'import os as o\ngetattr(o, "startfile")("x")',
    'g = getattr\ng(os, "system")',
    'compile("import socket", "x", "exec")',
    'breakpoint()',
    'globals()["__builtins__"]',
    'type(object).__subclasses__(object)',
    'import sqlite3\nsqlite3.connect(":memory:").enable_load_extension(True)',
    'import collections\ncollections.OrderedDict()',
    'run = server.run\nrun(transport="sse")',
    'server.run(transport="stdio", host="0.0.0.0")',
    'server.run("stdio")',
    'server.run_sse_async()',
    'import mcp.server.sse',
    'from mcp.server.streamable_http import StreamableHTTPSessionManager',
    'import anyio',
    'import threading',
    'import json\nimport socket',
    'def f():\n    import socket',
    'import logging.handlers\nlogging.handlers.SocketHandler("example.invalid", 9020)',
    'import logging\nlogging.handlers.HTTPHandler("example.invalid", "/")',
    'from logging import handlers',
    'import logging\nlogging.config.listen(9030)',
    'import logging as lg\ngetattr(lg, "handlers")',
]

# Review v2 N-L2: models network functions reached through another package module.
THROUGH_OTHER_MODULES = [
    'from . import cli\ncli.models.fetch("m")',
    'from .cli import models\nmodels.fetch("m")',
    'from .tools import models as mm\nmm.download_file("u", None, None, 0, "")',
    'import workmcp.cli\nworkmcp.cli.models.fetch("m")',
    'from workmcp import tools\ntools.models.main()',
    'from workmcp.cli import models as m\nm.fetch("m")',
    'from .. import cli\ncli.models.fetch("m")',
    'from . import cli\nm = cli.models\nm.fetch("m")',
    'from . import cli\ngetattr(cli, "models").fetch("m")',
    'from . import cli\ngetattr(cli.models, "fetch")("m")',
    'from .cli import models\nm = models',
]


@pytest.mark.parametrize('snippet', EARLIER_CONTROLS)
def test_checks_flag_earlier_violation_snippets(snippet):
    assert violations('search.py', snippet, {'fetch', 'download_file', 'main'})


@pytest.mark.parametrize('module', ['search.py', 'server.py'])
@pytest.mark.parametrize('snippet', REVIEW_BYPASSES + MORE_BYPASSES + THROUGH_OTHER_MODULES)
def test_checks_flag_bypass_snippets(module, snippet):
    assert violations(module, snippet, {'fetch', 'download_file', 'main'})


@pytest.mark.parametrize('snippet', THROUGH_OTHER_MODULES)
def test_models_through_other_modules_is_flagged_in_a_subpackage_too(snippet):
    assert violations('parsers/text.py', snippet, {'fetch', 'download_file', 'main'})


def test_models_through_other_modules_keeps_offline_functions_usable():
    network = {'fetch', 'download_file', 'main'}
    assert violations('tools.py', 'from . import models\nmodels.quick_status(models.DEFAULT_MODEL, h)\n', network) == []
    assert violations('tools.py', 'from . import cli\ncli.models.quick_status("m", h)\n', network) == []
    assert violations('tools.py', 'from . import cli\ncli.models.fetch("m", h)\n', network)


@pytest.mark.parametrize('snippet, banned', [
    ('from concurrent.futures import ProcessPoolExecutor', True),
    ('import concurrent.futures\nconcurrent.futures.ProcessPoolExecutor()', True),
    ('import concurrent.futures as cf\ncf.ProcessPoolExecutor(2)', True),
    ('from concurrent.futures import process', True),
    ('import concurrent.futures.process', True),
    ('import concurrent.futures\ngetattr(concurrent.futures, "ProcessPoolExecutor")', True),
    ('from concurrent.futures import ThreadPoolExecutor', False),
    ('import logging\nlogging.getLogger("x")', False),
    ('import logging\nlogging.handlers.SocketHandler("example.invalid", 9020)', True),
    ('from logging.handlers import SysLogHandler', True),
    ('from logging import handlers', True),
    ('import logging\nlogging.config.listen(9030)', True),
    ('from logging.config import listen', True),
])
def test_process_pools_and_logging_network_parts_stay_banned_when_their_parent_is_allowlisted(
        monkeypatch, snippet, banned):
    """Allowlist entries match by prefix, so allowing ``concurrent.futures`` or ``logging`` would open their
    children; these stay banned whatever the allowlist says (review v2 N-L7)."""
    monkeypatch.setitem(MODULE_IMPORTS, 'search.py', frozenset({'concurrent.futures', 'logging'}))
    assert bool(violations('search.py', snippet)) is banned


def test_review_bypass_list_is_complete():
    assert len(REVIEW_BYPASSES) == 29 and len(set(REVIEW_BYPASSES)) == 29


def test_urllib_is_allowed_only_inside_models_functions():
    inside = 'def download(u):\n    import urllib.request\n    return urllib.request.urlopen(u)\n'
    assert violations('models.py', inside) == []
    assert violations('models.py', 'import urllib.request\n')
    assert violations('models.py', 'def f():\n    import socket\n')
    assert violations('tools.py', inside)


@pytest.mark.parametrize('snippet', [
    'import logging.handlers', 'import logging\nlogging.handlers.SocketHandler("example.invalid", 9020)',
    'from logging.handlers import DatagramHandler', 'import logging\nlogging.config.listen(9030)',
    'import logging.config'])
def test_logging_allowlist_does_not_open_network_handlers(snippet):
    assert violations('parsers/pdf.py', 'import logging\nlogging.getLogger("pypdf").setLevel(logging.ERROR)\n') == []
    assert violations('parsers/pdf.py', snippet)


def test_parser_libraries_are_allowed_only_in_their_modules():
    assert violations('parsers/pdf.py', 'from pypdf import PdfReader\n') == []
    assert violations('parsers/hwp.py', 'import olefile\nimport zlib\n') == []
    assert violations('parsers/docx.py', 'from pypdf import PdfReader\n')
    assert violations('parsers/docx.py', 'import zipfile\n')  # through _container only
    assert violations('parsers/text.py', 'import olefile\n')
    assert violations('server.py', 'import xml.etree.ElementTree as ET\n')
    assert violations('parsers/_container.py', 'import xml.dom.minidom\n')
    assert violations('parsers/eml.py', 'import email.utils\nemail.utils.make_msgid()\n')


def test_m1_collector_imports_are_allowed_only_in_their_modules():
    assert violations('config.py', 'import tomllib\n') == []
    assert violations('collect.py', 'import fnmatch\n') == []
    assert violations('cli.py', 'import logging\nlogging.FileHandler("x.log", encoding="utf-8")\n') == []
    assert violations('collect.py', 'import tomllib\n')
    assert violations('binding.py', 'import fnmatch\n')
    assert violations('collect.py', 'import logging\n')  # events go to a callback; the CLI logs
    assert violations('cli.py', 'import logging.handlers\n')
    assert violations('collect.py', 'import shutil\n')
    assert violations('binding.py', 'import tempfile\n')


def test_server_may_use_mcp_but_not_other_transports():
    assert violations('server.py', 'from mcp.server import MCPServer\nserver.run(transport="stdio")\n') == []
    assert violations('server.py', 'server.run(transport="sse")')
    assert violations('server.py', 'import anyio\nanyio.run(server.run_sse_async)')
    assert violations('server.py', 'import socket')
    assert violations('tools.py', 'from mcp.server import MCPServer')


# 4
def test_checks_accept_clean_snippet():
    clean = ('import os\nfrom . import models\nfrom . import models as md\nfrom .paths import home\nx = os.environ.get("A")\n'
             'models.require_verified("m")\nmd.identity("m")\ngetattr(md, "identity")\nimport re\nre.compile("x")\n'
             'from collections.abc import Mapping\nimport importlib.metadata\nimportlib.metadata.version("mcp")\n'
             'getattr(os.stat("."), "st_file_attributes", None)\n')
    assert violations('embedder.py', clean, {'fetch', 'download_file', 'main'}) == []


# 5 Runtime layer
def run_wrapper(tmp_path, mode):
    log = tmp_path/f'audit-{mode}.json'
    env = {**os.environ, 'WORKMCP_HOME': str(tmp_path/'home'), 'PYTHONPATH': str(ROOT/'src')}
    run = subprocess.run([sys.executable, str(WRAPPER), str(log), mode], env=env, cwd=tmp_path,
                         capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert run.returncode == 0, run.stderr
    assert log.is_file(), run.stderr
    report = json.loads(log.read_text(encoding='utf-8'))
    assert report['completed'] is True, run.stderr
    return run, report


def test_import_all_modules_stays_inside_the_runtime_boundary(tmp_path):
    _, report = run_wrapper(tmp_path, 'imports')
    assert {'workmcp.server', 'workmcp.models', 'workmcp.tools', 'workmcp.parsers', 'workmcp.parsers._container',
            'workmcp.parsers.text', 'workmcp.parsers.eml', 'workmcp.parsers.docx', 'workmcp.parsers.pptx',
            'workmcp.parsers.xlsx', 'workmcp.parsers.pdf', 'workmcp.parsers.hwp', 'workmcp.config', 'workmcp.collect',
            'workmcp.binding'} <= set(report['modules'])
    # the parser libraries really were imported under the hook (their import side effects are audited)
    assert {'pypdf', 'olefile', 'xml.parsers.expat', 'zipfile', 'email.parser'} <= set(report['loaded'])
    assert [e for e in report['events'] if e['violation']] == []
    assert not [e for e in report['events'] if e['event'].startswith(('socket.getaddrinfo', 'socket.gethostbyname'))]


MCP_SESSION = [
    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
     'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'audit-test', 'version': '0'}}},
    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'status', 'arguments': {}}},
]


def serve_session(tmp_path, messages, expected_ids, timeout=60):
    """Run the wrapper in ``serve`` mode; keep stdin open until every expected reply arrived, then close it."""
    log = tmp_path/'audit-serve.json'
    env = {**os.environ, 'WORKMCP_HOME': str(tmp_path/'home'), 'PYTHONPATH': str(ROOT/'src')}
    proc = subprocess.Popen([sys.executable, str(WRAPPER), str(log), 'serve'], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=tmp_path,
                            text=True, encoding='utf-8')
    watchdog = threading.Timer(timeout, proc.kill)
    watchdog.start()
    try:
        proc.stdin.write(''.join(json.dumps(m) + '\n' for m in messages))
        proc.stdin.flush()
        lines, seen = [], set()
        while not expected_ids <= seen:
            line = proc.stdout.readline()
            if not line:
                break  # server exited or was killed by the watchdog
            lines.append(line)
            if line.strip().startswith('{') and (reply_id := json.loads(line).get('id')) is not None:
                seen.add(reply_id)
        proc.stdin.close()
        rest, stderr = proc.stdout.read(), proc.stderr.read()
        code = proc.wait()
    finally:
        watchdog.cancel()
        proc.kill()
    lines += rest.splitlines(keepends=True)
    assert code == 0, stderr  # (d)
    assert log.is_file(), stderr
    report = json.loads(log.read_text(encoding='utf-8'))
    assert report['completed'] is True, stderr
    return lines, stderr, report


def test_serve_path_stays_inside_the_runtime_boundary(tmp_path):
    """Real ``serve`` over stdio: stdout is JSON-RPC only, sockets loopback only, no process, exit 0."""
    lines, stderr, report = serve_session(tmp_path, MCP_SESSION, {1, 2, 3})
    stdout = ''.join(lines)
    assert stdout.endswith('\n'), stdout[-200:]  # every message, the last one too, ends with a newline
    records = stdout[:-1].split('\n')  # only that final newline is dropped; blank lines stay and fail below
    assert all(record.strip() for record in records), records  # (a) no blank or whitespace-only line ...
    messages = [json.loads(record) for record in records]  # ... every stdout line parses ...
    assert all(isinstance(m, dict) and m.get('jsonrpc') == '2.0' for m in messages)  # ... as a JSON-RPC object
    replies = {m['id']: m for m in messages if 'id' in m}
    assert set(replies) == {1, 2, 3}, stderr
    assert replies[1]['result']['serverInfo']['name'] == 'workmcp'
    assert 'status' in {tool['name'] for tool in replies[2]['result']['tools']}
    status = json.loads(replies[3]['result']['content'][0]['text'])
    assert status['ok'] and status['result']['schema_version'] == 1 and status['delivered_sources'] == []
    events = report['events']
    assert [e for e in events if e['violation']] == []  # (b) loopback only, no name resolution
    for e in events:
        if e['event'] in _audited_server.ADDRESS_EVENTS:
            assert re.search(r"\('(127\.0\.0\.1|::1)', \d+", e['detail']), e
    binds = [e for e in events if e['event'] == 'socket.bind']
    assert all(re.search(r"\('(127\.0\.0\.1|::1)', 0[,)]", e['detail']) for e in binds), binds  # port 0 only
    assert not [e for e in events if e['event'].startswith(_audited_server.PROCESS_EVENTS)]  # (c)
    assert not [e for e in events if e['event'].startswith(('socket.getaddrinfo', 'socket.gethostbyname'))]


@pytest.mark.parametrize('event, args, outside', [
    ('socket.connect', (None, ('127.0.0.1', 50000)), False),
    ('socket.bind', (None, ('127.0.0.1', 0)), False),
    ('socket.bind', (None, ('::1', 0, 0, 0)), False),
    ('socket.bind', (None, ('127.0.0.1', 8765)), True),     # fixed-port loopback listener (review v2 N-L5)
    ('socket.bind', (None, ('::1', 8765, 0, 0)), True),
    ('socket.bind', (None, ('127.0.0.1', '0')), True),
    ('socket.bind', (None, ('127.0.0.1',)), True),
    ('socket.connect', (None, ('::1', 50000, 0, 0)), False),
    ('socket.sendmsg', (None, None), False),
    ('socket.__new__', (None, 2, 1, 0), False),
    ('socket.connect', (None, ('203.0.113.1', 443)), True),
    ('socket.bind', (None, ('0.0.0.0', 8000)), True),
    ('socket.bind', (None, ('', 8000)), True),
    ('socket.connect', (None, ('localhost', 80)), True),
    ('socket.connect', (None, ('127.0.0.2', 80)), True),
    ('socket.sendto', (None, ('198.51.100.7', 53)), True),
    ('socket.getaddrinfo', ('127.0.0.1', 80, 0, 0, 0), True),
    ('socket.gethostbyname', ('example.invalid',), True),
    ('subprocess.Popen', ('curl', ['curl'], None, None), True),
    ('os.system', (b'curl x',), True),
    ('_winapi.CreateProcess', (None, 'curl', None), True),
    ('http.client.connect', (None, 'example.invalid', 443), True),
    ('urllib.Request', ('https://example.invalid', None, {}, 'GET'), True),
    ('open', ('x', 'r', 0), False),
])
def test_runtime_judge_separates_loopback_from_everything_else(event, args, outside):
    assert (_audited_server.judge(event, args) is not None) is outside


def test_audit_wrapper_detects_and_blocks_outside_events(tmp_path):
    """Positive control through the real hook: outside attempts are recorded and blocked before they happen."""
    _, report = run_wrapper(tmp_path, 'probe')
    temporary = {'source_open_temporary': True} if _audited_server.O_TEMPORARY else {}
    assert report['blocked'] == {'connect': True, 'getaddrinfo': True, 'subprocess': True, 'loopback': False,
                                 'loopback_listener': True, 'source_read': False, 'source_write': True,
                                 'source_create': True, 'source_remove': True, 'source_utime': True,
                                 **temporary, 'source_sqlite': True, 'source_sqlite_uri': True}
    assert report['source_unchanged'] is True  # the delete-on-close open did not delete the file, no .db made
    source_flagged = [e for e in report['events'] if e.get('source') and e['violation']]
    assert {e['event'] for e in source_flagged} >= {'open', 'os.remove', 'os.utime', 'sqlite3.connect'}
    if temporary:
        flags = [int(e['violation'].split()[2]) for e in source_flagged if e['violation'].startswith('os.open flags')]
        assert any(value & _audited_server.O_TEMPORARY and not value & (os.O_WRONLY | os.O_RDWR) for value in flags)
    flagged = {e['event'] for e in report['events'] if e['violation']}
    assert {'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen', 'socket.bind'} <= flagged
    listener = [e for e in report['events'] if e['event'] == 'socket.bind' and e['violation']]
    assert len(listener) == 1 and str(_audited_server.PROBE_LISTEN_PORT) in listener[0]['detail']
    loopback = [e for e in report['events'] if e['event'] in ('socket.bind', 'socket.connect')
                and '127.0.0.1' in e['detail'] and e not in listener]
    assert loopback and all(e['violation'] is None for e in loopback)


# 6
def requirement_name(spec: str) -> str:
    return re.split(r'[<>=!~;\[\s@]', spec, maxsplit=1)[0].strip().lower().replace('-', '_')


def test_direct_dependencies_have_no_network_client():
    project = tomllib.loads((ROOT/'pyproject.toml').read_text(encoding='utf-8'))['project']
    names = {requirement_name(spec) for spec in project['dependencies']}
    assert 'mcp' in names
    assert names & NETWORK_CLIENT_PACKAGES == set()
    assert requirement_name('httpx[http2]>=0.28') == 'httpx'


# 7
def test_only_models_imports_network_inside_functions():
    imports = list(absolute_imports(ast.parse((PACKAGE/'models.py').read_text(encoding='utf-8'))))
    assert all(covered(d, COMMON_IMPORTS) for d, _, inside in imports if not inside)
    assert {d for d, _, inside in imports if inside} == {'urllib.error', 'urllib.request'}

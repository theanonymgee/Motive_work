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

M5 remote (design M5 remote §6): each module has its own allowed transport (``ALLOWED_RUN``): ``server.py``
stdio, ``remote.py`` Streamable HTTP on a loopback literal host, nothing elsewhere. ``push.py`` may import
``urllib`` inside functions (like ``models.py``), never ``ssl``, holds no URL literal, opens no file and does
not import the collector, parsers or binding check; ``receive.py`` and ``sync.py`` stay offline and apart
from ``push``/``remote``. The runtime modes ``remote`` (server: no egress, its own loopback listener) and
``push`` (collector: the configured server only) have their judges here (``_audited_server.judge_remote``,
``judge_push``); their runs come in stage 1.

Ported in part from Motive_vari ``services/kakao_evidence/tests/test_kakao_isolation.py``
(tag kakao-p1-complete-20261022).
"""
import ast
import importlib.machinery
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

import pytest

import _audited_server
from fixtures import SENTINEL, bundle

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
    # M3 (design M3 §6.2, appendix C): the request Context (client name), anyio for the idle-release
    # ticker (sleep, create_task_group), contextlib for the lifespan, time for elapsed, logging to stderr.
    # pydantic's strict int/bool types keep the SDK from turning true or 2.0 into an integer (review L1).
    'server.py': frozenset({'mcp.server.MCPServer', 'mcp.server.mcpserver.Context', 'anyio', 'contextlib', 'time',
                            'logging', 'pydantic.StrictBool', 'pydantic.StrictInt'}),
    'paths.py': frozenset({'unicodedata'}),  # NFC before comparing sync folder names (M0 review v2 N-L8)
    'config.py': frozenset({'tomllib'}),
    'collect.py': frozenset({'fnmatch'}),
    'cli.py': frozenset({'logging'}),
    'parsers/_container.py': frozenset({'zipfile', 'xml.parsers.expat', 'olefile', 'time'}),
    'parsers/text.py': frozenset({'csv'}),
    'parsers/eml.py': frozenset({'email.policy', 'email.parser.BytesParser', 'html.parser.HTMLParser'}),
    'parsers/pdf.py': frozenset({'logging', 'pypdf.PdfReader', 'pypdf.errors.PdfReadError', 'pypdf.errors.LimitReachedError'}),
    'parsers/hwp.py': frozenset({'struct', 'zlib', 'olefile'}),
    # M2 (design M2 §7.1): numpy at module level for the embedder, index and search; the search
    # engine's clock and model release also need time and gc. Exact import forms:
    # ``import numpy as np``, ``import onnxruntime as ort``, ``from tokenizers import Tokenizer``.
    'embedder.py': frozenset({'numpy'}),
    'index.py': frozenset({'numpy', 'time'}),  # time: elapsed seconds of the run
    'search.py': frozenset({'numpy', 'time', 'gc'}),
    'store.py': frozenset({'time'}),  # M3: retry the WAL switch when two servers open a new store together
    # M4 (design M4 §10, appendix B): NFC before binding a quote and measuring text; knowledge_query.py
    # uses the common list only.
    'knowledge.py': frozenset({'unicodedata'}),
    # M5 (design M5 §9.3): setup writes config/client files from string templates and backs up the store
    # with ``open`` blocks, so it needs nothing beyond the common list (no ``shutil``, no ``subprocess``).
    'setup.py': frozenset(),
    # M5 remote (design M5 remote §6.1): the operating server's HTTP adapter receives through the SDK only
    # (auth settings, access token, Host/Origin checks, starlette request/response of the push routes) and
    # makes tokens (secrets, hmac); no uvicorn, socket, ssl or http import. gzip/base64 live in sync.py
    # (narrower than the design table, which also listed them here and in push.py).
    'remote.py': frozenset({'mcp.server.auth.settings.AuthSettings', 'mcp.server.auth.provider.AccessToken',
                            'mcp.server.transport_security.TransportSecuritySettings', 'starlette.requests.Request',
                            'starlette.responses.Response', 'starlette.responses.JSONResponse', 'secrets', 'hmac',
                            'time', 'logging'}),
    'push.py': frozenset({'time', 'logging'}),   # retry delays and the push log; urllib inside functions only
    'sync.py': frozenset({'gzip', 'base64'}),    # batch bodies and vector blobs, both sides
}
# Entries the stage-0 shells do not import yet (design M5 remote §11.1): exempt from the exactness test while
# unused; the test fails as soon as one is used, so stage 1 moves it out of here (and drops what stays unused).
STAGE1_PENDING = frozenset({('remote.py', 'starlette.responses.JSONResponse'), ('remote.py', 'time'),
                            ('push.py', 'time'), ('push.py', 'urllib.request'), ('push.py', 'urllib.error')})
# M5 (design M5 §12, model source): ``setup.download_model`` is the one caller of a models network
# function outside models.py, and only of ``fetch``; only ``cli.py`` may import ``setup`` at all, so the
# server and the tools never reach it. ``tools/`` lies outside the package and is not checked here;
# ``tests/test_tools_dir.py`` keeps it to the standard library without ``workmcp``.
MODELS_NETWORK_CALLERS = {'setup.py': frozenset({'fetch'})}
SETUP_IMPORTERS = frozenset({'cli.py'})
SETUP_DOTTED = 'workmcp.setup'
# Inside function bodies only: the explicit model download, and the ONNX runtime and tokenizer
# loaded in ``OnnxEmbedder.__init__`` (their DLLs never load on import, status or lexical search).
FUNCTION_IMPORTS = {'models.py': frozenset({'urllib.error', 'urllib.request'}),
                    'embedder.py': frozenset({'onnxruntime', 'tokenizers.Tokenizer'}),
                    'push.py': frozenset({'urllib.error', 'urllib.request'})}   # M5 remote §6.1
# Per-module transport of ``<x>.run(...)`` (design M5 remote §6.1): stdio in server.py, Streamable HTTP in
# remote.py with a loopback literal host and keywords from HTTP_RUN_KEYWORDS; no run call anywhere else.
ALLOWED_RUN = {'server.py': 'stdio', 'remote.py': 'streamable-http'}
HTTP_RUN_HOSTS = frozenset({'127.0.0.1', '::1'})
HTTP_RUN_KEYWORDS = frozenset({'transport', 'host', 'port', 'streamable_http_path', 'json_response', 'stateless_http',
                               'max_sessions', 'session_idle_timeout', 'transport_security', 'max_request_body_size'})
# Package modules each M5 module may not import (design M5 remote §6.1, §6.4): push never reaches code that
# reads originals; the server side and the wire schema stay apart from the collector's sender.
PACKAGE_IMPORT_BANS = {
    'push.py': frozenset({'collect', 'parsers', 'binding', 'receive', 'remote', 'server', 'tools', 'setup'}),
    'receive.py': frozenset({'push', 'remote', 'collect', 'parsers', 'binding', 'server', 'tools', 'setup'}),
    'sync.py': frozenset({'push', 'receive', 'remote', 'collect', 'parsers', 'binding', 'server', 'tools', 'setup',
                          'config'}),
    'remote.py': frozenset({'push', 'collect', 'parsers', 'setup'}),
}
# push.py: destinations come from config only (no URL literal) and no file is opened (the token comes through
# paths.read_token, the store through store.open_read).
URL_LITERAL_MODULES = frozenset({'push.py'})
URL_SCHEMES = ('http://', 'https://')
NO_FILE_MODULES = frozenset({'push.py'})
FILE_CALLS = frozenset({'open', 'read_text', 'read_bytes', 'fdopen', 'FileIO'})

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
# Hub download APIs, banned by name whatever they are called on (design M2 §7.1): tokenizers'
# ``Tokenizer.from_pretrained`` and huggingface_hub's helpers fetch files over the network.
HUB_DOWNLOADS = frozenset({'from_pretrained', 'hf_hub_download', 'snapshot_download'})
# anyio network and thread APIs, banned by name wherever they appear (design M3 §6.2): server.py may
# import anyio for its ticker only, and tools never run on a worker thread.
ANYIO_BANNED = frozenset({'open_tcp_stream', 'connect_tcp', 'create_tcp_listener', 'to_thread'})
# Heavy runtimes the import audit must not see loaded (lazy import, design M2 §7.2).
LAZY_RUNTIMES = ('onnxruntime', 'tokenizers', 'huggingface_hub')


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
    if parts[-1] in HUB_DOWNLOADS:
        return f'hub download API {dotted}'
    if ANYIO_BANNED & set(parts):
        return f'anyio network or thread API {dotted}'
    return None


def allowed_run(module: str, call: ast.Call) -> bool:
    """``<x>.run(...)`` with this module's transport only (``ALLOWED_RUN``): no positional, no ``**``, the
    literal ``transport``; stdio has no other keyword; Streamable HTTP needs a literal loopback ``host`` and
    takes keywords from ``HTTP_RUN_KEYWORDS`` only."""
    transport = ALLOWED_RUN.get(module)
    if transport is None or call.args or any(k.arg is None for k in call.keywords):
        return False
    keywords = {k.arg: k.value for k in call.keywords}
    value = keywords.get('transport')
    if not (isinstance(value, ast.Constant) and value.value == transport):
        return False
    if transport == 'stdio':
        return set(keywords) == {'transport'}
    host = keywords.get('host')
    return isinstance(host, ast.Constant) and host.value in HTTP_RUN_HOSTS and set(keywords) <= HTTP_RUN_KEYWORDS


def package_modules_imported(tree) -> set[str]:
    """First component of every package module a source imports (``from . import push``, ``from .parsers.pdf
    import x``, ``import workmcp.collect``, ``from workmcp import binding``) or reaches as ``workmcp.<name>``."""
    found = set()
    bound = bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split('.')[1] for a in node.names if a.name.startswith('workmcp.')}
        elif isinstance(node, ast.ImportFrom):
            package = node.level >= 1 or (node.module or '').split('.')[0] == 'workmcp'
            if not package:
                continue
            module = (node.module or '') if node.level else (node.module or '').removeprefix('workmcp').lstrip('.')
            if module:
                found.add(module.split('.')[0])
            else:
                found |= {a.name for a in node.names}
        elif isinstance(node, ast.Attribute):
            dotted = qualify(node, bound)
            if dotted and dotted.startswith('workmcp.'):
                found.add(dotted.split('.')[1])
    return found


def module_rule_violations(name: str, tree) -> list[str]:
    """The M5 per-module rules: banned package imports, URL literals and file access in push.py."""
    found = [f'imports package module {m} (not allowed in {name})'
             for m in sorted(package_modules_imported(tree) & PACKAGE_IMPORT_BANS.get(name, frozenset()))]
    bound = bindings(tree)
    for node in ast.walk(tree):
        if name in URL_LITERAL_MODULES and isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and any(scheme in node.value.lower() for scheme in URL_SCHEMES):
            found.append('URL literal (destinations come from config [push] url)')
        if name in NO_FILE_MODULES:
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FILE_CALLS:
                found.append(f'file access {node.func.id}(...)')
            elif isinstance(node, ast.Attribute) and node.attr in FILE_CALLS:
                found.append(f'file access .{node.attr}')
            elif isinstance(node, ast.Attribute) and qualify(node, bound) == 'os.open':
                found.append('file access os.open')
    return found


def setup_import_violations(name: str, tree) -> list[str]:
    """``setup`` (which reaches the model download) is imported by ``cli.py`` only, in any import form,
    through another package module (``cli.setup``) or ``getattr(..., "setup")``."""
    if name in SETUP_IMPORTERS:
        return []
    found = []
    bound = bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == SETUP_DOTTED or a.name.startswith(SETUP_DOTTED + '.') for a in node.names):
                found.append('imports the setup module (cli only)')
        elif isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            package = node.level >= 1 or (node.module or '').split('.')[0] == 'workmcp'
            module = (node.module or '') if node.level else (node.module or '').removeprefix('workmcp').lstrip('.')
            if package and (module == 'setup' or module.startswith('setup.')
                            or (module in ('', 'cli') and 'setup' in names)):
                found.append('imports the setup module (cli only)')
        elif isinstance(node, ast.Attribute) and node.attr == 'setup':
            dotted = qualify(node, bound)
            if dotted and is_package_name(dotted):
                found.append(f'setup module through {dotted} (cli only)')
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'getattr' \
                and len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and node.args[1].value == 'setup':
            found.append('getattr on the setup module (cli only)')
    return found


def violations(name: str, source: str, models_network: set[str] = frozenset()) -> list[str]:
    """``name`` is the path relative to the package (``models.py``, ``parsers/__init__.py``)."""
    tree = ast.parse(source)
    found = []
    models_network = set(models_network) - MODELS_NETWORK_CALLERS.get(name, frozenset())
    found += setup_import_violations(name, tree)
    found += module_rule_violations(name, tree)
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
            if node.id in HUB_DOWNLOADS:
                found.append(f'hub download API {node.id}')
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
            elif node.attr in DYNAMIC_LOADERS | ESCAPE_ATTRS | NON_STDIO_RUNNERS | HUB_DOWNLOADS | ANYIO_BANNED:
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
                if not allowed_run(name, node):
                    found.append('run(...) other than the transport of this module (ALLOWED_RUN)')
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
        'config.py', 'collect.py', 'binding.py', 'chunks.py', 'embedder.py', 'index.py', 'search.py', 'snippet.py',
        'setup.py', 'remote.py', 'push.py', 'receive.py', 'sync.py'}


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
        pending = {entry for owner, entry in STAGE1_PENDING if owner == module}
        names = {dotted for dotted, _, in_function in used[module] if not covered(dotted, COMMON_IMPORTS)
                 and not (in_function and covered(dotted, FUNCTION_IMPORTS.get(module, frozenset())))}
        assert not names & pending, f'{module}: now used, remove from STAGE1_PENDING: {sorted(names & pending)}'
        assert names == extra - pending, module
    for module, extra in FUNCTION_IMPORTS.items():
        pending = {entry for owner, entry in STAGE1_PENDING if owner == module}
        module_level = COMMON_IMPORTS | MODULE_IMPORTS.get(module, frozenset())
        names = {dotted for dotted, _, _ in used[module] if not covered(dotted, module_level)}
        assert not names & pending, f'{module}: now used, remove from STAGE1_PENDING: {sorted(names & pending)}'
        assert names == extra - pending, module
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
    assert not models_network() & {'entry', 'verify', 'quick_status', 'identity', 'sha256_file', 'require_verified',
                                   'import_from', 'available', 'cli_fetch_model', 'cli_verify_model',
                                   'file_url', '_range_start'}  # N-2; M5 URL and header helpers stay offline


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


# M2 (design M2 §7.1): the runtimes only inside embedder functions, no hub client, no hub download API.
M2_BYPASSES = [
    ('search.py', 'import onnxruntime'),
    ('search.py', 'def f():\n    import onnxruntime as ort'),
    ('index.py', 'from tokenizers import Tokenizer'),
    ('embedder.py', 'from onnxruntime import InferenceSession'),
    ('embedder.py', 'import onnxruntime as ort'),
    ('embedder.py', 'from tokenizers import Tokenizer'),
    ('embedder.py', 'import huggingface_hub'),
    ('index.py', 'def f():\n    import huggingface_hub'),
    ('search.py', 'from huggingface_hub import hf_hub_download'),
    ('embedder.py', 'def f():\n    from tokenizers import Tokenizer\n    return Tokenizer.from_pretrained("x")'),
    ('embedder.py', 'tokenizer = Tokenizer.from_pretrained("x")'),
    ('index.py', 'def f(t):\n    return t.from_pretrained("x")'),
    ('search.py', 'hf_hub_download("repo", "file")'),
    ('search.py', 'snapshot_download("repo")'),
    ('embedder.py', 'def f():\n    from tokenizers import Tokenizer\n    getattr(Tokenizer, "from_pretrained")("x")'),
    ('chunks.py', 'import numpy as np'),
    ('binding.py', 'import numpy'),
    ('collect.py', 'import numpy as np'),
    ('cli.py', 'import numpy as np'),
    ('tools.py', 'import numpy'),
    ('models.py', 'import numpy'),
    ('index.py', 'import gc'),
]


@pytest.mark.parametrize('module, snippet', M2_BYPASSES)
def test_checks_flag_m2_runtime_and_hub_snippets(module, snippet):
    assert violations(module, snippet, {'fetch', 'download_file', 'main'})


def test_m2_runtime_imports_are_allowed_in_their_functions_only():
    clean = ('import numpy as np\n'
             'def load(folder):\n'
             '    import onnxruntime as ort\n'
             '    from tokenizers import Tokenizer\n'
             '    tokenizer = Tokenizer.from_file(str(folder))\n'
             '    options = ort.SessionOptions()\n'
             '    return ort.InferenceSession("m.onnx", options, providers=["CPUExecutionProvider"]), tokenizer\n'
             'x = np.zeros(3).astype(np.float32)\n')
    assert violations('embedder.py', clean) == []
    assert violations('search.py', 'import numpy as np\nimport time\nimport gc\ngc.collect()\nt = time.monotonic\n') == []
    assert violations('index.py', 'import numpy as np\nnp.frombuffer(b"", dtype="<f4")\n') == []


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


# M5 (design M5 §12): setup may call models.fetch and nothing else of the download path; only cli imports setup.
SETUP_IMPORT_FORMS = [
    'from . import setup', 'from .setup import main', 'from .setup import download_model as d',
    'import workmcp.setup', 'import workmcp.setup as s', 'from workmcp import setup', 'from workmcp.setup import run',
    'from .cli import setup', 'from workmcp.cli import setup as s',
    'from . import cli\ncli.setup.main(a)', 'import workmcp\nworkmcp.setup.download_model("m", h)',
    'from . import cli\ngetattr(cli, "setup")', 'import workmcp\ngetattr(workmcp, "setup").main(a)',
    'def f():\n    from . import setup\n    return setup.download_model("m", h)',
]


@pytest.mark.parametrize('module', ['server.py', 'tools.py', 'search.py', 'models.py', 'setup.py'])
@pytest.mark.parametrize('snippet', SETUP_IMPORT_FORMS)
def test_only_cli_may_import_setup(module, snippet):
    assert violations(module, snippet, {'fetch', 'download_file', 'main'})


def test_setup_import_rule_in_a_subpackage_and_cli_is_allowed():
    assert violations('parsers/text.py', 'from .. import setup', {'fetch'})
    assert violations('parsers/text.py', 'from ..setup import run', {'fetch'})
    assert violations('cli.py', 'def f(a):\n    from . import setup\n    return setup.main(a)\n', {'fetch'}) == []
    assert violations('tools.py', 'import dataclasses\nx = dataclasses.field()\n', {'fetch'}) == []


@pytest.mark.parametrize('snippet, flagged', [
    ('from . import models\nmodels.fetch(name, home, log=log)', False),
    ('from . import models\nmodels.download_file("u", t, p, 1, "x")', True),
    ('from . import models\nmodels.main(["fetch"])', True),
    ('from .models import download_file', True),
    ('from . import models\ngetattr(models, "download_file")', True),
    ('from . import cli\ncli.models.main()', True),
])
def test_setup_may_reach_models_fetch_only(snippet, flagged):
    network = models_network()
    assert bool(violations('setup.py', snippet, network)) is flagged
    assert violations('search.py', snippet, network)  # every other module: still banned


def test_models_network_callers_match_the_sources_exactly():
    """Each allowed caller really uses exactly the functions it is allowed, as ``models.<name>``."""
    network = models_network()
    for module, allowed in MODELS_NETWORK_CALLERS.items():
        assert allowed <= network
        tree = ast.parse((PACKAGE/module).read_text(encoding='utf-8'))
        aliases = models_aliases(tree)
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id in aliases and n.attr in network}
        assert used == allowed
    importing = {rel(p) for p in package_sources() if setup_import_violations(  # judged as if not allowed
        'server.py', ast.parse(p.read_text(encoding='utf-8')))}
    assert importing == SETUP_IMPORTERS  # cli.py really imports setup, and nothing else does


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
    ticker = ('import anyio\nimport contextlib\nfrom mcp.server.mcpserver import Context\n'
              '@contextlib.asynccontextmanager\nasync def life(s):\n'
              '    async with anyio.create_task_group() as group:\n        group.start_soon(tick)\n'
              '        yield {}\n        group.cancel_scope.cancel()\n'
              'async def tick():\n    await anyio.sleep(30)\n')
    assert violations('server.py', ticker) == []  # design M3 §6.2: import anyio alone is allowed here
    assert violations('server.py', 'server.run(transport="sse")')
    assert violations('server.py', 'import anyio\nanyio.run(server.run_sse_async)')
    assert violations('server.py', 'import socket')
    assert violations('tools.py', 'from mcp.server import MCPServer')


# M3 (design M3 §6.2, appendix C): anyio, contextlib and Context only in server.py; no anyio network or
# thread API anywhere; no new process, thread or id modules.
M3_BYPASSES = [
    ('tools.py', 'import anyio'),
    ('search.py', 'import anyio'),
    ('snippet.py', 'import anyio'),
    ('tools.py', 'import contextlib'),
    ('snippet.py', 'import contextlib'),
    ('tools.py', 'from mcp.server.mcpserver import Context'),
    ('tools.py', 'import time'),
    ('server.py', 'from anyio import to_thread'),
    ('server.py', 'from anyio.to_thread import run_sync'),
    ('server.py', 'import anyio\nanyio.to_thread.run_sync(f)'),
    ('server.py', 'import anyio\nasync def g():\n    await anyio.to_thread.run_sync(f)'),
    ('server.py', 'import anyio as a\na.to_thread.run_sync(f)'),
    ('server.py', 'import anyio\ngetattr(anyio, "to_thread")'),
    ('server.py', 'import anyio\nanyio.open_tcp_stream("example.invalid", 443)'),
    ('server.py', 'from anyio import connect_tcp'),
    ('server.py', 'import anyio\nanyio.create_tcp_listener(local_port=8000)'),
    ('server.py', 'x.open_tcp_stream("example.invalid", 443)'),
    ('server.py', 'import anyio\nanyio.run(main)'),
    ('server.py', 'import asyncio'),
    ('server.py', 'import threading'),
    ('server.py', 'import uuid'),
    ('server.py', 'import subprocess'),
    ('server.py', 'import ctypes'),
    ('server.py', 'import logging.handlers'),
    ('tools.py', 'import uuid'),
    ('tools.py', 'import threading'),
]


@pytest.mark.parametrize('module, snippet', M3_BYPASSES)
def test_checks_flag_m3_snippets(module, snippet):
    assert violations(module, snippet, {'fetch', 'download_file', 'main'})


# M5 remote (design M5 remote §6.1): the thirteen positive controls of the design, then further forms.
M5_REMOTE_DESIGN_CONTROLS = [
    ('tools.py', 'from starlette.responses import JSONResponse'),
    ('remote.py', 'import uvicorn'),
    ('remote.py', 'server.run(transport="streamable-http", host="0.0.0.0")'),
    ('remote.py', 'server.run(transport="sse")'),
    ('server.py', 'server.run(transport="streamable-http")'),
    ('push.py', 'import urllib.request'),
    ('push.py', 'import ssl'),
    ('push.py', 'URL = "https://example.invalid"'),
    ('receive.py', 'from . import push'),
    ('push.py', 'from . import collect'),
    ('push.py', 'open(path, "rb")'),
    ('remote.py', 'import socket'),
    ('remote.py', 'from anyio import to_thread'),
]
M5_REMOTE_MORE_CONTROLS = [
    ('remote.py', 'server.run(transport="streamable-http")'),                        # no host
    ('remote.py', 'server.run(transport="streamable-http", host=host)'),             # host not a literal
    ('remote.py', 'server.run(transport="streamable-http", host="localhost")'),
    ('remote.py', 'server.run(transport="streamable-http", host="127.0.0.1", event_store=s)'),
    ('remote.py', 'server.run(transport="streamable-http", host="127.0.0.1", **extra)'),
    ('remote.py', 'server.run("streamable-http", host="127.0.0.1")'),
    ('remote.py', 'server.run(transport="stdio")'),
    ('remote.py', 'server.streamable_http_app()'),
    ('remote.py', 'server.run_streamable_http_async(host="127.0.0.1")'),
    ('remote.py', 'import ssl'),
    ('remote.py', 'import http.server'),
    ('remote.py', 'import threading'),
    ('remote.py', 'import httpx2'),
    ('remote.py', 'from starlette.applications import Starlette'),
    ('remote.py', 'from . import push'),
    ('remote.py', 'import anyio'),
    ('search.py', 'server.run(transport="stdio")'),                                   # stdio in server.py only
    ('tools.py', 'server.run(transport="streamable-http", host="127.0.0.1")'),
    ('server.py', 'server.run(transport="streamable-http", host="127.0.0.1")'),
    ('push.py', 'import urllib.parse'),
    ('push.py', 'def f():\n    import ssl'),
    ('push.py', 'def f():\n    import http.client'),
    ('push.py', 'def f():\n    import socket'),
    ('push.py', 'import gzip'),                                                       # in sync.py, not here
    ('push.py', 'def f():\n    return "http://127.0.0.1:8765/workmcp"'),
    ('push.py', 'def f(host):\n    return f"https://{host}/workmcp"'),
    ('push.py', '"""Sends to HTTPS://example.invalid."""'),
    ('push.py', 'from pathlib import Path\nPath(p).read_text()'),
    ('push.py', 'p.read_bytes()'),
    ('push.py', 'import os\nos.open(p, os.O_RDONLY)'),
    ('push.py', 'import io\nio.open(p)'),
    ('push.py', 'from . import parsers'),
    ('push.py', 'from .parsers import pdf'),
    ('push.py', 'from . import binding'),
    ('push.py', 'import workmcp.collect'),
    ('push.py', 'from workmcp import collect'),
    ('push.py', 'import workmcp\nworkmcp.collect.scan(db, r, s)'),
    ('push.py', 'from . import receive'),
    ('push.py', 'from . import remote'),
    ('receive.py', 'from .push import main'),
    ('receive.py', 'from . import remote'),
    ('receive.py', 'import gzip'),
    ('receive.py', 'def f():\n    import urllib.request'),
    ('sync.py', 'from . import push'),
    ('sync.py', 'from . import config'),
    ('sync.py', 'def f():\n    import urllib.request'),
    ('sync.py', 'import socket'),
    ('models.py', 'import gzip'),
]


def test_m5_remote_design_control_list_is_complete():
    assert len(M5_REMOTE_DESIGN_CONTROLS) == 13 and len(set(M5_REMOTE_DESIGN_CONTROLS)) == 13


@pytest.mark.parametrize('module, snippet', M5_REMOTE_DESIGN_CONTROLS + M5_REMOTE_MORE_CONTROLS)
def test_checks_flag_m5_remote_snippets(module, snippet):
    assert violations(module, snippet, {'fetch', 'download_file', 'main'})


def test_m5_remote_modules_may_use_what_their_role_needs():
    """Negative controls: the forms the stage-1 code is meant to use pass."""
    remote = ('from mcp.server.auth.settings import AuthSettings\n'
              'from mcp.server.auth.provider import AccessToken\n'
              'from mcp.server.transport_security import TransportSecuritySettings\n'
              'from starlette.requests import Request\nfrom starlette.responses import JSONResponse, Response\n'
              'import secrets\nimport hmac\nimport time\nimport logging\nfrom . import receive, server, sync\n'
              'def main(built, port, path, security):\n'
              '    built.run(transport="streamable-http", host="127.0.0.1", port=port, streamable_http_path=path,\n'
              '              json_response=True, stateless_http=False, max_sessions=16, session_idle_timeout=1800,\n'
              '              transport_security=security)\n'
              '    built.run(transport="streamable-http", host="::1", port=port)\n')
    assert violations('remote.py', remote) == []
    push = ('import logging\nimport time\nfrom . import config, paths, store, sync\n'
            'def send(url, data, token):\n'
            '    import urllib.error\n    import urllib.request\n'
            '    request = urllib.request.Request(sync.endpoint(url, "batch"), data=data, method="POST")\n'
            '    return urllib.request.urlopen(request, timeout=300)\n'
            'token = paths.read_token(home)\ndb = store.open_read(home)\n')
    assert violations('push.py', push) == []
    assert violations('sync.py', 'import gzip\nimport base64\nfrom . import chunks, store\n') == []
    assert violations('receive.py', 'from . import index, store, sync\n') == []
    assert violations('server.py', 'server.run(transport="stdio")') == []
    # the M5 rules are per module: models.py keeps its URLs, other modules may import collect as before
    assert violations('models.py', 'BASE = "https://huggingface.co"\n') == []
    assert violations('cli.py', 'from . import collect\n') == []


def test_package_import_detection_sees_every_form():
    forms = {'from . import push': {'push'}, 'from .parsers.pdf import x': {'parsers'},
             'import workmcp.collect': {'collect'}, 'from workmcp import binding, store': {'binding', 'store'},
             'from workmcp.receive import apply': {'receive'}, 'import workmcp\nworkmcp.remote.main()': {'remote'},
             'import json': set(), 'from mcp.server import MCPServer': set()}
    for source, expected in forms.items():
        assert package_modules_imported(ast.parse(source)) == expected, source


def test_allowed_run_follows_the_transport_table():
    call = lambda text: ast.parse(text).body[0].value  # noqa: E731
    assert allowed_run('server.py', call('s.run(transport="stdio")'))
    assert not allowed_run('remote.py', call('s.run(transport="stdio")'))
    assert allowed_run('remote.py', call('s.run(transport="streamable-http", host="127.0.0.1", port=p)'))
    assert allowed_run('remote.py', call('s.run(transport="streamable-http", host="::1")'))
    assert not allowed_run('remote.py', call('s.run(transport="streamable-http", host="0.0.0.0")'))
    assert not allowed_run('remote.py', call('s.run(transport="streamable-http", host="127.0.0.1", stray=1)'))
    assert not allowed_run('search.py', call('s.run(transport="stdio")'))
    assert set(ALLOWED_RUN) == {'server.py', 'remote.py'}


def test_run_calls_in_the_sources_are_the_allowed_ones():
    """server.py calls run(transport="stdio"), remote.py run(transport="streamable-http", loopback literal);
    no other source calls run at all."""
    calls = {}
    for path in package_sources():
        tree = ast.parse(path.read_text(encoding='utf-8'))
        found = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == 'run']
        if found:
            calls[rel(path)] = {next(k.value.value for k in n.keywords if k.arg == 'transport') for n in found}
    assert calls == {'server.py': {'stdio'}, 'remote.py': {'streamable-http'}}


# 4
def test_checks_accept_clean_snippet():
    clean = ('import os\nfrom . import models\nfrom . import models as md\nfrom .paths import home\nx = os.environ.get("A")\n'
             'models.require_verified("m")\nmd.identity("m")\ngetattr(md, "identity")\nimport re\nre.compile("x")\n'
             'from collections.abc import Mapping\nimport importlib.metadata\nimportlib.metadata.version("mcp")\n'
             'getattr(os.stat("."), "st_file_attributes", None)\n')
    assert violations('embedder.py', clean, {'fetch', 'download_file', 'main'}) == []


# 5 Runtime layer
def run_wrapper(tmp_path, mode, *extra, home=None, timeout=120):
    log = tmp_path/f'audit-{mode}.json'
    env = {**os.environ, 'WORKMCP_HOME': str(home or tmp_path/'home'), 'PYTHONPATH': str(ROOT/'src')}
    run = subprocess.run([sys.executable, str(WRAPPER), str(log), mode, *map(str, extra)], env=env, cwd=tmp_path,
                         capture_output=True, text=True, encoding='utf-8', timeout=timeout)
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
            'workmcp.binding', 'workmcp.chunks', 'workmcp.embedder', 'workmcp.index',
            'workmcp.search', 'workmcp.snippet', 'workmcp.setup', 'workmcp.remote', 'workmcp.push',
            'workmcp.receive', 'workmcp.sync'} <= set(report['modules'])
    assert report['remote_token_check'] is True  # remote.verify_token_sha ran under the hook (pure, offline)
    # setup.plan (dry run, M5 design §9.3) ran under the hook; stage 0 has its signature only
    from workmcp import setup
    assert report['setup_plan'] in ('not_implemented', sorted(setup.RESULT_KEYS))
    # the parser libraries and numpy really were imported under the hook (their import side effects are audited)
    assert {'pypdf', 'olefile', 'xml.parsers.expat', 'zipfile', 'email.parser', 'numpy'} <= set(report['loaded'])
    # ... and the heavy runtimes were not: they load inside OnnxEmbedder only (design M2 §7.2)
    assert not {name for name in report['loaded'] if name.split('.')[0] in LAZY_RUNTIMES}
    assert [e for e in report['events'] if e['violation']] == []
    assert not [e for e in report['events'] if e['event'].startswith(('socket.getaddrinfo', 'socket.gethostbyname'))]


def stage_skip(report):
    if report.get('skipped'):
        pytest.skip(f"audit mode {report['mode']}: {report['skipped']}")


def test_index_path_stays_inside_the_runtime_boundary(tmp_path):
    """scan -> index (HashEmbedder) -> index again: no network, no process, no source write or open."""
    src = tmp_path/'src'
    bundle.write_bundle(src)
    _, report = run_wrapper(tmp_path, 'index', src)
    stage_skip(report)
    assert [e for e in report['events'] if e['violation']] == []
    assert report['source_opens_during_index'] == 0
    assert report['second']['chunked'] == 0 and report['second']['embedded'] == 0
    assert report['fts_rows'] == report['chunks'] > 0


def test_search_path_stays_inside_the_runtime_boundary_and_is_read_only(tmp_path):
    src = tmp_path/'src'
    bundle.write_bundle(src)
    _, report = run_wrapper(tmp_path, 'search', src)
    stage_skip(report)
    assert [e for e in report['events'] if e['violation']] == []
    assert report['source_opens_during_search'] == 0
    assert report['connects'] and all('mode=ro' in uri for uri in report['connects'])
    assert report['hits'] > 0


def test_embed_path_stays_inside_the_runtime_boundary(tmp_path):
    """The real model, loaded and run under the hook, from the data home that has it
    (``tests/_real_model.py``: ``WORKMCP_TEST_MODEL_HOME`` or the default data home); skipped without it."""
    import _real_model
    home = _real_model.real_model_home()
    if home is None:
        pytest.skip(_real_model.SKIP_REASON)
    _, report = run_wrapper(tmp_path, 'embed', home=home, timeout=600)
    stage_skip(report)
    assert report['dims'] == 1024 and report['unit_norms'] is True
    assert report['hub_modules_loaded'] == []
    assert [e for e in report['events'] if e['violation']] == []
    assert report['providers'] == ['CPUExecutionProvider']


def test_tokenizers_and_onnxruntime_imports_do_not_load_a_hub_client():
    """tokenizers depends on huggingface_hub in the lock (transitive, allowed); importing it must not load
    it or any HTTP client (design M2 §7.1)."""
    code = ('import sys, json\n'
            'import numpy, tokenizers, onnxruntime\n'
            'from tokenizers import Tokenizer\n'
            'print(json.dumps(sorted(m for m in sys.modules if m.split(".")[0] in '
            '("huggingface_hub", "requests", "httpx", "urllib3", "hf_xet", "fsspec", "aiohttp"))))\n')
    run = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == []


SERVE_TOOLS = {'search', 'get_snippet', 'list_documents', 'recent_changes', 'status',
               'record_entity', 'record_fact', 'record_decision', 'record_task', 'supersede', 'retract',
               'find_entity', 'get_entity', 'list_knowledge', 'get_history'}
# Arguments of each tool's input schema (design M3 §2, M4 §6); ctx is injected by the SDK, not an argument.
EVIDENCE_ARGUMENTS = {'sources', 'my_note'}
SERVE_ARGUMENTS = {
    'search': {'query', 'mode', 'limit', 'path_prefix', 'ext', 'since', 'until', 'collapse_by_document'},
    'get_snippet': {'chunk_id', 'locator', 'context_units', 'max_chars'},
    'list_documents': {'path_prefix', 'ext', 'status', 'changed_since', 'limit', 'offset'},
    'recent_changes': {'since', 'until', 'kinds', 'path_prefix', 'ext', 'limit', 'offset'},
    'status': set(),
    'record_entity': {'kind', 'name', 'aliases', 'note'},
    'record_fact': {'entity_id', 'predicate', 'value', 'unit', 'object_entity_id', 'statement', 'valid_from',
                    'valid_to'} | EVIDENCE_ARGUMENTS,
    'record_decision': {'statement', 'entity_id', 'options', 'chosen', 'rationale', 'decided_on',
                        'valid_to'} | EVIDENCE_ARGUMENTS,
    'record_task': {'statement', 'entity_id', 'due', 'owner_entity_id', 'state', 'done_at'} | EVIDENCE_ARGUMENTS,
    'supersede': {'assertion_id', 'reason', 'old_valid_to', 'entity_id', 'predicate', 'value', 'unit',
                  'object_entity_id', 'statement', 'valid_from', 'valid_to', 'options', 'chosen', 'rationale',
                  'decided_on', 'due', 'owner_entity_id', 'state', 'done_at'} | EVIDENCE_ARGUMENTS,
    'retract': {'assertion_id', 'reason'},
    'find_entity': {'query', 'kind', 'limit'},
    'get_entity': {'entity_id', 'kind', 'name', 'as_of_valid', 'as_of_recorded', 'include_closed', 'source_kind',
                   'limit'},
    'list_knowledge': {'kind', 'entity_id', 'predicate', 'status', 'state', 'due_before', 'source_kind',
                       'as_of_valid', 'as_of_recorded', 'recorded_since', 'limit', 'offset'},
    'get_history': {'assertion_id'},
}


def tool_call(number, name, arguments):
    return {'jsonrpc': '2.0', 'id': number, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}


CLIENT_NAME = 'audit-test'


def initialize(number=1):
    return {'jsonrpc': '2.0', 'id': number, 'method': 'initialize',
            'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                       'clientInfo': {'name': CLIENT_NAME, 'version': '0'}}}


def filled_home(tmp_path) -> tuple[Path, Path, dict]:
    """The synthetic bundle scanned into ``tmp_path/home`` and indexed (``HashEmbedder``) by this test
    process, before ``serve`` starts; returns ``(home, source root, first lexical hit for SENTINEL)``."""
    from test_collect import add_root, scan
    from workmcp import embedder, index, paths, search, store
    src = tmp_path/'src'
    bundle.write_bundle(src)
    home = paths.ensure_home(paths.validate_home(str(tmp_path/'home'), {}, None))
    db = store.open_home(home)
    try:
        scan(db, add_root(db, src))
        index.run_index(db, home, embedder=embedder.HashEmbedder(64))
    finally:
        db.close()
    hits = search.Engine(home).search(SENTINEL, limit=5)['hits']  # no model files: lexical, as serve answers
    assert hits
    return home, src, hits[0]


def mcp_session(hit: dict) -> list[dict]:
    """The serve audit session (design M3 §12): every tool, two error envelopes, one SDK validation error."""
    return [
        initialize(),
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        tool_call(3, 'status', {}),
        tool_call(4, 'search', {'query': SENTINEL, 'limit': 5}),
        tool_call(5, 'search', {'query': SENTINEL, 'mode': 'semantic', 'limit': 5}),  # no model: lexical + note
        tool_call(6, 'get_snippet', {'chunk_id': hit['chunk_id']}),
        tool_call(7, 'get_snippet', {'locator': hit['locator'], 'context_units': 2}),
        tool_call(8, 'list_documents', {'limit': 3}),
        tool_call(9, 'recent_changes', {}),
        tool_call(10, 'search', {'query': ''}),          # our error envelope: empty_query
        tool_call(11, 'get_snippet', {}),                # our error envelope: argument_invalid
        tool_call(12, 'search', {'query': 'x', 'limit': 'x'}),  # SDK argument validation: isError, not our envelope
    ]


SERVE_IDS = set(range(1, 13))
SERVE_CALLED = {3: 'status', 4: 'search', 5: 'search', 6: 'get_snippet', 7: 'get_snippet', 8: 'list_documents',
                9: 'recent_changes', 10: 'search', 11: 'get_snippet'}  # the calls our code handles (not 12)
SERVE_ERRORS = {10: 'empty_query', 11: 'argument_invalid'}
SERVE_DELIVERING = {4, 5, 6, 7}                       # the calls whose answers carry document text

# The knowledge part of the session (design M4 §10): write -> read -> supersede -> retract, then three
# refused writes. Steps are callables of the replies so far (ids come from earlier answers). Every text
# argument a client writes carries SENTINEL, which must never reach stderr or tool_calls.args_json.
KNOWLEDGE_CALLED = {13: 'record_entity', 14: 'record_fact', 15: 'record_fact', 16: 'record_fact',
                    17: 'record_decision', 18: 'record_task', 19: 'find_entity', 20: 'get_entity',
                    21: 'list_knowledge', 22: 'supersede', 23: 'retract', 24: 'get_history',
                    25: 'record_fact', 26: 'record_fact', 27: 'record_fact'}
KNOWLEDGE_ERRORS = {25: 'evidence_quote_not_found', 26: 'evidence_required', 27: 'chunk_not_found'}
KNOWLEDGE_DELIVERING = {14, 15, 17, 20, 22, 24}       # stored quotes come back: writes with sources, details
ENTITY_NAME = f'합성 과제 {SENTINEL}'
PREDICATE = '담당'


def answer(replies: dict, number: int) -> dict:
    return json.loads(replies[number]['result']['content'][0]['text'])


def knowledge_session(hit: dict) -> list:
    """Steps 13-27 after ``mcp_session``; ``hit['snippet']`` is quoted (it is the text under ``hit['locator']``
    and occurs once in the hit's chunk, which ``unique_in_chunk`` checks before the session)."""
    quote = hit['snippet']

    def entity(replies):
        return answer(replies, 13)['result']['entity']['id']

    def fact(replies, **extra):
        return {'entity_id': entity(replies), 'predicate': PREDICATE, 'value': f'{SENTINEL} 값', **extra}

    def assertion(replies, number, key='assertion'):
        return answer(replies, number)['result'][key]['id']

    by_chunk = [{'chunk_id': hit['chunk_id'], 'quote': quote}]
    by_locator = [{'locator': hit['locator'], 'quote': quote}]
    return [
        tool_call(13, 'record_entity', {'kind': 'project', 'name': ENTITY_NAME, 'aliases': ['과제P']}),
        lambda r: tool_call(14, 'record_fact', fact(r, sources=by_chunk)),
        lambda r: tool_call(15, 'record_fact', fact(r, sources=by_locator)),                 # same value: duplicate
        lambda r: tool_call(16, 'record_fact', fact(r, value='합성 다른 값', my_note=True)),    # another value: conflict
        lambda r: tool_call(17, 'record_decision', {'statement': f'합성 결정 {SENTINEL}', 'entity_id': entity(r),
                                                    'options': ['가안', '나안'], 'chosen': '가안',
                                                    'sources': by_locator}),
        lambda r: tool_call(18, 'record_task', {'statement': f'합성 할 일 {SENTINEL}', 'entity_id': entity(r),
                                                'due': '2026-11-01', 'my_note': True}),
        tool_call(19, 'find_entity', {'query': '합성 과제'}),
        lambda r: tool_call(20, 'get_entity', {'entity_id': entity(r)}),
        tool_call(21, 'list_knowledge', {'kind': 'fact'}),
        lambda r: tool_call(22, 'supersede', {'assertion_id': assertion(r, 14),
                                              'statement': f'합성 교정 문장 {SENTINEL}',     # same value
                                              'reason': f'합성 교정 {SENTINEL}', 'sources': by_locator}),
        lambda r: tool_call(23, 'retract', {'assertion_id': assertion(r, 16), 'reason': f'합성 철회 {SENTINEL}'}),
        lambda r: tool_call(24, 'get_history', {'assertion_id': assertion(r, 22, 'new')}),
        lambda r: tool_call(25, 'record_fact', fact(r, sources=[{'chunk_id': hit['chunk_id'],
                                                                 'quote': f'없는 문장 {SENTINEL}'}])),
        lambda r: tool_call(26, 'record_fact', fact(r)),                                      # neither: refused
        lambda r: tool_call(27, 'record_fact', fact(r, sources=[{'chunk_id': 999_999, 'quote': f'{SENTINEL} xx'}])),
    ]


def unique_in_chunk(home: Path, hit: dict) -> None:
    """The session quotes ``hit['snippet']`` by chunk id: it must occur exactly once in the chunk's units."""
    from workmcp import store
    db = store.open_read(home)
    try:
        version, first, last = db.execute('SELECT version_id, unit_first, unit_last FROM chunks WHERE id = ?',
                                          (hit['chunk_id'],)).fetchone()
        texts = [row[0] for row in db.execute('SELECT text FROM units WHERE version_id = ? AND ordinal BETWEEN ? AND ?',
                                              (version, first, last))]
    finally:
        db.close()
    assert sum(text.count(hit['snippet']) for text in texts) == 1


def snapshot(root: Path) -> dict:
    """Size, mtime_ns and SHA-256 of every file under ``root`` (the originals must not change)."""
    import hashlib
    return {str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns,
                                          hashlib.sha256(path.read_bytes()).hexdigest())
            for path in sorted(root.rglob('*')) if path.is_file()}


def serve_session(tmp_path, steps, expected_ids, timeout=120, root=None):
    """Run the wrapper in ``serve [ROOT]`` mode and drive it one message at a time: a step is a message or a
    callable of the replies so far; after a request its reply is awaited before the next step. Then stdin
    is closed. Returns every stdout line, stderr and the audit report."""
    log = tmp_path/'audit-serve.json'
    env = {**os.environ, 'WORKMCP_HOME': str(tmp_path/'home'), 'PYTHONPATH': str(ROOT/'src')}
    extra = [str(root)] if root is not None else []
    proc = subprocess.Popen([sys.executable, str(WRAPPER), str(log), 'serve', *extra], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=tmp_path,
                            text=True, encoding='utf-8')
    watchdog = threading.Timer(timeout, proc.kill)
    watchdog.start()
    try:
        lines, replies = [], {}
        for step in steps:
            message = step(replies) if callable(step) else step
            proc.stdin.write(json.dumps(message) + '\n')
            proc.stdin.flush()
            while 'id' in message and message['id'] not in replies:
                line = proc.stdout.readline()
                if not line:
                    break  # server exited or was killed by the watchdog
                lines.append(line)
                if line.strip().startswith('{') and (reply := json.loads(line)).get('id') is not None:
                    replies[reply['id']] = reply
            if 'id' in message and message['id'] not in replies:
                break
        proc.stdin.close()
        rest, stderr = proc.stdout.read(), proc.stderr.read()
        code = proc.wait()
    finally:
        watchdog.cancel()
        proc.kill()
    lines += rest.splitlines(keepends=True)
    assert code == 0, stderr  # (d)
    assert expected_ids <= set(replies), stderr
    assert log.is_file(), stderr
    report = json.loads(log.read_text(encoding='utf-8'))
    assert report['completed'] is True, stderr
    return lines, stderr, report


def body_pieces(node) -> list[tuple[dict, str]]:
    """Every ``(locator, text)`` of document text in an answer, found independently of ``tools.pieces_of``: a
    dict with a ``locator`` and one of ``snippet``/``text``/``quote`` as a string, in JSON order."""
    found = []
    if isinstance(node, dict):
        texts = [node[key] for key in ('snippet', 'text', 'quote') if isinstance(node.get(key), str)]
        if isinstance(node.get('locator'), dict) and len(texts) == 1:
            found.append((node['locator'], texts[0]))
        for value in node.values():
            found += body_pieces(value)
    elif isinstance(node, list):
        for item in node:
            found += body_pieces(item)
    return found


def test_serve_path_stays_inside_the_runtime_boundary(tmp_path):
    """Real ``serve`` over stdio on a scanned and indexed synthetic home (design M3 §12, M4 §10): stdout is
    JSON-RPC only, sockets loopback only, no name resolution, no process, no open (any mode) under the
    source root and no change to any original, exit 0; fifteen unstructured tools answer with envelopes
    whose delivered_sources are exactly the document text they carry, and every such text is the stored
    text under its locator; every call our code handles is logged once under the client's name, one
    deliveries row per piece, and no query, statement, value, quote, name or reason in the log or stderr;
    the knowledge part (write -> read -> supersede -> retract) leaves the rows ``check_knowledge_rows`` expects."""
    from workmcp import binding, locator, store
    home, src, hit = filled_home(tmp_path)
    unique_in_chunk(home, hit)
    steps = mcp_session(hit) + knowledge_session(hit)
    called, errors = SERVE_CALLED | KNOWLEDGE_CALLED, SERVE_ERRORS | KNOWLEDGE_ERRORS
    delivering = SERVE_DELIVERING | KNOWLEDGE_DELIVERING
    expected_ids = SERVE_IDS | set(called)
    before = snapshot(src)
    lines, stderr, report = serve_session(tmp_path, steps, expected_ids, root=src)
    assert snapshot(src) == before                                            # (c) originals unchanged
    stdout = ''.join(lines)
    assert stdout.endswith('\n'), stdout[-200:]  # every message, the last one too, ends with a newline
    records = stdout[:-1].split('\n')  # only that final newline is dropped; blank lines stay and fail below
    assert all(record.strip() for record in records), records  # (a) no blank or whitespace-only line ...
    messages = [json.loads(record) for record in records]  # ... every stdout line parses ...
    assert all(isinstance(m, dict) and m.get('jsonrpc') == '2.0' for m in messages)  # ... as a JSON-RPC object
    replies = {m['id']: m for m in messages if 'id' in m}
    assert set(replies) == expected_ids, stderr
    assert replies[1]['result']['serverInfo']['name'] == 'workmcp'
    assert 'delivered_sources' in replies[1]['result']['instructions']
    listed = {tool['name']: tool for tool in replies[2]['result']['tools']}   # (d)
    assert set(listed) == SERVE_TOOLS
    assert {name: set(tool['inputSchema'].get('properties', {})) for name, tool in listed.items()} == SERVE_ARGUMENTS
    assert all('outputSchema' not in tool for tool in listed.values())  # structured_output=False
    envelopes = {}
    for number in called:                                                     # (e)
        result = replies[number]['result']
        assert 'structuredContent' not in result and len(result['content']) == 1 and not result.get('isError')
        envelopes[number] = json.loads(result['content'][0]['text'])
    for number, code in errors.items():                                       # (i)
        assert envelopes[number] == {'ok': False, 'error': {'code': code, 'message': envelopes[number]['error'][
            'message'], 'retryable': False}}, (number, envelopes[number])
    for number in set(called) - set(errors):
        assert envelopes[number]['ok'] is True, (number, envelopes[number])
    status = envelopes[3]['result']
    assert status['schema_version'] == 5 and set(status) == STATUS_RESULT_KEYS
    assert (status['role'], status['collectors'], status['last_push']) == ('local', [], None)
    assert status['home'] == str(home) and status['db']['chunks'] > 0
    assert set(status['home_virtualization']) == {'checked', 'under_appdata', 'redirected_to', 'package'}
    hits = envelopes[4]['result']['hits']
    assert hits and hits[0]['chunk_id'] == hit['chunk_id'] and hits[0]['locator'] == hit['locator']
    assert envelopes[5]['result']['meta']['mode'] == 'lexical'
    assert envelopes[5]['result']['meta']['semantic_note'].startswith('model_missing')
    assert envelopes[6]['result']['chunk']['id'] == hit['chunk_id']
    assert envelopes[7]['result']['pieces'][0] == {'locator': hit['locator'], 'text': hit['snippet'],
                                                   'truncated': False}
    db = sqlite3.connect(f'{(home/"db"/"work.sqlite3").as_uri()}?mode=ro', uri=True)
    try:
        pieces = 0
        for number, envelope in envelopes.items():                            # (f)
            if not envelope['ok']:
                assert 'delivered_sources' not in envelope
                continue
            found = body_pieces(envelope['result'])
            assert envelope['delivered_sources'] == [locator.display(obj) for obj, _ in found], number
            assert bool(found) == (number in delivering), number
            for obj, text in found:                                           # the text is the stored text
                assert binding.validate_locator(db, obj)['text'] == text, number
            pieces += len(found)
        calls = db.execute('SELECT client, client_version, tool, ok, error_code, args_json, call_id FROM tool_calls '
                           'ORDER BY id').fetchall()                          # (g)
        deliveries = db.execute('SELECT client, COUNT(*) FROM deliveries GROUP BY client').fetchall()
        check_knowledge_rows(db, envelopes, calls)
    finally:
        db.close()
    assert replies[12]['result']['isError'] is True  # (X-2) rejected by the SDK before our code runs
    assert 'limit' in replies[12]['result']['content'][0]['text']
    assert report['source_opens'] == 0  # (c) the tools never open the originals, for reading or writing
    assert sorted((c[2], c[3], c[4]) for c in calls) == sorted(
        (tool, 0 if number in errors else 1, errors.get(number)) for number, tool in called.items())
    assert {(c[0], c[1]) for c in calls} == {(CLIENT_NAME, '0')}
    assert deliveries == [(CLIENT_NAME, pieces)] and pieces > 0
    assert SENTINEL not in stderr and not any(SENTINEL in c[5] for c in calls)  # (h) no query or knowledge text
    assert 'app virtualization' in stderr  # the start line (design M3 §10.3)
    events = report['events']
    assert [e for e in events if e['violation']] == []  # (b) loopback only, no name resolution, no source write
    for e in events:
        if e['event'] in _audited_server.ADDRESS_EVENTS:
            assert re.search(r"\('(127\.0\.0\.1|::1)', \d+", e['detail']), e
    binds = [e for e in events if e['event'] == 'socket.bind']
    assert all(re.search(r"\('(127\.0\.0\.1|::1)', 0[,)]", e['detail']) for e in binds), binds  # port 0 only
    assert not [e for e in events if e['event'].startswith(_audited_server.PROCESS_EVENTS)]  # (c)
    assert not [e for e in events if e['event'].startswith(('socket.getaddrinfo', 'socket.gethostbyname'))]


def check_knowledge_rows(db, envelopes: dict, calls: list) -> None:
    """The store after the knowledge part (design M4 §10 (e), (h)): what each step wrote, by whom, under
    which call; every evidence row re-derives from the stored text; the client's text is in the knowledge
    tables (that is the knowledge base) and nowhere in the call log."""
    from workmcp import binding
    ids = {number: envelopes[number]['result']['assertion']['id'] for number in (14, 15, 16, 17, 18)}
    ids[22] = envelopes[22]['result']['new']['id']
    assert envelopes[22]['result']['old']['id'] == ids[14]
    entity = envelopes[13]['result']['entity']['id']
    assert envelopes[13]['result']['created'] is True
    assert envelopes[15]['result']['duplicates'] == [ids[14]]
    assert sorted(c['id'] for c in envelopes[16]['result']['conflicts']) == sorted([ids[14], ids[15]])
    # the correction keeps the value (a duplicate of 15) and conflicts with the note 16; retracting 16
    # resolves its conflicts and 15 and the correction become active again
    assert envelopes[22]['result']['duplicates'] == [ids[15]]
    assert [c['id'] for c in envelopes[22]['result']['conflicts']] == [ids[16]]
    assert envelopes[23]['result']['assertion']['status'] == 'withdrawn'
    assert sorted(envelopes[23]['result']['cleared_conflicts']) == sorted([ids[15], ids[22]])
    assert [row['id'] for row in envelopes[24]['result']['chain']] == [ids[14], ids[22]]
    assert entity in [e['id'] for e in envelopes[19]['result']['entities']]
    assert envelopes[21]['result']['items'] and all(item['evidence'] is None for item in envelopes[21]['result']['items'])
    call_of = {}                                       # step number -> its tool_calls.call_id, in order
    handled = sorted(n for n in envelopes)
    assert len(calls) == len(handled)
    for number, row in zip(handled, calls):
        call_of[number] = row[6]
    rows = {row[0]: row[1:] for row in db.execute('SELECT id, kind, status, recorded_by, source_kind FROM assertions')}
    assert rows == {
        ids[14]: ('fact', 'superseded', f'{CLIENT_NAME}/record_fact', 'evidence'),
        ids[15]: ('fact', 'active', f'{CLIENT_NAME}/record_fact', 'evidence'),
        ids[16]: ('fact', 'withdrawn', f'{CLIENT_NAME}/record_fact', 'my_note'),
        ids[17]: ('decision', 'active', f'{CLIENT_NAME}/record_decision', 'evidence'),
        ids[18]: ('todo', 'active', f'{CLIENT_NAME}/record_task', 'my_note'),
        ids[22]: ('fact', 'active', f'{CLIENT_NAME}/supersede', 'evidence'),
    }
    assert db.execute('SELECT name, created_by FROM entities').fetchall() == [(ENTITY_NAME,
                                                                              f'{CLIENT_NAME}/record_entity')]
    evidence = db.execute('SELECT assertion_id, locator_json, quote FROM evidence ORDER BY id').fetchall()
    assert sorted(row[0] for row in evidence) == sorted([ids[14], ids[15], ids[17], ids[22]])
    for _, locator_json, quote in evidence:                       # every stored source re-derives
        assert binding.validate_locator(db, json.loads(locator_json))['text'] == quote
    kinds = {row[0] for row in db.execute('SELECT kind FROM assertion_events')}
    assert kinds == {'recorded', 'superseded', 'retracted', 'conflict_marked', 'conflict_cleared'}
    assert {row[0] for row in db.execute('SELECT relation FROM links')} == {'supersedes', 'conflicts'}
    event_calls = {row[0]: row[1] for row in db.execute(
        "SELECT kind, call_id FROM assertion_events WHERE assertion_id = ? AND kind IN ('recorded', 'superseded')",
        (ids[14],))}
    assert event_calls == {'recorded': call_of[14], 'superseded': call_of[22]}
    texts = ' '.join(str(value) for row in db.execute(
        'SELECT a.statement, a.data_json, e.reason FROM assertions a LEFT JOIN assertion_events e '
        'ON e.assertion_id = a.id') for value in row)
    assert SENTINEL in texts and ENTITY_NAME in db.execute('SELECT name FROM entities').fetchone()[0]


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


SERVER_PORT = 48765
TARGET = _audited_server.PushTarget(host='127.0.0.1', port=SERVER_PORT, addresses=frozenset({'127.0.0.1'}),
                                    url_prefix=f'http://127.0.0.1:{SERVER_PORT}/workmcp/')


@pytest.mark.parametrize('event, args, outside', [
    ('socket.bind', (None, ('127.0.0.1', SERVER_PORT)), False),       # its own listener
    ('socket.bind', (None, ('::1', SERVER_PORT, 0, 0)), False),
    ('socket.bind', (None, ('127.0.0.1', 0)), False),                 # the event loop's socket pair
    ('socket.bind', (None, ('127.0.0.1', SERVER_PORT + 1)), True),    # another fixed port
    ('socket.bind', (None, ('0.0.0.0', SERVER_PORT)), True),
    ('socket.bind', (None, ('', SERVER_PORT)), True),
    ('socket.getaddrinfo', ('127.0.0.1', SERVER_PORT, 0, 1, 6, 0), False),
    ('socket.getaddrinfo', (b'::1', SERVER_PORT, 0, 1, 6, 0), False),
    ('socket.getaddrinfo', ('localhost', SERVER_PORT, 0, 1, 6, 0), True),   # a name: resolution
    ('socket.getaddrinfo', ('huggingface.co', 443, 0, 1, 6, 0), True),
    ('socket.connect', (None, ('127.0.0.1', 50000)), False),
    ('socket.connect', (None, ('203.0.113.1', 443)), True),           # egress
    ('http.client.connect', (None, 'example.invalid', 443), True),
    ('urllib.Request', ('https://example.invalid', None, {}, 'GET'), True),
    ('subprocess.Popen', ('curl', ['curl'], None, None), True),
    ('open', ('x', 'r', 0), False),
])
def test_remote_judge_allows_its_listener_and_nothing_outside(event, args, outside):
    assert (_audited_server.judge_remote(event, args, SERVER_PORT) is not None) is outside


@pytest.mark.parametrize('event, args, outside', [
    ('socket.getaddrinfo', ('127.0.0.1', SERVER_PORT, 0, 1, 6, 0), False),
    ('socket.getaddrinfo', ('127.0.0.1', str(SERVER_PORT), 0, 1, 6, 0), False),
    ('socket.getaddrinfo', ('127.0.0.1', 443, 0, 1, 6, 0), True),     # another port
    ('socket.getaddrinfo', ('example.invalid', SERVER_PORT, 0, 1, 6, 0), True),
    ('socket.connect', (None, ('127.0.0.1', SERVER_PORT)), False),
    ('socket.connect', (None, ('127.0.0.1', 50000)), True),           # loopback, but not the server
    ('socket.connect', (None, ('203.0.113.1', SERVER_PORT)), True),
    ('socket.bind', (None, ('127.0.0.1', SERVER_PORT)), True),        # a collector listens on nothing fixed
    ('http.client.connect', (None, '127.0.0.1', SERVER_PORT), False),
    ('http.client.connect', (None, 'example.invalid', SERVER_PORT), True),
    ('http.client.connect', (None, '127.0.0.1', 80), True),
    ('http.client.send', (None, b'POST /workmcp/push/batch'), False),  # on the checked connection
    ('urllib.Request', (f'http://127.0.0.1:{SERVER_PORT}/workmcp/push/state', None, {}, 'GET'), False),
    ('urllib.Request', (f'http://127.0.0.1:{SERVER_PORT}/workmcpx/push/state', None, {}, 'GET'), True),
    ('urllib.Request', ('https://example.invalid/workmcp/push/state', None, {}, 'GET'), True),
    ('socket.gethostbyname', ('example.invalid',), True),
    ('subprocess.Popen', ('curl', ['curl'], None, None), True),
    ('open', ('x', 'r', 0), False),
])
def test_push_judge_allows_the_configured_server_only(event, args, outside):
    assert (_audited_server.judge_push(event, args, TARGET) is not None) is outside


@pytest.mark.parametrize('mode', ['remote', 'push'])
def test_m5_audit_modes_are_stage_1(tmp_path, mode):
    """Stage 0 has the judges; the remote and push runs report skipped until stage 1 fills them in."""
    _, report = run_wrapper(tmp_path, mode)
    assert report['skipped'].startswith('not_implemented')
    assert [e for e in report['events'] if e['violation']] == []


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
STATUS_RESULT_KEYS = {'home', 'schema_version', 'db', 'model', 'fts5', 'last_scan', 'index', 'delivery_log_count',
                      'engine', 'home_virtualization', 'calls', 'process', 'knowledge', 'role', 'collectors',
                      'last_push'}


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


def test_embed_mode_reads_the_home_and_never_creates_it(tmp_path):
    """Review M2-L4: the ``embed`` audit mode may point at the user's data home, so it only reads it."""
    home = tmp_path/'not-created-home'
    _, report = run_wrapper(tmp_path, 'embed', home=home)
    assert report['skipped'] == 'model_missing'
    assert not home.exists()

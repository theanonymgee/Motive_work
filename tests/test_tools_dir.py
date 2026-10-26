"""``tools/`` holds the tools that run outside the package (design M5 §2.1, §3.2, §9.3): the bootstrap
``install.py`` runs before the package exists and ``make_bundle.py`` builds the offline bundle, so both
use the standard library only and never import ``workmcp`` (in any form, also not dynamically). They
are outside ``src/workmcp``, so the package network boundary (``test_network_boundary.py``) does not
apply to them; this test is their rule.
"""
import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT/'tools'
DYNAMIC_IMPORTS = frozenset({'__import__', 'import_module', 'run_module', 'run_path', 'spec_from_file_location',
                             'module_from_spec', 'exec_module', 'load_module'})


def tool_sources() -> list[Path]:
    return sorted(TOOLS.rglob('*.py'))


def tool_violations(source: str) -> list[str]:
    """Reasons a tool source breaks the rule: a non-stdlib or ``workmcp`` import (module level or inside a
    function), a relative import or a dynamic import. Running the installed package in a child process
    (``python -I -m workmcp --help``, design M5 §2.2 step 4) is not an import and stays allowed."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split('.')[0]
                if top not in sys.stdlib_module_names:
                    found.append(f'not standard library: {alias.name}')
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.append(f'relative import (level {node.level})')
            elif (node.module or '').split('.')[0] not in sys.stdlib_module_names:
                found.append(f'not standard library: {node.module}')
        elif isinstance(node, ast.Name) and node.id in DYNAMIC_IMPORTS:
            found.append(f'dynamic import {node.id}')
        elif isinstance(node, ast.Attribute) and node.attr in DYNAMIC_IMPORTS:
            found.append(f'dynamic import {node.attr}')
    return found


def test_tools_folder_exists_outside_the_package():
    assert TOOLS.is_dir()
    assert not TOOLS.is_relative_to(ROOT/'src')


def test_tools_use_the_standard_library_only_and_never_workmcp():
    offenders = {p.relative_to(ROOT).as_posix(): v for p in tool_sources()
                 if (v := tool_violations(p.read_text(encoding='utf-8')))}
    assert offenders == {}


@pytest.mark.parametrize('snippet', [
    'import workmcp',
    'import workmcp.models',
    'from workmcp import models',
    'from workmcp.setup import main',
    'def f():\n    from workmcp import paths',
    'from . import helper',
    'import numpy',
    'def f():\n    import onnxruntime',
    'from mcp.server import MCPServer',
    '__import__("workmcp")',
    'import importlib\nimportlib.import_module("workmcp.models")',
    'import runpy\nrunpy.run_module("workmcp")',
    'import sys\nsys.path.insert(0, "src")\nimport workmcp.cli',
])
def test_tool_rule_flags_package_and_third_party_imports(snippet):
    assert tool_violations(snippet)


def test_tool_rule_accepts_a_stdlib_bootstrap():
    clean = ('import argparse, hashlib, json, os, shutil, subprocess, sys, venv, zipfile\n'
             'import urllib.request\nfrom pathlib import Path\n'
             'def main():\n    print(sys.executable, sys.version_info[:2])\n'
             '    subprocess.run([sys.executable, "-I", "-m", "pip", "--version"], check=False)\n'
             '    subprocess.run([venv_python, "-I", "-m", "workmcp", "--help"], check=False)\n'
             '    print("next: python -I -m workmcp setup --home ...")\n')
    assert tool_violations(clean) == []

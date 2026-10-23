"""Every test runs with a temporary data home; nothing touches the real one."""
import getpass
import os
import tempfile
from pathlib import Path

import pytest


def _default_temp_root_usable() -> bool:
    root = Path(tempfile.gettempdir())/f'pytest-of-{getpass.getuser()}'
    try:
        if root.exists():
            os.listdir(root)
        return True
    except OSError:
        return False


# A stale, unreadable %TEMP%\pytest-of-<user> (seen on the dev PC) breaks tmp_path; use a
# fresh root beside it instead. Still outside the repository, so data-home rules hold.
if 'PYTEST_DEBUG_TEMPROOT' not in os.environ and not _default_temp_root_usable():
    os.environ['PYTEST_DEBUG_TEMPROOT'] = str(Path(tempfile.gettempdir())/'workmcp-pytest')
    Path(os.environ['PYTEST_DEBUG_TEMPROOT']).mkdir(exist_ok=True)


@pytest.fixture(autouse=True)
def workmcp_home(tmp_path, monkeypatch):
    home = tmp_path/'workmcp-home'
    monkeypatch.setenv('WORKMCP_HOME', str(home))
    return home

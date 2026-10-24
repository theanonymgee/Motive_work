"""Where the tests find the real embedding model, if there is one (design M2 §10).

``WORKMCP_TEST_MODEL_HOME`` names a data home whose ``models/`` folder holds the model; otherwise the
default data home (``%LOCALAPPDATA%``/Motive_work) is used. The result is a home only when the model
files are there with their pinned sizes; the tests then verify the SHA-256 themselves. Tests that need
the model skip with ``SKIP_REASON`` otherwise. Nothing here downloads or writes anything.
"""
import os
from pathlib import Path

from workmcp import models, paths

ENV = 'WORKMCP_TEST_MODEL_HOME'
SKIP_REASON = ('no real model: put the bge-m3 files in the data home (workmcp fetch-model bge-m3 --from FOLDER) '
               f'or set {ENV} to a data home that has them')


def candidate_home() -> Path | None:
    if os.environ.get(ENV):
        return Path(os.environ[ENV])
    local = os.environ.get('LOCALAPPDATA')
    return Path(local)/paths.APP_DIR if local else None


def real_model_home(name: str = models.DEFAULT_MODEL) -> Path | None:
    home = candidate_home()
    return home if home is not None and models.available(name, home) else None

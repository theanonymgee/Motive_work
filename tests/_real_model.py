"""Where the tests find the real embedding model, if there is one (design M2 §10).

``WORKMCP_TEST_MODEL_HOME`` names a data home whose ``models/`` folder holds the model; otherwise the
default data home (``%USERPROFILE%``/Motive_work, M5) and then the earlier default
(``%LOCALAPPDATA%``/Motive_work, M0-M4) are tried. The result is a home only when the model
files are there with their pinned sizes; the tests then verify the SHA-256 themselves. Tests that need
the model skip with ``SKIP_REASON`` otherwise. Nothing here downloads or writes anything.
"""
import os
from pathlib import Path

from workmcp import models, paths

ENV = 'WORKMCP_TEST_MODEL_HOME'
SKIP_REASON = ('no real model: put the bge-m3 files in the data home (workmcp fetch-model bge-m3 --from FOLDER) '
               f'or set {ENV} to a data home that has them')


def candidate_homes() -> list[Path]:
    if os.environ.get(ENV):
        return [Path(os.environ[ENV])]
    return [Path(os.environ[name])/paths.APP_DIR for name in (paths.DEFAULT_BASE_ENV, 'LOCALAPPDATA')
            if os.environ.get(name)]


def real_model_home(name: str = models.DEFAULT_MODEL) -> Path | None:
    return next((home for home in candidate_homes() if models.available(name, home)), None)

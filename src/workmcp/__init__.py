"""workmcp: personal, local-only work-knowledge MCP server.

Network rule: the only package module that may use the network is ``models.py``, and
only inside its explicit model download functions (network imports live inside those
functions). The server talks stdio only and makes no external calls. Data lives in the
data home chosen by ``paths.py`` (outside the repository, never in a sync folder).
"""

__version__ = '0.0.1'

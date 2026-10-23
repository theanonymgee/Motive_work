"""MCP stdio transport adapter; business behavior lives in ``tools.py`` (design §1.1, §8.5).

stdout carries MCP JSON-RPC only: nothing here prints to stdout, logs go to stderr.
The server runs the stdio transport only and makes no outbound calls.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from . import __version__, paths, store, tools


def build_server(home: Path) -> MCPServer:
    server = MCPServer('workmcp', version=__version__)

    @server.tool()
    def status() -> dict[str, Any]:
        """Data home, store counts, model availability and FTS5 support. Returns no document text."""
        return tools.status(home)

    return server


def main(argv=None) -> int:
    try:
        home = paths.resolve_home()
    except paths.DataPathError as exc:
        print(exc, file=sys.stderr)
        return 2
    paths.ensure_home(home)
    store.open_home(home).close()
    build_server(home).run(transport='stdio')
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""The Commons MCP server package.

The server implementation lives in server.py next to this file and is
unchanged; this module only provides the console entry point used by
the installed `the-commons-mcp` command (and `python -m
the_commons_mcp`). Configuration comes from the environment, see
server.py and README.md.
"""

import sys


def main():
    from .server import NETWORK, server

    if NETWORK != "testnet":
        print("COMMONS_NETWORK must be testnet", file=sys.stderr)
        sys.exit(1)
    server.run(transport="stdio")


__all__ = ["main"]

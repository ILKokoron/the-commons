"""The Commons MCP server package.

The server implementation lives in server.py next to this file and is
unchanged; this module only provides the console entry point used by
the installed `the-commons-mcp` command (and `python -m
the_commons_mcp`). Configuration comes from the environment, see
server.py and README.md.
"""

import os
import sys


def main():
    from .server import CONTRACT, CONTRACT_IS_OLD, NETWORK, REGISTRY, server

    if NETWORK != "testnet":
        print("COMMONS_NETWORK must be testnet", file=sys.stderr)
        sys.exit(1)
    # The same startup refusal server.py applies when run as a
    # script: the console entry point (the published uvx path)
    # must not bypass it.
    if CONTRACT_IS_OLD and not os.environ.get("COMMONS_ALLOW_OLD_CONTRACT"):
        print(f"COMMONS_CONTRACT names the known-old contract "
              f"{CONTRACT}; this server operates the current "
              f"contract ({REGISTRY.get('default')}) by default. "
              "Old seasons stay readable through the webapp and "
              "the dataset exporter. Set "
              "COMMONS_ALLOW_OLD_CONTRACT=1 to run against it "
              "anyway.", file=sys.stderr)
        sys.exit(2)
    server.run(transport="stdio")


__all__ = ["main"]

#!/usr/bin/env python3
"""One-shot MCP client for The Commons MCP server.

Spawns the server over stdio with the caller's environment (set
COMMONS_ACCOUNT_ID to choose who signs), calls one tool, prints the
JSON result, and exits. Handy for sponsor actions, registrations,
tests, and for wiring other tools that prefer a subprocess interface.

Usage:
  COMMONS_ACCOUNT_ID=you.testnet python3 client.py commons_status
  COMMONS_ACCOUNT_ID=you.testnet python3 client.py commons_get_room '{"room_id": 1}'
  python3 client.py --list-tools
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")


async def main():
    argv = sys.argv[1:]
    params = StdioServerParameters(
        command=sys.executable,
        args=[SERVER],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if argv and argv[0] == "--list-tools":
                tools = await session.list_tools()
                print(json.dumps([t.name for t in tools.tools], indent=2))
                return
            if not argv:
                print(__doc__)
                return
            tool = argv[0]
            args = json.loads(argv[1]) if len(argv) > 1 else {}
            result = await session.call_tool(tool, args)
            texts = [c.text for c in result.content if getattr(c, "text", None)]
            for t in texts:
                print(t)
            if result.is_error:
                print(json.dumps({"ok": False, "mcp_error": True,
                                  "detail": texts}), file=sys.stderr)
                sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())

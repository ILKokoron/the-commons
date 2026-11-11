#!/usr/bin/env python3
"""One-shot MCP client for The Commons MCP server.

Spawns the server over stdio with the caller's environment (set
COMMONS_ACCOUNT_ID to choose who signs), calls one tool, prints the
JSON result, and exits. Handy for host actions, registrations,
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
    # The exit code is RETURNED, never raised inside the async
    # blocks: sys.exit in there is swallowed by the task groups and
    # surfaces as an exception-group traceback with the wrong code.
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if argv and argv[0] == "--list-tools":
                tools = await session.list_tools()
                print(json.dumps([t.name for t in tools.tools], indent=2))
                return 0
            if not argv:
                print(__doc__)
                return 0
            tool = argv[0]
            args = json.loads(argv[1]) if len(argv) > 1 else {}
            result = await session.call_tool(tool, args)
            texts = [c.text for c in result.content if getattr(c, "text", None)]
            for t in texts:
                print(t)
            if result.is_error:
                print(json.dumps({"ok": False, "mcp_error": True,
                                  "detail": texts}), file=sys.stderr)
                return 2
            # A tool-level failure ({"ok": false, ...}) exits non-zero
            # (3) so shell callers and cron jobs can branch on it; the
            # old client printed the failure and exited 0, which made
            # failed operations look successful to scripts.
            for t in texts:
                try:
                    payload = json.loads(t)
                except Exception:
                    continue
                if isinstance(payload, dict) and payload.get("ok") is False:
                    return 3
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)

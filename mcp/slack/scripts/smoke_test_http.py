#!/usr/bin/env python3
"""Smoke test for a running slack-mcp: MCP handshake + tool calls over
streamable-HTTP. Read-only; lists channels and reads one message.

Usage:
  smoke_test_http.py [base-url] [bearer-token]
    base-url      default: http://127.0.0.1:8009
    bearer-token  omit only if this deployment has auth disabled
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

EXPECTED_TOOLS = {"get_server_info", "slack_list_channels", "slack_read_channel", "slack_read_thread"}


async def check_mcp(base_url: str, token: str | None) -> None:
    url = base_url.rstrip("/") + "/mcp"
    print(f"Connecting to {url} ...")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx2.AsyncClient(headers=headers, timeout=60) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                missing = EXPECTED_TOOLS - tools
                print(f"tools: {sorted(tools)}")
                if missing:
                    raise SystemExit(f"FAILED: missing tools {sorted(missing)}")
                info = await session.call_tool("get_server_info", {})
                print("server info:", info.content[0].text[:300])
                chans = await session.call_tool("slack_list_channels", {})
                print("channels:", chans.content[0].text[:300])
                first = json.loads(chans.content[0].text)
                first = first[0] if isinstance(first, list) and first else first
                name = (first or {}).get("name") if isinstance(first, dict) else None
                if name:
                    msgs = await session.call_tool("slack_read_channel", {"channel": name, "since": "7d", "limit": 1})
                    print("read_channel:", msgs.content[0].text[:300])
    print("OK")


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8009"
    tok = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(check_mcp(base, tok))

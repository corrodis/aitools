"""MCP tool access.

The agent's entire capability surface is what the MCP servers in the internal
registry expose. There is deliberately no shell, file or code-execution tool
anywhere in this package -- "no system access" is a property of what exists
here, not a setting that can be flipped.

Connections are opened per call rather than held open for the life of the
process. That costs one HTTP round trip per tool call on a campus network,
and buys three things that matter more here: MCP servers can be redeployed
underneath a running bot, no session expires while a Slack thread sits idle
for hours, and every anyio cancel scope opens and closes inside the same task
(long-lived streamablehttp sessions shared across asyncio tasks are the usual
source of "attempted to exit cancel scope in a different task" crashes).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiohttp
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

log = logging.getLogger(__name__)

# Slack threads are chatty and tool output is not; a runaway result would
# otherwise eat the whole context window in one call.
MAX_RESULT_CHARS = 20000


@dataclass
class Server:
    name: str
    url: str
    description: str
    needs_token: bool


@dataclass
class Tool:
    server: str
    name: str
    qualified: str
    description: str
    schema: dict


def _qualify(server: str, tool: str) -> str:
    """OpenAI tool names allow [a-zA-Z0-9_-]{1,64}; registry names and MCP tool
    names are both free-form, so sanitize and truncate."""
    raw = f"{server}__{tool}"
    return re.sub(r"[^a-zA-Z0-9_-]", "_", raw)[:64]


class ToolRegistry:
    """Tools from every server in the MCP registry, namespaced by server."""

    def __init__(self, registry_url: str, token: str = "", timeout: int = 120):
        self._registry_url = registry_url
        self._token = token
        self._timeout = timeout
        self.servers: dict[str, Server] = {}
        self.tools: dict[str, Tool] = {}
        self.failed: dict[str, str] = {}

    # -- discovery ------------------------------------------------------------

    async def load(self) -> None:
        """Fetch the registry, then list tools on every server it names.

        A server that is down is recorded in .failed and skipped: one broken
        MCP should not keep the bot off Slack entirely.
        """
        self.servers = await self._fetch_registry()
        self.tools = {}
        self.failed = {}

        results = await asyncio.gather(
            *(self._list_server_tools(s) for s in self.servers.values()),
            return_exceptions=True,
        )
        for server, result in zip(self.servers.values(), results):
            if isinstance(result, BaseException):
                reason = describe(result)
                self.failed[server.name] = reason
                log.warning("MCP server %s unavailable: %s", server.name, reason)
                continue
            for tool in result:
                self.tools[tool.qualified] = tool

        log.info(
            "Loaded %d tools from %d/%d MCP servers",
            len(self.tools), len(self.servers) - len(self.failed), len(self.servers),
        )

    async def _fetch_registry(self) -> dict[str, Server]:
        async with aiohttp.ClientSession() as session:
            async with session.get(self._registry_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)

        servers: dict[str, Server] = {}
        for name, entry in (payload.get("mcpServers") or {}).items():
            if name == "registry":
                # The registry endpoint itself -- a directory, not a tool
                # provider worth handing to the model.
                continue
            servers[name] = Server(
                name=name,
                url=entry["url"],
                description=entry.get("description", ""),
                needs_token=entry.get("token") == "yes",
            )
        return servers

    async def _list_server_tools(self, server: Server) -> list[Tool]:
        async with self._session(server) as session:
            listing = await session.list_tools()

        tools = []
        for t in listing.tools:
            schema = dict(t.input_schema or {})
            if not schema:
                schema = {"type": "object", "properties": {}}
            schema.setdefault("type", "object")
            tools.append(Tool(
                server=server.name,
                name=t.name,
                qualified=_qualify(server.name, t.name),
                description=(t.description or "").strip(),
                schema=schema,
            ))
        return tools

    # -- use ------------------------------------------------------------------

    @property
    def schemas(self) -> list[dict]:
        """Tool definitions in OpenAI chat-completions form."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.qualified,
                    "description": tool.description,
                    "parameters": tool.schema,
                },
            }
            for tool in self.tools.values()
        ]

    async def call(self, qualified: str, arguments: dict) -> str:
        tool = self.tools.get(qualified)
        if tool is None:
            return f"Error: no such tool {qualified!r}."

        server = self.servers[tool.server]
        started = time.monotonic()
        try:
            async with self._session(server) as session:
                result = await session.call_tool(
                    tool.name, arguments, read_timeout_seconds=float(self._timeout)
                )
        except Exception as exc:
            reason = describe(exc)
            log.warning("Tool %s failed after %.1fs: %s",
                        qualified, time.monotonic() - started, reason)
            return f"Error calling {qualified}: {reason}"

        text = _result_text(result)
        # A server that is broken usually reports it *in* a successful response
        # rather than by raising -- memory-mcp answers a dead connection pool
        # with a 30s "PoolTimeout" string, which looked like a healthy call in
        # the log and left us blaming the model for making it up. Log the
        # outcome of every call, and say so out loud when it smells like a
        # failure.
        elapsed = time.monotonic() - started
        looks_wrong = getattr(result, "isError", False) or text.lstrip().startswith("Error")
        log.log(logging.WARNING if looks_wrong else logging.INFO,
                "tool %s %s in %.1fs (%d chars)%s", qualified,
                "FAILED" if looks_wrong else "ok", elapsed, len(text),
                f": {text[:160]}" if looks_wrong else "")
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n...[truncated]"
        return text

    @asynccontextmanager
    async def _session(self, server: Server):
        # mcp 2.x takes per-request headers only through a pre-built HTTP
        # client, so the bearer token for the gated servers (ecl, runs,
        # memory) has to be set here rather than on the transport.
        headers = {}
        if server.needs_token and self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        # Long read timeout because a server may hold the response stream open
        # while it works; connect/write stay short so a dead host fails fast.
        timeout = httpx2.Timeout(30.0, read=float(self._timeout))

        async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client:
            async with streamable_http_client(server.url, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session


def describe(exc: BaseException) -> str:
    """Flatten an exception to something worth printing.

    anyio runs the transport in a task group, so a refused connection or a 401
    surfaces as an ExceptionGroup whose str() is "unhandled errors in a
    TaskGroup (1 sub-exception)" -- which tells an operator nothing. Unwrap to
    the causes underneath.
    """
    inner = getattr(exc, "exceptions", None)
    if inner:
        return "; ".join(describe(e) for e in inner)
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _result_text(result) -> str:
    """Flatten an MCP CallToolResult into something a chat model can read."""
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        parts.append(text if text is not None else str(item))

    if not parts:
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return json.dumps(structured, indent=2, default=str)
        return "(tool returned no content)"

    body = "\n".join(parts)
    if getattr(result, "isError", False):
        return f"Tool reported an error: {body}"
    return body

"""Default backend: chat-completions loop + the MCP registry's tools.

This is the agent the bot shipped with (``agent.Conversation`` over
``mcp_tools.ToolRegistry`` against an OpenAI-compatible endpoint), packaged
behind the :mod:`backend` protocols so the Slack adapter does not depend on
it directly.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx2
from openai import AsyncOpenAI

from .agent import Conversation
from .backend import Backend
from .mcp_tools import ToolRegistry

log = logging.getLogger(__name__)


def make_llm_client(cfg) -> AsyncOpenAI:
    # Keep-alive is switched off deliberately, and it is not a micro-optimisation
    # knob -- without it the agent cannot complete a single tool-using turn.
    # Measured against vllm.fnal.gov: an LLM call works, an MCP tool call works,
    # and the next LLM call on the SAME client dies with
    # ssl.SSLError("[SSL] passed invalid argument") -- while the same call on a
    # fresh client succeeds, and the whole sequence succeeds with no MCP call in
    # between. An MCP session's teardown invalidates this client's idle pooled
    # TLS connection (mcp and openai share one anyio/httpcore2 runtime, and the
    # transport's task-group cancellation reaches a connection it does not own).
    # No idle connection, nothing to poison. Costs one TLS handshake per request,
    # which is free at chat pace.
    http_client = httpx2.AsyncClient(
        timeout=httpx2.Timeout(600.0, connect=30.0),
        limits=httpx2.Limits(max_keepalive_connections=0),
    )
    # vllm.fnal.gov takes no key today, but the OpenAI client insists on one.
    return AsyncOpenAI(base_url=cfg.endpoint, api_key=cfg.api_key or "EMPTY",
                       timeout=600, http_client=http_client)


class RegistryBackend(Backend):
    name = "registry"

    def __init__(self, cfg):
        self.cfg = cfg
        self.llm = make_llm_client(cfg)
        self.tools = ToolRegistry(cfg.registry_url, cfg.mcp_token, cfg.tool_timeout)

    async def open(self) -> None:
        await self.tools.load()
        log.info("Loaded %d tool(s) from %d server(s)", len(self.tools.tools), len(self.tools.servers))

    async def close(self) -> None:
        return None

    async def new_conversation(self, key: str, context: dict[str, Any]) -> Conversation:
        return Conversation(key, self.cfg, self.llm, self.tools, context)

    async def close_conversation(self, conv: Conversation) -> None:
        return None  # nothing held outside memory

    async def list_models(self) -> list[str]:
        listing = await self.llm.models.list()
        return sorted(m.id for m in listing.data)

    def tools_by_server(self) -> tuple[dict[str, list[str]], dict[str, str]]:
        by_server: dict[str, list[str]] = {}
        for tool in self.tools.tools.values():
            by_server.setdefault(tool.server, []).append(tool.name)
        return by_server, dict(self.tools.failed)

    async def check(self) -> tuple[bool, list[str]]:
        ok = True
        lines = [f"LLM endpoint: {self.cfg.endpoint}"]
        try:
            names = await self.list_models()
            lines.append(f"  reachable, {len(names)} model(s); configured: {self.cfg.model}"
                         f"{'' if self.cfg.model in names else '  <-- NOT offered by this endpoint'}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            lines.append(f"  FAILED: {exc}")

        lines.append(f"MCP registry: {self.cfg.registry_url}")
        try:
            await self.tools.load()
            lines.append(f"  {len(self.tools.tools)} tool(s) from "
                         f"{len(self.tools.servers) - len(self.tools.failed)}/{len(self.tools.servers)} server(s)")
            for name, reason in sorted(self.tools.failed.items()):
                lines.append(f"  unavailable: {name} ({reason})")
            if not self.cfg.mcp_token:
                lines.append("  note: MIKEY_TOKEN unset -- token-gated servers (ecl, runs, memory) will refuse calls")
        except Exception as exc:  # noqa: BLE001
            ok = False
            lines.append(f"  FAILED: {exc}")
        return ok, lines

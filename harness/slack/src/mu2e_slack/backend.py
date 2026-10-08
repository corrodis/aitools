"""The seam between the Slack adapter and whatever agent answers.

The adapter (slackbot.py, commands.py, usage_log.py) only ever talks to a
:class:`Backend` and the :class:`Conversation` objects it hands out.  The
default is :class:`~mu2e_slack.registry_backend.RegistryBackend` -- the
plain chat-completions loop over the MCP registry's tools that this bot
shipped with -- but another package can front a different agent with the
same Slack behaviour (threads, commands, status message, usage log) by
implementing these two protocols.  daqpy does this for the DAQ bot.

Operations a backend does not offer raise :class:`NotSupported`; the
command layer turns that into a short "not available here" reply instead
of an error.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

# Called before each tool invocation: (tool name, arguments).
OnTool = Callable[[str, dict], Awaitable[None]]


class NotSupported(Exception):
    """This backend does not offer the requested operation."""


@runtime_checkable
class Conversation(Protocol):
    """One Slack thread's worth of agent state."""

    key: str
    lock: asyncio.Lock           # one turn at a time per thread
    last_active: float           # time.monotonic() of the last activity, for idle cleanup
    tool_notifications: bool     # !verbose on|off
    model: str                   # shown by !model / !status

    async def ask(self, text: str, on_tool: OnTool | None = None) -> str:
        """Run one user turn to completion and return the reply (markdown)."""

    def status(self) -> dict[str, Any]:
        """Facts for !status: model, turns, llm_calls, tool_calls, input_tokens,
        output_tokens, last_prompt_tokens, context_limit, plus any backend-specific
        keys (rendered as extra lines)."""

    def links(self) -> dict[str, str]:
        """Named URLs about this conversation (e.g. a web transcript), may be empty."""

    def reset(self) -> None:
        """Forget this thread's history."""

    async def compact(self) -> str:
        """Summarize the history to free context; return the summary.  May raise NotSupported."""

    def set_model(self, name: str) -> None:
        """Switch this thread's model.  May raise NotSupported."""

    def usage_snapshot(self) -> dict[str, Any] | None:
        """Fields for the harness usage record (see usage_log.build_record):
        endpoint_url, model, created_at, updated_at, turns, llm_calls,
        tool_calls, tool_breakdown, input_tokens, output_tokens,
        cache_read_tokens, cache_write_tokens, thinking_tokens; optionally
        provider (default: derived from endpoint_url).  ``None`` when the backend
        writes its own usage log and the adapter should not."""


class Backend(Protocol):
    """Factory for conversations plus what the commands and --check need."""

    name: str
    # False when conversations return None from usage_snapshot() because the
    # backend keeps its own usage log; --check then does not advertise the
    # adapter's log path.
    adapter_usage_log: bool = True

    async def open(self) -> None:
        """Connect / load tools.  Called once before Slack is joined."""

    async def close(self) -> None: ...

    async def new_conversation(self, key: str, context: dict[str, Any]) -> Conversation:
        """``key`` is "<channel>:<thread_ts>"; ``context`` has interface, user_name,
        slack_channel, thread_url when known."""

    async def close_conversation(self, conv: Conversation) -> None:
        """Release a conversation (idle cleanup or shutdown).  Backends holding
        subprocesses or sessions free them here."""

    async def list_models(self) -> list[str]:
        """Models a thread may switch to; empty when the model is fixed."""

    def tools_by_server(self) -> tuple[dict[str, list[str]], dict[str, str]]:
        """({server: [tool names]}, {server: why unavailable}) for !tools."""

    async def check(self) -> tuple[bool, list[str]]:
        """Verify the backend's own dependencies; (ok, human-readable lines)."""

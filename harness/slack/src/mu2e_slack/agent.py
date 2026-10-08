"""The agent loop: one Conversation per Slack thread.

A plain OpenAI chat-completions loop against whatever endpoint is configured
(vllm.fnal.gov/gpt-oss today), with the MCP registry's tools as the only
capability. Conversation state lives in memory for as long as the thread is
active; the durable artifact is the usage record (see usage_log.py), not the
transcript.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .mcp_tools import ToolRegistry

log = logging.getLogger(__name__)

COMPACT_TRIGGER = 0.8  # fraction of the context budget that forces a compaction

# Anthropic prompt caching, passed through LiteLLM's OpenAI-compatible API as
# cache_control markers. Three breakpoints (of the four allowed), in render
# order tools -> system -> messages:
#   1. the last tool definition -- identical for every thread and user;
#   2. the system prompt        -- identical for the life of a thread;
#   3. the newest message       -- the conversation so far, reused by the next
#      model call in the same turn (one per tool round trip) and the next turn.
# A cache hit needs a byte-identical prefix, which is why nothing in the
# system prompt changes per request (the time of day rides on each user
# message instead). Reads cost ~0.1x input, writes ~1.25x.
CACHE_MARK = {"type": "ephemeral"}

COMPACT_INSTRUCTION = (
    "Summarize the conversation so far for your own future reference. Keep the "
    "user's goal, every fact you established from tool calls (with document "
    "ids, run numbers, dataset names and links), and anything still open. Drop "
    "pleasantries and superseded detail. Write it as notes, not prose."
)


@dataclass
class Usage:
    turns: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    thinking_tokens: int = 0
    tool_breakdown: dict[str, int] = field(default_factory=dict)

    @property
    def tool_calls(self) -> int:
        return sum(self.tool_breakdown.values())


class Conversation:
    """One Slack thread's worth of state."""

    def __init__(self, key: str, cfg, client, tools: ToolRegistry, context: dict):
        self.key = key
        self.cfg = cfg
        self.client = client
        self.tools = tools
        self.context = context
        self.model = cfg.model  # per-thread, switchable with !model
        self.messages: list[dict] = []
        self.usage = Usage()
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = self.created_at
        self.last_prompt_tokens = 0
        self.tool_notifications = cfg.tool_notifications
        self.lock = asyncio.Lock()
        self.last_active = time.monotonic()

    # -- prompt ---------------------------------------------------------------

    def _system_prompt(self) -> dict:
        # No clock in here: anything that changes per request in the system
        # prompt defeats prompt caching for it and everything after it. The
        # time travels with each user message instead (see ask()).
        prefix = self.cfg.command_prefix
        extra = (
            f"\n\nEach user message starts with the local time it was sent, in brackets."
            f"\nThis conversation is a Slack thread; everything said in it is shared "
            f"context. Users can type {prefix}help for commands that control this "
            f"session (model, compaction, reset) -- mention that only if they ask "
            f"how to change something."
            f"\n\nContext: {json.dumps(self.context, indent=2)}"
        )
        return {"role": "system", "content": self.cfg.system_prompt + extra}

    # -- main loop ------------------------------------------------------------

    async def ask(self, text: str, on_tool=None) -> str:
        """Run one user turn to completion and return the reply text."""
        self.usage.turns += 1
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        self.messages.append({"role": "user", "content": f"[{now}] {text}"})

        # Reset per turn, not per conversation: asking the same question again
        # later is legitimate, repeating a call inside one turn is not.
        attempted: set[str] = set()

        for _ in range(self.cfg.max_tool_iterations):
            message = await self._complete()

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                await self._maybe_compact()
                return message.get("content") or "(no answer)"

            for call in tool_calls:
                await self._run_tool(call, on_tool, attempted)

        # Ran out of tool budget. Everything gathered so far is still in
        # self.messages, so a follow-up question can continue from here.
        return (
            f"I stopped after {self.cfg.max_tool_iterations} tool calls without "
            f"reaching an answer. Ask me to continue, or narrow the question."
        )

    def _caching(self) -> bool:
        mode = getattr(self.cfg, "prompt_cache", "auto")
        if mode == "auto":
            # Only Anthropic models understand cache_control; vLLM/gpt-oss may
            # reject the unknown field. Checked per call: !model can switch.
            return "claude" in self.model.lower()
        return mode == "on"

    def _request(self) -> tuple[list[dict], list[dict]]:
        """Messages and tools for one model call, with cache markers when
        caching is on. Marks go on copies: self.messages stays plain, so the
        history is the same whichever model reads it next."""
        system = self._system_prompt()
        messages = [system] + self.messages
        schemas = self.tools.schemas
        if not self._caching():
            return messages, schemas
        if schemas:
            schemas = schemas[:-1] + [{**schemas[-1], "cache_control": CACHE_MARK}]
        messages = [_marked(system)] + self.messages[:-1] + [_marked(self.messages[-1])]
        return messages, schemas

    async def _complete(self) -> dict:
        messages, schemas = self._request()
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=schemas or None,
            temperature=self.cfg.temperature,
            max_tokens=self.cfg.max_tokens,
        )
        self._account(response)

        choice = response.choices[0].message
        # Rebuild the assistant message by hand rather than dumping the model
        # object: vLLM adds fields (reasoning_content and friends) that some
        # endpoints reject when they come back in the next request.
        message: dict = {"role": "assistant", "content": choice.content or ""}
        if choice.tool_calls:
            message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
                }
                for tc in choice.tool_calls
            ]
        self.messages.append(message)
        self.updated_at = datetime.now(timezone.utc)
        self.last_active = time.monotonic()
        return message

    async def _run_tool(self, call: dict, on_tool, attempted: set[str]) -> None:
        name = call["function"]["name"]
        raw_args = call["function"]["arguments"] or "{}"
        try:
            arguments = json.loads(raw_args)
        except json.JSONDecodeError:
            arguments = {}
            result = f"Error: could not parse arguments for {name}: {raw_args!r}"
        else:
            # A model that gets an empty result tends to re-issue the same call
            # rather than accept the answer. Refusing the duplicate costs one
            # cheap round trip instead of a real one, and says plainly what to
            # do instead -- far more effective than prompt wording alone.
            signature = f"{name}:{json.dumps(arguments, sort_keys=True, default=str)}"
            if signature in attempted:
                self.messages.append({
                    "role": "tool", "tool_call_id": call["id"],
                    "content": ("You already made this exact call in this turn and the "
                                "result is above. Repeating it will not change the answer. "
                                "Either take a materially different approach, or tell the "
                                "user what you found and what you could not find."),
                })
                return
            attempted.add(signature)

            self.usage.tool_breakdown[name] = self.usage.tool_breakdown.get(name, 0) + 1
            if on_tool is not None:
                await on_tool(name, arguments)
            result = await self.tools.call(name, arguments)

        self.messages.append({
            "role": "tool",
            "tool_call_id": call["id"],
            "content": result,
        })

    def _account(self, response) -> None:
        self.usage.llm_calls += 1
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.last_prompt_tokens = usage.prompt_tokens or 0
        self.usage.input_tokens += usage.prompt_tokens or 0
        self.usage.output_tokens += usage.completion_tokens or 0
        # LiteLLM passes Anthropic's counters through; prompt_tokens is the
        # total including both. Fall back to OpenAI's cached_tokens elsewhere.
        read = getattr(usage, "cache_read_input_tokens", None)
        if read is None:
            details = getattr(usage, "prompt_tokens_details", None)
            read = getattr(details, "cached_tokens", 0) if details is not None else 0
        self.usage.cache_read_tokens += read or 0
        self.usage.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        details = getattr(usage, "completion_tokens_details", None)
        if details is not None:
            self.usage.thinking_tokens += getattr(details, "reasoning_tokens", 0) or 0

    # -- context management ---------------------------------------------------

    async def _maybe_compact(self) -> None:
        if self.last_prompt_tokens < COMPACT_TRIGGER * self.cfg.context_limit:
            return
        log.info("Auto-compacting %s at %d prompt tokens", self.key, self.last_prompt_tokens)
        await self.compact()

    async def compact(self) -> str:
        """Replace the transcript with a summary of itself. Returns the summary."""
        if not self.messages:
            return "Nothing to compact yet."

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[self._system_prompt()] + self.messages + [
                {"role": "user", "content": COMPACT_INSTRUCTION}
            ],
            temperature=0.0,
            max_tokens=self.cfg.max_tokens,
        )
        self._account(response)
        summary = response.choices[0].message.content or ""

        self.messages = [
            {"role": "user", "content": f"Notes from earlier in this thread:\n\n{summary}"},
            {"role": "assistant", "content": "Noted -- carrying on from there."},
        ]
        return summary

    def reset(self) -> None:
        self.messages = []
        self.last_prompt_tokens = 0

    # -- backend protocol (see backend.py) ------------------------------------

    def set_model(self, name: str) -> None:
        self.model = name

    def status(self) -> dict:
        u = self.usage
        return {
            "model": self.model, "endpoint": self.cfg.endpoint,
            "turns": u.turns, "llm_calls": u.llm_calls, "tool_calls": u.tool_calls,
            "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
            "cache_read_tokens": u.cache_read_tokens, "cache_write_tokens": u.cache_write_tokens,
            "last_prompt_tokens": self.last_prompt_tokens, "context_limit": self.cfg.context_limit,
            "tool_notifications": self.tool_notifications,
        }

    def links(self) -> dict[str, str]:
        return {}

    def usage_snapshot(self) -> dict:
        u = self.usage
        return {
            "endpoint_url": self.cfg.endpoint, "model": self.model,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "turns": u.turns, "llm_calls": u.llm_calls, "tool_calls": u.tool_calls,
            "tool_breakdown": dict(u.tool_breakdown),
            "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
            "cache_read_tokens": u.cache_read_tokens,
            "cache_write_tokens": u.cache_write_tokens,
            "thinking_tokens": u.thinking_tokens,
        }


def _marked(message: dict) -> dict:
    """A copy of ``message`` whose last content block carries a cache mark.
    Plain-string content becomes a single text block; a message with no text
    (an assistant turn that only calls tools) is returned unmarked."""
    content = message.get("content")
    if isinstance(content, str) and content:
        blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list) and content:
        blocks = list(content)
    else:
        return message
    blocks[-1] = {**blocks[-1], "cache_control": CACHE_MARK}
    return {**message, "content": blocks}

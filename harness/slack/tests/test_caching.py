"""Prompt-cache markers on the request -- no LLM, no network."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

from mu2e_slack.agent import CACHE_MARK, Conversation


class Tools:
    schemas = [{"type": "function", "function": {"name": f"t{i}", "parameters": {}}} for i in range(3)]


def _conv(model="azure/claude-sonnet-5-5", prompt_cache="auto"):
    cfg = NS(model=model, prompt_cache=prompt_cache, command_prefix="!", system_prompt="You help.",
             tool_notifications=True)
    conv = Conversation("k", cfg, client=None, tools=Tools(), context={"channel": "C"})
    conv.messages = [
        {"role": "user", "content": "[2026-10-07 20:00 CDT] hi"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function",
                                                             "function": {"name": "t0", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "result"},
    ]
    return conv


def _marks(obj) -> int:
    return json.dumps(obj).count('"cache_control"')


def test_three_breakpoints_on_copies_only():
    conv = _conv()
    before = json.dumps(conv.messages)
    messages, tools = conv._request()
    assert tools[-1]["cache_control"] == CACHE_MARK and _marks(tools[:-1]) == 0
    assert messages[0]["content"][-1]["cache_control"] == CACHE_MARK  # system
    assert messages[-1]["content"][-1] == {"type": "text", "text": "result", "cache_control": CACHE_MARK}
    assert _marks(messages[1:-1]) == 0
    assert _marks(messages) + _marks(tools) == 3  # Anthropic allows 4
    assert json.dumps(conv.messages) == before and _marks(Tools.schemas) == 0


def test_no_markers_for_models_that_do_not_cache():
    for conv in (_conv(model="openai/gpt-oss-120b"), _conv(prompt_cache="off")):
        messages, tools = conv._request()
        assert _marks(messages) + _marks(tools) == 0
    messages, tools = _conv(model="openai/gpt-oss-120b", prompt_cache="on")._request()
    assert _marks(messages) + _marks(tools) == 3


def test_system_prompt_is_byte_identical_across_calls():
    conv = _conv()
    assert conv._system_prompt() == conv._system_prompt()
    assert "Current date and time" not in conv._system_prompt()["content"]


def test_user_messages_carry_the_time():
    conv = _conv()
    conv.messages = []

    async def fake_complete():
        return {"role": "assistant", "content": "ok"}

    conv._complete = fake_complete
    conv._maybe_compact = lambda: asyncio.sleep(0)
    conv.cfg.max_tool_iterations = 1
    asyncio.run(conv.ask("what time is it?"))
    text = conv.messages[0]["content"]
    assert text.startswith("[20") and text.endswith("] what time is it?")

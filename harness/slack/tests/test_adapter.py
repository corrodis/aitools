"""Adapter tests with a fake backend -- no Slack, no LLM, no network."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace as NS

import pytest

from mu2e_slack import commands, slackbot, usage_log
from mu2e_slack.backend import NotSupported


def _cfg(**kw):
    base = dict(slack_bot_token="xoxb-test", slack_app_token="xapp-test", channel="home",
                thread_followups=True, command_prefix="!", idle_timeout=10, privacy=False,
                log_output="", endpoint="http://llm", context_limit=1000,
                rate_user="2/10m", rate_total="3/10m", max_concurrent=2,
                allowed_channels=[], dm_enabled=True)
    base.update(kw)
    return NS(**base)


class FakeConv:
    def __init__(self, key, context):
        self.key, self.context = key, context
        self.lock = asyncio.Lock()
        self.last_active = time.monotonic()
        self.tool_notifications = True
        self.model = "fixed-model"
        self.asked: list[str] = []
        self.was_reset = False

    async def ask(self, text, on_tool=None):
        self.asked.append(text)
        if on_tool:
            await on_tool("some_tool", {"x": 1})
        return f"echo: {text}"

    def status(self):
        return {"model": self.model, "turns": len(self.asked), "llm_calls": 2, "tool_calls": 1,
                "input_tokens": 300, "output_tokens": 20, "last_prompt_tokens": 250, "context_limit": 1000,
                "transcript": "chat-123"}

    def links(self):
        return {"web": "https://daq/chat/123"}

    def reset(self):
        self.was_reset = True

    async def compact(self):
        raise NotSupported("history is kept on disk")

    def set_model(self, name):
        raise NotSupported()

    def usage_snapshot(self):
        return None  # this backend logs its own usage


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.closed: list[str] = []

    async def open(self): ...
    async def close(self): ...

    async def new_conversation(self, key, context):
        return FakeConv(key, context)

    async def close_conversation(self, conv):
        self.closed.append(conv.key)

    async def list_models(self):
        return []

    def tools_by_server(self):
        return {"daqpy": ["get_app_status", "read_log"], "dcs": ["dcs_epics_get"]}, {"ecl": "token missing"}

    async def check(self):
        return True, ["fake backend: ok"]


def _bot():
    # the aiohttp Socket Mode client wants a running loop at construction
    bot = slackbot.SlackBot(_cfg(), FakeBackend())
    bot.bot_user_id = "UBOT"
    bot.home_channel_id = "CHOME"
    return bot


def _run(coro_fn):
    async def go():
        return await coro_fn(_bot())
    return asyncio.run(go())


def test_should_handle_rules():
    async def case(bot):
        msg = {"type": "message"}
        # mention anywhere
        assert bot._should_handle({"type": "app_mention"}, "CELSE", True, "CELSE:1")
        # follow-up only in a tracked thread in the home channel
        assert not bot._should_handle(msg, "CHOME", False, "CHOME:1")
        bot.conversations["CHOME:1"] = object()
        assert bot._should_handle(msg, "CHOME", False, "CHOME:1")
        bot.conversations["CELSE:1"] = object()
        assert not bot._should_handle(msg, "CELSE", False, "CELSE:1")
        bot.cfg.thread_followups = False
        assert not bot._should_handle(msg, "CHOME", False, "CHOME:1")
        # direct messages need no mention: top-level (new thread) and replies alike
        assert bot._should_handle(msg, "D0123ABCD", False, "D0123ABCD:9")
        bot.conversations["D0123ABCD:9"] = object()
        assert bot._should_handle(msg, "D0123ABCD", False, "D0123ABCD:9")
        # --no-dm: nothing in DMs, not even mentions
        bot.cfg.dm_enabled = False
        assert not bot._should_handle(msg, "D0123ABCD", False, "D0123ABCD:9")
        assert not bot._should_handle({"type": "app_mention"}, "D0123ABCD", True, "D0123ABCD:9")
        bot.cfg.dm_enabled = True
        # channel allowlist: mentions outside it are dropped, inside still work
        bot.cfg.thread_followups = True
        bot.allowed_channel_ids = {"CHOME", "COK"}
        assert bot._should_handle({"type": "app_mention"}, "COK", True, "COK:1")
        assert not bot._should_handle({"type": "app_mention"}, "CELSE", True, "CELSE:1")
        assert bot._should_handle(msg, "CHOME", False, "CHOME:1")       # tracked thread, home
        assert bot._should_handle(msg, "D0123ABCD", False, "D0123ABCD:9")  # DMs unaffected by the allowlist
    _run(case)


def test_parse_rate_and_limiter():
    assert slackbot.parse_rate("10/10m") == (10, 600.0)
    assert slackbot.parse_rate("100/1h") == (100, 3600.0)
    assert slackbot.parse_rate("") == (0, 0.0) and slackbot.parse_rate("0/1m")[0] == 0
    with pytest.raises(ValueError):
        slackbot.parse_rate("ten per minute")
    lim = slackbot.RateLimiter("2/10m")
    assert lim.allow("u")[0] and lim.allow("u")[0]
    ok, wait = lim.allow("u")
    assert not ok and 0 < wait <= 600
    assert lim.allow("other")[0]            # per key
    lim.refund("other")
    assert lim.allow("other")[0]            # refund gave the slot back
    assert slackbot.RateLimiter("")._events == {} and slackbot.RateLimiter("").allow("x") == (True, 0.0)


def test_rate_check_user_then_total():
    async def case(bot):
        assert bot._rate_check("alice") is None and bot._rate_check("alice") is None
        assert bot._rate_check("alice") is not None          # user limit 2/10m
        assert bot._rate_check("bob") is None                 # total now 3/3
        assert bot._rate_check("carol") is not None           # total limit
        # carol's user slot was refunded, so once the total frees she is not doubly penalised
        bot._rate_total = slackbot.RateLimiter("")
        assert bot._rate_check("carol") is None
    _run(case)


def test_dedup_remembers_event_keys():
    async def case(bot):
        assert not bot._already_seen("C:1")
        assert bot._already_seen("C:1")
        assert not bot._already_seen("C:2")
    _run(case)


def test_cleanup_closes_idle_conversations_via_backend():
    async def case(bot):
        old = FakeConv("CHOME:old", {})
        old.last_active = time.monotonic() - 100
        fresh = FakeConv("CHOME:new", {})
        bot.conversations = {"CHOME:old": old, "CHOME:new": fresh}
        n = await bot.cleanup()
        assert n == 1 and list(bot.conversations) == ["CHOME:new"]
        assert bot.backend.closed == ["CHOME:old"]
    _run(case)


def _dispatch(text, conv=None, backend=None):
    conv = conv or FakeConv("k", {})
    backend = backend or FakeBackend()
    return asyncio.run(commands.dispatch(
        text, "!", lambda args: commands.CommandContext(conv=conv, args=args, cfg=_cfg(), backend=backend)))


def test_commands_through_protocol():
    assert "Commands" in _dispatch("!help") and "!links" in _dispatch("!help")
    assert "fixed-model" in _dispatch("!models")
    assert "not available with this bot" in _dispatch("!model other") and "history is kept" not in _dispatch("!model other")
    assert "history is kept on disk" in _dispatch("!compact")
    tools = _dispatch("!tools")
    assert "*daqpy*" in tools and "`dcs_epics_get`" in tools and "unavailable (token missing)" in tools
    status = _dispatch("!status")
    assert "fixed-model" in status and "300 in / 20 out" in status and "25% of budget" in status
    assert "*Transcript* chat-123" in status and "<https://daq/chat/123|web>" in status
    assert "https://daq/chat/123" in _dispatch("!links")
    conv = FakeConv("k", {})
    assert "cleared" in _dispatch("!reset", conv) and conv.was_reset
    assert "Unknown command" in _dispatch("!bogus")


def test_usage_log_skips_backends_that_log_themselves(tmp_path):
    cfg = _cfg(log_output=str(tmp_path / "usage.jsonl"))
    usage_log.record_turn(FakeConv("k", {}), cfg, "C", "1.0", "U")
    assert not (tmp_path / "usage.jsonl").exists()


def test_usage_record_from_snapshot(tmp_path):
    cfg = _cfg(log_output=str(tmp_path / "usage.jsonl"))
    snap = {"provider": "openai", "endpoint_url": "http://llm", "model": "m", "created_at": "2026-10-05T00:00:00",
            "updated_at": "2026-10-05T00:01:00", "turns": 1, "llm_calls": 2, "tool_calls": 1,
            "tool_breakdown": {"t": 1}, "input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 0,
            "cache_write_tokens": 0}
    rec = usage_log.build_record(snap, cfg, "C", "1.0", "U")
    assert rec["session_id"] == "C-1.0" and rec["model"] == "m" and rec["input_tokens"] == 10
    assert rec["user"] == "U" and rec["working_dir"] == "slack://C/1.0"
    usage_log.write(rec, cfg.log_output)
    usage_log.write({**rec, "turns": 2}, cfg.log_output)
    lines = (tmp_path / "usage.jsonl").read_text().splitlines()
    assert len(lines) == 1 and '"turns": 2' in lines[0]


def test_mrkdwn_and_chunks():
    md = "# Title\n\nSome **bold** and [a link](http://x) and\n- item\n```\n**raw**\n```"
    out = slackbot.to_mrkdwn(md)
    assert out.startswith("*Title*") and "*bold*" in out and "<http://x|a link>" in out and "• item" in out
    assert "**raw**" in out  # code fences untouched
    text = "\n".join("x" * 100 for _ in range(60))
    chunks = slackbot._chunks(text)
    assert len(chunks) > 1 and all(len(c) <= slackbot.MAX_MESSAGE_CHARS for c in chunks)
    assert "".join(chunks) == text

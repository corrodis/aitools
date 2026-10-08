"""Adapter tests with a fake backend -- no Slack, no LLM, no network."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace as NS

import pytest

from mu2e_slack import commands, slackbot, usage_log
from mu2e_slack.backend import NotSupported


def _cfg(**kw):
    base = dict(slack_bot_token="xoxb-test", slack_app_token="xapp-test", channel="home",
                thread_followups="asker", followup_window=1800, command_prefix="!", idle_timeout=10,
                log_output="", pg_dsn="", endpoint="http://llm", context_limit=1000,
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
        mention = {"type": "app_mention"}
        # mention anywhere
        assert bot._should_handle(mention, "CELSE", True, "CELSE:1", "alice")
        # follow-up (default: asker): only in a tracked thread, only by someone who mentioned the bot there
        assert not bot._should_handle(msg, "CHOME", False, "CHOME:1", "alice")
        bot.conversations["CHOME:1"] = FakeConv("CHOME:1", {})
        bot._askers["CHOME:1"] = {"alice"}
        assert bot._should_handle(msg, "CHOME", False, "CHOME:1", "alice")
        assert not bot._should_handle(msg, "CHOME", False, "CHOME:1", "bob")      # bystander must mention
        bot.conversations["CELSE:1"] = FakeConv("CELSE:1", {})
        bot._askers["CELSE:1"] = {"alice"}
        assert bot._should_handle(msg, "CELSE", False, "CELSE:1", "alice")        # asker rule works in any channel
        # follow-up window: too long after the last answer → mention needed again
        bot.conversations["CELSE:1"].last_active = time.monotonic() - 5000
        assert not bot._should_handle(msg, "CELSE", False, "CELSE:1", "alice")
        bot.cfg.followup_window = 0
        assert bot._should_handle(msg, "CELSE", False, "CELSE:1", "alice")
        # other modes
        bot.cfg.thread_followups = "home"
        assert bot._should_handle(msg, "CHOME", False, "CHOME:1", "bob")
        assert not bot._should_handle(msg, "CELSE", False, "CELSE:1", "alice")
        bot.cfg.thread_followups = "all"
        assert bot._should_handle(msg, "CELSE", False, "CELSE:1", "bob")
        bot.cfg.thread_followups = "none"
        assert not bot._should_handle(msg, "CHOME", False, "CHOME:1", "alice")
        bot.cfg.thread_followups = "asker"
        # direct messages need no mention: top-level (new thread) and replies alike
        assert bot._should_handle(msg, "D0123ABCD", False, "D0123ABCD:9", "alice")
        bot.conversations["D0123ABCD:9"] = FakeConv("D0123ABCD:9", {})
        assert bot._should_handle(msg, "D0123ABCD", False, "D0123ABCD:9", "alice")
        # --no-dm: nothing in DMs, not even mentions
        bot.cfg.dm_enabled = False
        assert not bot._should_handle(msg, "D0123ABCD", False, "D0123ABCD:9", "alice")
        assert not bot._should_handle(mention, "D0123ABCD", True, "D0123ABCD:9", "alice")
        bot.cfg.dm_enabled = True
        # channel allowlist: mentions outside it are dropped, inside still work
        bot.allowed_channel_ids = {"CHOME", "COK"}
        assert bot._should_handle(mention, "COK", True, "COK:1", "alice")
        assert not bot._should_handle(mention, "CELSE", True, "CELSE:1", "alice")
        assert bot._should_handle(msg, "CHOME", False, "CHOME:1", "alice")        # tracked thread, asker
        assert bot._should_handle(msg, "D0123ABCD", False, "D0123ABCD:9", "alice")  # DMs unaffected
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
    usage_log.record_turn(FakeConv("k", {}), cfg, "C", "1.0")
    assert not (tmp_path / "usage.jsonl").exists()


SNAP = {"endpoint_url": "https://litellm.fnal.gov/v1/", "model": "m", "created_at": "2026-10-05T00:00:00",
        "updated_at": "2026-10-05T00:01:00", "turns": 1, "llm_calls": 2, "tool_calls": 1,
        "tool_breakdown": {"t": 1}, "input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "thinking_tokens": 3}


def test_usage_record_is_the_table_row_and_identifies_nobody(tmp_path):
    cfg = _cfg(log_output=str(tmp_path / "usage.jsonl"))
    rec = usage_log.build_record(SNAP, "C0SECRET", "1791419757.630359")
    assert tuple(rec) == usage_log.COLUMNS
    assert rec["interface"] == "slack" and rec["provider"] == "litellm" and rec["thinking_tokens"] == 3
    assert rec["session_id"].startswith("slack-")
    assert rec["session_id"] == usage_log.build_record(SNAP, "C0SECRET", "1791419757.630359")["session_id"]
    flat = json.dumps(rec)
    assert "C0SECRET" not in flat and "1791419757" not in flat and "user" not in rec
    usage_log.write(rec, cfg.log_output)
    usage_log.write({**rec, "turns": 2}, cfg.log_output)
    lines = (tmp_path / "usage.jsonl").read_text().splitlines()
    assert len(lines) == 1 and '"turns": 2' in lines[0]


class FakePg:
    def __init__(self):
        self.executed = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append((query.as_string(None) if hasattr(query, "as_string") else query, params))


def test_upsert_overwrites_running_totals():
    pytest.importorskip("psycopg")
    conn = FakePg()
    usage_log.upsert(conn, usage_log.build_record(SNAP, "C", "1.0"), "usage.ai_usage")
    (query, params), = conn.executed
    assert query.startswith('INSERT INTO "usage"."ai_usage"')
    assert "ON CONFLICT (session_id) DO UPDATE SET" in query and '"turns" = EXCLUDED."turns"' in query
    assert '"session_id" = EXCLUDED' not in query
    assert set(params) == set(usage_log.COLUMNS)


def test_database_failure_keeps_the_file(tmp_path, monkeypatch, caplog):
    class Conv:
        def usage_snapshot(self):
            return SNAP

    def boom(*a, **k):
        raise OSError("ifdb11 unreachable")

    monkeypatch.setattr(usage_log, "write_postgres", boom)
    cfg = _cfg(log_output=str(tmp_path / "usage.jsonl"), pg_dsn="host=x", pg_table="usage.ai_usage")
    usage_log.record_turn(Conv(), cfg, "C", "1.0")
    assert (tmp_path / "usage.jsonl").exists()
    assert "ifdb11 unreachable" in caplog.text


def test_incoming_markup_is_rendered():
    async def case(bot):
        async def fake_users_info(user):
            return {"user": {"real_name": {"U1": "Alice"}.get(user)}}

        async def fake_conversations_info(channel):
            return {"channel": {"name": {"C04": "crv_vst"}.get(channel)}}
        bot.web.users_info = fake_users_info
        bot.web.conversations_info = fake_conversations_info
        out = await bot._plain_text("<@U1> look at <#C9|mu2e-shift> and <#C04> and <https://x.y/z|the log> &lt;ok&gt; <!here>")
        assert out == "@Alice look at #mu2e-shift and #crv_vst and the log (https://x.y/z) <ok> @here"
        assert bot._user_names == {"U1": "Alice"} and bot._channel_names == {"C04": "crv_vst"}
    _run(case)


def test_mrkdwn_and_chunks():
    md = "# Title\n\nSome **bold** and [a link](http://x) and\n- item\n```\n**raw**\n```"
    out = slackbot.to_mrkdwn(md)
    assert out.startswith("*Title*") and "*bold*" in out and "<http://x|a link>" in out and "• item" in out
    assert "**raw**" in out  # code fences untouched
    text = "\n".join("x" * 100 for _ in range(60))
    chunks = slackbot._chunks(text)
    assert len(chunks) > 1 and all(len(c) <= slackbot.MAX_MESSAGE_CHARS for c in chunks)
    assert "".join(chunks) == text

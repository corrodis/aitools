"""slack-mcp tests with a fake Slack client -- no network."""

from __future__ import annotations

import time
from types import SimpleNamespace as NS

import pytest

from slack_mcp import server as S


# -- pure helpers ----------------------------------------------------------

def test_allowlist_parsing(monkeypatch):
    monkeypatch.setenv("SLACK_MCP_CHANNELS", "#mu2e-shift, C0123456789 test_llm")
    assert S.allowlist() == ["mu2e-shift", "C0123456789", "test_llm"]
    monkeypatch.setenv("SLACK_MCP_CHANNELS", "")
    assert S.allowlist() == []


def test_parse_time_forms():
    now = time.time()
    assert abs((now - S.parse_time("24h")) - 86400) < 5
    assert abs((now - S.parse_time("-7d")) - 7 * 86400) < 5
    assert abs((now - S.parse_time("now-30m")) - 1800) < 5
    assert S.parse_time(1700000000) == 1700000000.0
    assert S.parse_time("1700000000000") == 1700000000.0  # ms
    assert S.parse_time("2026-10-05T12:00:00+00:00") == 1791201600.0
    assert abs(S.parse_time(None) - (now - 86400)) < 5  # default: 24h ago
    with pytest.raises(ValueError):
        S.parse_time("yesterday")


def test_render_text_resolves_markup():
    names = {"U1": "Alice", "U2": "Bob"}
    out = S.render_text("<@U1> see <#C9|mu2e-shift> and <https://x.y/z|the log> or <https://a.b> &lt;ok&gt; <!here>",
                        lambda uid: names.get(uid, uid))
    assert out == "@Alice see #mu2e-shift and the log (https://x.y/z) or https://a.b <ok> @here"


def test_permalink():
    assert S.permalink("https://mu2e.slack.com/", "C1", "1791227465.254249") == \
        "https://mu2e.slack.com/archives/C1/p1791227465254249"
    assert S.permalink("https://mu2e.slack.com", "C1", "1791227470.1", "1791227465.254249").endswith(
        "?thread_ts=1791227465.254249&cid=C1")


# -- reader with a fake client -------------------------------------------

class FakeClient:
    def __init__(self):
        self.calls = []
        self.channels = [
            {"id": "C0000000001", "name": "mu2e-shift", "is_private": False, "topic": {"value": "shift"}, "purpose": {"value": ""}, "num_members": 40},
            {"id": "G0000000002", "name": "test_llm", "is_private": True, "topic": {"value": ""}, "purpose": {"value": "tests"}, "num_members": 3},
            {"id": "C0000000003", "name": "secret", "is_private": False, "topic": {"value": ""}, "purpose": {"value": ""}, "num_members": 9},
        ]
        self.history_rows = [
            {"ts": "100.000001", "user": "U1", "text": "run 125037 started <@U2>", "reply_count": 2},
            {"ts": "200.000002", "user": "U2", "text": "BR corruption again", "thread_ts": "200.000002", "reactions": [{"name": "eyes", "count": 2}]},
            {"ts": "150.000003", "bot_id": "B1", "username": "watchdog", "text": "report", "subtype": "bot_message"},
        ]

    def auth_test(self):
        return {"user": "mu2eshifterbot", "user_id": "UB", "team": "Mu2e", "url": "https://mu2e.slack.com/"}

    def conversations_list(self, types, limit, cursor, exclude_archived):
        self.calls.append(("list", types))
        return {"channels": [c for c in self.channels if c["is_private"] == (types == "private_channel")],
                "response_metadata": {"next_cursor": ""}}

    def conversations_info(self, channel):
        return {"channel": next(c for c in self.channels if c["id"] == channel)}

    def users_info(self, user):
        return {"user": {"real_name": {"U1": "Alice", "U2": "Bob"}[user]}}

    def conversations_history(self, channel, oldest, limit, cursor, latest=None):
        self.calls.append(("history", channel, oldest, latest, limit))
        rows = [r for r in self.history_rows if float(r["ts"]) >= float(oldest)]
        return {"messages": rows[:limit], "has_more": False}

    def conversations_replies(self, channel, ts, limit, cursor):
        self.calls.append(("replies", channel, ts))
        return {"messages": [{"ts": ts, "user": "U1", "text": "parent"},
                             {"ts": "101.0", "user": "U2", "text": "reply 1", "thread_ts": ts},
                             {"ts": "102.0", "user": "U2", "text": "reply 2", "thread_ts": ts}], "has_more": False}


@pytest.fixture
def reader(monkeypatch):
    monkeypatch.setenv("SLACK_MCP_CHANNELS", "mu2e-shift,G0000000002")
    r = S.SlackReader(client=FakeClient())
    monkeypatch.setattr(S, "_reader", r)
    return r


def test_list_channels_only_allowlisted(reader):
    chans = S.list_channels()
    assert [c["name"] for c in chans] == ["mu2e-shift", "test_llm"]
    assert chans[0]["topic"] == "shift" and chans[1]["is_private"]
    assert not any(c["name"] == "secret" for c in chans)


def test_resolve_refuses_non_allowlisted(reader):
    assert reader.resolve("#mu2e-shift")["id"] == "C0000000001"
    assert reader.resolve("G0000000002")["name"] == "test_llm"
    with pytest.raises(ValueError) as e:
        reader.resolve("secret")
    assert "#mu2e-shift (C0000000001)" in str(e.value) and "#secret is not readable" in str(e.value)
    with pytest.raises(ValueError) as e:
        reader.resolve("C0000000003")  # by id either — and the id is named in the message
    assert "#secret (C0000000003) is not readable" in str(e.value)


def test_read_channel_formats_and_orders(reader):
    out = S.read_channel("mu2e-shift", since=0, limit=50)
    assert out["channel"] == {"id": "C0000000001", "name": "mu2e-shift"} and out["count"] == 3
    texts = [m["text"] for m in out["messages"]]
    assert texts == ["run 125037 started @Bob", "report", "BR corruption again"]  # oldest first
    first = out["messages"][0]
    assert first["user"] == "Alice" and first["reply_count"] == 2 and first["thread_ts"] == "100.000001"
    assert first["permalink"] == "https://mu2e.slack.com/archives/C0000000001/p100000001"
    assert out["messages"][1]["user"] == "watchdog" and out["messages"][1]["subtype"] == "bot_message"
    assert out["messages"][2]["reactions"] == {"eyes": 2}
    assert "replies" not in first


def test_read_channel_with_threads_and_thread_tool(reader):
    out = S.read_channel("mu2e-shift", since=0, include_threads=True)
    first = out["messages"][0]
    assert [r["text"] for r in first["replies"]] == ["reply 1", "reply 2"]
    assert first["replies"][0]["in_thread"] == "100.000001"
    th = S.read_thread("mu2e-shift", "100.000001")
    assert th["count"] == 3 and th["messages"][0]["text"] == "parent"


def test_tool_errors_are_tool_errors(reader):
    from mcp.server.mcpserver.exceptions import ToolError
    with pytest.raises(ToolError) as e:
        S.read_channel("secret")
    assert "bad_request" in str(e.value)

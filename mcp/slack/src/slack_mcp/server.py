"""slack-mcp: read-only MCP server for an allowlisted set of Slack channels.

Why it exists: operational channels (#mu2e-shift and friends) hold the human
side of what happened -- what shifters saw, decided and tried -- next to
the machine side that the DAQ, run-log and DCS servers expose. An agent
that can read those channels can answer "what did the shifters report
overnight?" or put a log error into context.

What it deliberately is not: a Slack client. Three read tools, nothing that
posts, reacts, searches the workspace or lists channels beyond the ones an
operator put in ``SLACK_MCP_CHANNELS``. Channels outside the allowlist do
not exist as far as a caller is concerned -- they are neither listed nor
readable by id.

Transport: streamable-HTTP like every ``mcp/*`` server here (``slack-mcp``),
plus stdio (``slack-mcp-stdio``) so a local agent such as daqpy can spawn
it as a subprocess. Auth: mikey bearer tokens when ``MIKEY_KEYS_FILE`` is
set (HTTP only; stdio is already local).

Slack app scopes needed by the bot token: ``channels:history``,
``channels:read``, ``users:read``; for private channels also
``groups:history`` and ``groups:read``. The bot must be a member of each
allowlisted channel.
"""

from __future__ import annotations

import argparse
import functools
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mikey import build_auth_kwargs

from . import version as _version

LOGGER = logging.getLogger("slack_mcp")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8009
MAX_MESSAGES = int(os.environ.get("SLACK_MCP_MAX_MESSAGES", "500"))
PAGE = 200  # Slack's conversations.history page size

INSTRUCTIONS = (
    "Read-only access to a fixed, operator-chosen set of Mu2e Slack channels "
    "(e.g. #mu2e-shift). Use it for the human side of operations: what "
    "shifters and experts reported, decided or asked, and when.\n\n"
    "Flow: slack_list_channels to see which channels exist here (only those; "
    "nothing else is readable), slack_read_channel for recent messages in one "
    "of them (default last 24h, newest last), slack_read_thread for the "
    "replies under a message. Messages carry an ISO time, the author's name, "
    "the text with @mentions resolved, a reply count and a permalink. "
    "Nothing here can post or search."
)

# Constructed at import time so @mcp.tool() registers before main() runs;
# build_auth_kwargs() reads MIKEY_KEYS_FILE and returns {} (auth off) if unset.
mcp = MCPServer("slack", instructions=INSTRUCTIONS, **build_auth_kwargs())


# ---------------------------------------------------------------------------
# configuration / helpers (pure, testable)
# ---------------------------------------------------------------------------

def allowlist() -> list[str]:
    """Channel names (without #) or ids from SLACK_MCP_CHANNELS."""
    raw = os.environ.get("SLACK_MCP_CHANNELS", "")
    return [c.strip().lstrip("#") for c in re.split(r"[,\s]+", raw) if c.strip()]


_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_time(value: str | float | int | None, default_ago: str = "24h") -> float:
    """``"24h"``/``"-7d"``/``"now-2h"`` (ago), ISO 8601, or unix epoch → epoch seconds."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "now")):
        return time.time() if value is not None else time.time() - parse_span(default_ago)
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 1e11 else float(value)
    s = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return parse_time(float(s))
    m = re.fullmatch(r"(?:now)?\s*-?\s*(\d+(?:\.\d+)?)\s*([mhdw])", s, re.I)
    if m:
        return time.time() - float(m.group(1)) * _UNITS[m.group(2).lower()]
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def parse_span(spec: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mhdw])\s*", spec, re.I)
    if not m:
        raise ValueError(f"bad time span {spec!r} (use e.g. 30m, 24h, 7d)")
    return float(m.group(1)) * _UNITS[m.group(2).lower()]


def ts_to_iso(ts: str | float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def permalink(team_url: str, channel_id: str, ts: str, thread_ts: str | None = None) -> str:
    base = f"{team_url.rstrip('/')}/archives/{channel_id}/p{str(ts).replace('.', '')}"
    return f"{base}?thread_ts={thread_ts}&cid={channel_id}" if thread_ts and thread_ts != ts else base


_MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL_REF = re.compile(r"<#([A-Z0-9]+)\|([^>]*)>")
_LINK = re.compile(r"<(https?://[^|>]+)\|([^>]*)>")
_BARE_LINK = re.compile(r"<(https?://[^>]+)>")
_SPECIAL = re.compile(r"<!(channel|here|everyone)>")


def render_text(text: str, user_name) -> str:
    """Slack message markup → plain text: @mentions by name, #channel, links as 'text (url)'."""
    text = _MENTION.sub(lambda m: "@" + user_name(m.group(1)), text or "")
    text = _CHANNEL_REF.sub(lambda m: "#" + (m.group(2) or m.group(1)), text)
    text = _LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)
    text = _BARE_LINK.sub(lambda m: m.group(1), text)
    text = _SPECIAL.sub(lambda m: "@" + m.group(1), text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


# ---------------------------------------------------------------------------
# Slack access
# ---------------------------------------------------------------------------

class SlackReader:
    """Thin, cached wrapper over slack_sdk's WebClient for the three tools."""

    def __init__(self, client=None, token: str | None = None):
        if client is None:
            token = token or os.environ.get("SLACK_BOT_TOKEN", "")
            if not token:
                raise ValueError("SLACK_BOT_TOKEN is not set")
            from slack_sdk import WebClient
            client = WebClient(token=token)
        self.client = client
        self._lock = threading.Lock()
        self._users: dict[str, str] = {}
        self._channels: list[dict[str, Any]] | None = None
        self._team_url = ""
        self._bot_user = ""

    # -- identity -----------------------------------------------------------

    def connect(self) -> dict[str, Any]:
        auth = self.client.auth_test()
        self._team_url = auth.get("url", "")
        self._bot_user = auth.get("user", "")
        return {"user": self._bot_user, "user_id": auth.get("user_id"), "team": auth.get("team"), "url": self._team_url}

    @property
    def team_url(self) -> str:
        if not self._team_url:
            self.connect()
        return self._team_url

    # -- channels -----------------------------------------------------------

    def channels(self, refresh: bool = False) -> list[dict[str, Any]]:
        """Resolve the allowlist to channel records; cached."""
        with self._lock:
            if self._channels is not None and not refresh:
                return self._channels
            wanted = allowlist()
            if not wanted:
                raise ValueError("SLACK_MCP_CHANNELS is empty -- no channel is readable")
            by_name: dict[str, dict[str, Any]] = {}
            names_wanted = [w for w in wanted if not re.fullmatch(r"[CG][A-Z0-9]{8,}", w)]
            if names_wanted:
                by_name = self._list_channels_by_name()
            out: list[dict[str, Any]] = []
            for w in wanted:
                if re.fullmatch(r"[CG][A-Z0-9]{8,}", w):
                    out.append(self._channel_info(w))
                elif w in by_name:
                    out.append(self._describe(by_name[w]))
                else:
                    out.append({"id": None, "name": w, "error": "not found (not a member, or missing groups:read for a private channel)"})
            self._channels = out
            return out

    def _list_channels_by_name(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for types in ("public_channel", "private_channel"):
            cursor = None
            try:
                while True:
                    resp = self.client.conversations_list(types=types, limit=200, cursor=cursor, exclude_archived=True)
                    for ch in resp.get("channels", []):
                        found[ch["name"]] = ch
                    cursor = (resp.get("response_metadata") or {}).get("next_cursor")
                    if not cursor:
                        break
            except Exception as exc:  # noqa: BLE001  -- e.g. missing_scope groups:read
                LOGGER.info("conversations.list(%s) failed: %s", types, _slack_error(exc))
        return found

    def _channel_info(self, channel_id: str) -> dict[str, Any]:
        try:
            return self._describe(self.client.conversations_info(channel=channel_id)["channel"])
        except Exception as exc:  # noqa: BLE001
            return {"id": channel_id, "name": channel_id, "note": f"info unavailable ({_slack_error(exc)})"}

    @staticmethod
    def _describe(ch: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": ch.get("id"), "name": ch.get("name"), "is_private": bool(ch.get("is_private")),
            "topic": (ch.get("topic") or {}).get("value") or "", "purpose": (ch.get("purpose") or {}).get("value") or "",
            "members": ch.get("num_members"),
        }

    def resolve(self, channel: str) -> dict[str, Any]:
        """Allowlisted channel by name or id, else ValueError (never leaks other channels)."""
        key = (channel or "").strip().lstrip("#")
        for ch in self.channels():
            if ch.get("id") and (key == ch["id"] or key == ch.get("name")):
                return ch
        names = ", ".join(f"#{c.get('name')}" for c in self.channels() if c.get("id"))
        raise ValueError(f"channel {channel!r} is not available here; readable: {names or '(none)'}")

    # -- users --------------------------------------------------------------

    def user_name(self, user_id: str) -> str:
        if not user_id:
            return "?"
        with self._lock:
            if user_id in self._users:
                return self._users[user_id]
        name = user_id
        try:
            u = self.client.users_info(user=user_id)["user"]
            name = u.get("real_name") or (u.get("profile") or {}).get("display_name") or u.get("name") or user_id
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("users.info(%s) failed: %s", user_id, _slack_error(exc))
        with self._lock:
            self._users[user_id] = name
        return name

    # -- messages -----------------------------------------------------------

    def history(self, channel_id: str, oldest: float, latest: float | None, limit: int) -> list[dict[str, Any]]:
        """Newest ``limit`` messages in (oldest, latest]; the caller sorts.

        Slack pages newest-first only when ``latest`` is given (with just
        ``oldest`` it starts at the oldest end), so ``latest`` always is.
        """
        limit = max(1, min(int(limit), MAX_MESSAGES))
        latest = latest or time.time()
        out: list[dict[str, Any]] = []
        cursor = None
        while len(out) < limit:
            resp = self._call(self.client.conversations_history, channel_id, channel=channel_id,
                              oldest=f"{oldest:.6f}", latest=f"{latest:.6f}",
                              limit=min(PAGE, limit - len(out)), cursor=cursor)
            out.extend(resp.get("messages", []))
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not resp.get("has_more") or not cursor:
                break
        return out[:limit]

    def replies(self, channel_id: str, thread_ts: str, limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), MAX_MESSAGES))
        out: list[dict[str, Any]] = []
        cursor = None
        while len(out) < limit:
            resp = self._call(self.client.conversations_replies, channel_id, channel=channel_id, ts=thread_ts,
                              limit=min(PAGE, limit - len(out)), cursor=cursor)
            out.extend(resp.get("messages", []))
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not resp.get("has_more") or not cursor:
                break
        return out[:limit]

    def _call(self, method, channel_id: str, **kwargs):
        """Slack call with the two expected failures turned into actionable messages."""
        try:
            return method(**kwargs)
        except Exception as exc:  # noqa: BLE001
            err = _slack_error(exc)
            name = next((f"#{c.get('name')}" for c in (self._channels or []) if c.get("id") == channel_id), channel_id)
            if err.startswith("not_in_channel"):
                raise ValueError(f"the bot is not a member of {name}; an admin must invite it (/invite) before it can be read") from exc
            if err.startswith("missing_scope"):
                raise ValueError(f"the Slack app lacks a scope for {name}: {err}") from exc
            raise

    def format(self, msg: dict[str, Any], channel_id: str) -> dict[str, Any]:
        ts = msg.get("ts", "")
        thread_ts = msg.get("thread_ts")
        row: dict[str, Any] = {
            "ts": ts,
            "time": ts_to_iso(ts) if ts else None,
            "user": self.user_name(msg["user"]) if msg.get("user") else (msg.get("username") or ("bot" if msg.get("bot_id") else "?")),
            "text": render_text(msg.get("text", ""), self.user_name),
            "permalink": permalink(self.team_url, channel_id, ts, thread_ts) if ts else None,
        }
        if msg.get("subtype"):
            row["subtype"] = msg["subtype"]
        if msg.get("reply_count"):
            row["reply_count"] = msg["reply_count"]
            row["thread_ts"] = ts
        elif thread_ts and thread_ts != ts:
            row["in_thread"] = thread_ts
        if msg.get("files"):
            row["files"] = [f.get("name") or f.get("title") for f in msg["files"]]
        if msg.get("reactions"):
            row["reactions"] = {r.get("name"): r.get("count") for r in msg["reactions"]}
        return row


def _slack_error(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    try:
        err = resp.get("error") if resp is not None else None
        needed = resp.get("needed") if resp is not None else None
        return f"{err}{f' (needs scope {needed})' if needed else ''}" if err else str(exc)
    except Exception:  # noqa: BLE001
        return str(exc)


_reader_lock = threading.Lock()
_reader: SlackReader | None = None


def get_reader() -> SlackReader:
    """Process-wide reader, built lazily from SLACK_BOT_TOKEN (env only, never a flag)."""
    global _reader
    with _reader_lock:
        if _reader is None:
            _reader = SlackReader()
        return _reader


def _wrap(fn):
    """Exceptions → ToolError (the SDK's mechanism for expected failures); see ecl-mcp."""

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as e:
            raise ToolError(f"bad_request: {e}") from e
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("slack-mcp tool %s failed", fn.__name__)
            raise ToolError(f"tool_failed: {type(e).__name__}: {_slack_error(e)}") from e

    return inner


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

@mcp.tool(name="slack_server_info", description="Get slack-mcp configuration and version info.")
def get_server_info() -> dict[str, Any]:
    # Prefixed (unlike other mcp/* servers' get_server_info) because clients
    # that merge several servers into one tool list -- daqpy -- reject
    # duplicate names.
    return {
        "name": "slack",
        "version": _version(),
        "transport": "streamable-http",
        "read_only": True,
        "channels": allowlist(),
        "max_messages": MAX_MESSAGES,
        "auth": "mikey" if mcp.settings.auth else "disabled",
    }


@mcp.tool(name="slack_list_channels")
@_wrap
def list_channels(refresh: bool = False) -> list[dict[str, Any]]:
    """The Slack channels readable through this server (an operator-set allowlist),
    with id, name, privacy, topic, purpose and member count. Nothing else is
    readable. ``refresh=True`` re-resolves names (e.g. after the bot was invited)."""
    return get_reader().channels(refresh=refresh)


@mcp.tool(name="slack_read_channel")
@_wrap
def read_channel(channel: str, since: str = "24h", until: str | None = None, limit: int = 100,
                 include_threads: bool = False) -> dict[str, Any]:
    """Recent messages of one allowlisted channel, oldest first.

    ``channel``: name (with or without #) or id from slack_list_channels.
    ``since``/``until``: relative ("24h", "7d", "30m"), ISO 8601, or unix
    epoch; ``until`` defaults to now. ``limit`` caps the messages (max 500).
    Each message: time (ISO), user (name), text (mentions resolved),
    permalink, reply_count/thread_ts when it starts a thread, files,
    reactions. ``include_threads=True`` also returns each thread's replies
    (nested under ``replies``), within the same overall limit.
    """
    r = get_reader()
    ch = r.resolve(channel)
    oldest = parse_time(since)
    latest = parse_time(until) if until else None
    raw = r.history(ch["id"], oldest, latest, limit)
    raw.sort(key=lambda m: float(m.get("ts", 0)))
    msgs = [r.format(m, ch["id"]) for m in raw]
    budget = max(0, MAX_MESSAGES - len(msgs))
    if include_threads:
        for m in msgs:
            if m.get("reply_count") and budget > 0:
                reps = r.replies(ch["id"], m["ts"], min(budget, m["reply_count"] + 1))
                m["replies"] = [r.format(x, ch["id"]) for x in reps if x.get("ts") != m["ts"]]
                budget -= len(m["replies"])
    return {
        "channel": {"id": ch["id"], "name": ch.get("name")},
        "since": ts_to_iso(oldest), "until": ts_to_iso(latest) if latest else None,
        "count": len(msgs), "truncated": len(raw) >= min(int(limit), MAX_MESSAGES),
        "messages": msgs,
    }


@mcp.tool(name="slack_read_thread")
@_wrap
def read_thread(channel: str, thread_ts: str, limit: int = 200) -> dict[str, Any]:
    """All messages of one thread (the parent first) in an allowlisted channel.
    ``thread_ts`` is the parent's ``ts`` / ``thread_ts`` from slack_read_channel."""
    r = get_reader()
    ch = r.resolve(channel)
    raw = r.replies(ch["id"], thread_ts, limit)
    raw.sort(key=lambda m: float(m.get("ts", 0)))
    return {"channel": {"id": ch["id"], "name": ch.get("name")}, "thread_ts": thread_ts,
            "count": len(raw), "messages": [r.format(m, ch["id"]) for m in raw]}


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="slack-mcp", description=__doc__)
    parser.add_argument("--host", default=os.environ.get("SLACK_MCP_HOST", DEFAULT_HOST),
                        help="bind address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SLACK_MCP_PORT", str(DEFAULT_PORT))),
                        help="bind port (default: %(default)s)")
    parser.add_argument("--transport", choices=("streamable-http", "stdio"), default="streamable-http",
                        help="stdio for a local agent spawning this as a subprocess (default: %(default)s)")
    parser.add_argument("--check", action="store_true",
                        help="verify SLACK_BOT_TOKEN (auth.test) and resolve SLACK_MCP_CHANNELS, print, exit. "
                             "Makes live Slack API calls; reads no messages.")
    return parser.parse_args(argv)


def _check() -> int:
    try:
        r = get_reader()
        who = r.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {_slack_error(exc)}", file=sys.stderr)
        return 1
    print(f"slack-mcp {_version()}  auth={'mikey' if mcp.settings.auth else 'disabled'}")
    print(f"Slack: {who['user']} ({who['user_id']}) in {who['team']}  {who['url']}")
    chans = allowlist()
    if not chans:
        print("FAILED: SLACK_MCP_CHANNELS is empty", file=sys.stderr)
        return 1
    ok = True
    for ch in r.channels():
        if ch.get("id"):
            print(f"  #{ch.get('name')} ({ch['id']}){' private' if ch.get('is_private') else ''}"
                  f"{'  ' + ch['note'] if ch.get('note') else ''}")
        else:
            ok = False
            print(f"  {ch['name']}: {ch.get('error')}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=os.environ.get("SLACK_MCP_LOG_LEVEL", "INFO"), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    if args.check:
        sys.exit(_check())
    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return
    LOGGER.info("Starting slack MCP server over streamable-http on %s:%s (channels=%s, auth=%s)",
                args.host, args.port, ",".join(allowlist()) or "-", "mikey" if mcp.settings.auth else "disabled")
    mcp.run(transport="streamable-http", host=args.host, port=args.port)


def main_stdio() -> None:
    """``slack-mcp-stdio``: for agents that spawn MCP servers as subprocesses (daqpy)."""
    level = os.environ.get("SLACK_MCP_LOG_LEVEL", "WARNING")
    logging.basicConfig(level=level, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("mcp", "mcp.server", "mcp.server.lowlevel.server"):
        logging.getLogger(noisy).setLevel(level)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

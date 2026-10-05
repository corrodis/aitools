"""Slack adapter: Socket Mode in, threads out.

Socket Mode (rather than an HTTP Request URL) because the host this runs on
has no inbound path from Slack -- the bot dials out, like the mcp/* services
do not have to.

What the bot acts on, and nothing else:

  * a message that @-mentions it, in any channel it has been invited to
    (and, if --channels is set, only in those);
  * a reply, without a mention, in a thread it already holds a conversation
    for -- by default only from someone who has mentioned it in that thread
    (--thread-followups asker) and only within --followup-window of its last
    answer; other people must mention it. "home" restores the original
    rule (anyone, home channel only), "all" allows anyone anywhere, "none"
    requires a mention every time;
  * any message in a direct message with it -- a top-level DM starts a
    thread, which is the conversation, and replies in it continue it.

The follow-up rule is what makes a thread feel like a conversation instead
of a sequence of @-prefixed commands; restricting it to the people who asked
keeps a busy human thread from turning every reply into a model call. Slack
delivers every message in subscribed channels to this process (the Events
API has no per-thread subscription), so the filter below runs before
anything else: a message that is not addressed to the bot is dropped where
it arrives -- never logged, never sent to the model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time

from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from . import commands, usage_log
from .backend import Backend, Conversation

log = logging.getLogger(__name__)

# Rotated at random purely so the channel does not feel like a cron job. All
# standard Slack aliases -- a name this workspace does not have simply fails the
# reactions.add call, which is already best-effort.
WORKING_EMOJI = (
    "eyes", "thinking_face", "mag", "microscope", "telescope", "brain",
    "hourglass_flowing_sand", "stopwatch", "gear", "robot_face", "satellite",
    "books", "bulb", "abacus", "atom_symbol", "zap",
)

MAX_MESSAGE_CHARS = 3500
SEEN_TTL = 300  # seconds to remember an event id for de-duplication


class SlackBot:
    """Socket Mode adapter in front of any :class:`~mu2e_slack.backend.Backend`."""

    def __init__(self, cfg, backend: Backend):
        self.cfg = cfg
        self.backend = backend
        self.web = AsyncWebClient(token=cfg.slack_bot_token)
        self.socket = SocketModeClient(app_token=cfg.slack_app_token, web_client=self.web)
        self.bot_user_id: str = ""
        self.workspace_url: str = ""
        self.home_channel_id: str = ""
        self.allowed_channel_ids: set[str] = set()   # empty = no restriction
        self.conversations: dict[str, Conversation] = {}
        self._askers: dict[str, set[str]] = {}   # thread key -> users who mentioned the bot there
        self._user_names: dict[str, str] = {}
        self._seen: dict[str, float] = {}
        # Brakes: per-user and global turn rates, and turns in flight. A bot
        # that can call tools and an LLM must not be able to "go crazy" on a
        # burst of messages, a mention storm, or a misbehaving client.
        self._rate_user = RateLimiter(getattr(cfg, "rate_user", "10/10m"))
        self._rate_total = RateLimiter(getattr(cfg, "rate_total", "60/10m"))
        self._inflight = asyncio.Semaphore(max(1, int(getattr(cfg, "max_concurrent", 3))))
        self._limit_notified: dict[str, float] = {}

    # -- lifecycle ------------------------------------------------------------

    async def connect(self) -> None:
        auth = await self.web.auth_test()
        self.bot_user_id = auth["user_id"]
        self.workspace_url = auth.get("url", "")
        log.info("Authenticated as %s (%s)", auth.get("user"), self.bot_user_id)

        if self.cfg.channel:
            self.home_channel_id = await self._resolve_channel(self.cfg.channel)
            log.info("Home channel: %s (%s)", self.cfg.channel, self.home_channel_id)
        else:
            log.info("No home channel configured -- mention-only in every channel")

        allowed = list(getattr(self.cfg, "allowed_channels", []) or [])
        if allowed:
            ids = {await self._resolve_channel(c) for c in allowed}
            if self.home_channel_id:
                ids.add(self.home_channel_id)
            self.allowed_channel_ids = ids
            log.info("Acting only in channels: %s", ", ".join(sorted(ids)))
        if not getattr(self.cfg, "dm_enabled", True):
            log.info("Direct messages disabled")

    async def start(self) -> None:
        self.socket.socket_mode_request_listeners.append(self._on_request)
        await self.socket.connect()
        log.info("Connected to Slack (Socket Mode)")

    async def close(self) -> None:
        await self.socket.disconnect()
        await self.socket.close()

    async def _resolve_channel(self, channel: str) -> str:
        if re.fullmatch(r"[CG][A-Z0-9]{8,}", channel):
            return channel
        cursor = None
        while True:
            resp = await self.web.conversations_list(
                types="public_channel,private_channel", limit=200, cursor=cursor
            )
            for entry in resp["channels"]:
                if entry["name"] == channel:
                    return entry["id"]
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                raise RuntimeError(f"Channel not found (is the bot a member?): {channel}")

    # -- event intake ---------------------------------------------------------

    async def _on_request(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        # Acknowledge first, always: Slack retries anything unacked within
        # 3 seconds, and an agent turn takes far longer than that.
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event") or {}
        if event.get("type") not in ("app_mention", "message"):
            return
        # Bots (including this one) and edits/joins/deletions are not turns.
        if event.get("bot_id") or event.get("subtype") or event.get("user") == self.bot_user_id:
            return

        channel = event.get("channel", "")
        ts = event.get("ts", "")
        text = event.get("text", "")
        thread_ts = event.get("thread_ts") or ts
        key = f"{channel}:{thread_ts}"
        mentioned = f"<@{self.bot_user_id}>" in text

        # Metadata only -- never the message text, which is why this can stay on
        # at INFO. Without it there is no way to tell a missing subscription
        # (event never arrives) from a rejected post (event handled, reply lost).
        log.info("event type=%s channel=%s ts=%s thread=%s mentioned=%s",
                 event.get("type"), channel, ts, thread_ts, mentioned)

        if not self._should_handle(event, channel, mentioned, key, event.get("user", "")):
            log.info("  dropped: not addressed to me (known thread=%s, home=%s)",
                     key in self.conversations, channel == self.home_channel_id)
            return
        if mentioned and not _is_dm(channel):
            self._askers.setdefault(key, set()).add(event.get("user", ""))

        # A mention in a channel arrives twice: once as app_mention, once as
        # message. Whichever lands first wins.
        if self._already_seen(f"{channel}:{ts}"):
            return

        clean = re.sub(rf"<@{self.bot_user_id}>", "", text).strip()
        asyncio.create_task(self._respond(event, channel, thread_ts, key, clean))

    async def _plain_text(self, text: str) -> str:
        """Slack markup → what a human sees: <#C…|name> → #name, <@U…> → @Name,
        <url|label> → label (url). Otherwise the model reads channel/user ids
        and repeats them back."""
        async def user_name(uid: str) -> str:
            if uid in self._user_names:
                return self._user_names[uid]
            name = uid
            try:
                info = await self.web.users_info(user=uid)
                name = info["user"].get("real_name") or info["user"].get("name") or uid
            except Exception:  # noqa: BLE001
                pass
            self._user_names[uid] = name
            return name

        for uid in set(re.findall(r"<@([A-Z0-9]+)(?:\|[^>]*)?>", text)):
            text = re.sub(rf"<@{uid}(?:\|[^>]*)?>", "@" + await user_name(uid), text)
        text = re.sub(r"<#([A-Z0-9]+)\|([^>]*)>", lambda m: "#" + (m.group(2) or m.group(1)), text)
        text = re.sub(r"<(https?://[^|>]+)\|([^>]*)>", lambda m: f"{m.group(2)} ({m.group(1)})", text)
        text = re.sub(r"<(https?://[^>]+)>", lambda m: m.group(1), text)
        text = re.sub(r"<!(channel|here|everyone)>", lambda m: "@" + m.group(1), text)
        return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")

    def _should_handle(self, event: dict, channel: str, mentioned: bool, key: str, user: str = "") -> bool:
        # Direct messages: everything is addressed to the bot. A top-level DM
        # starts a thread (the conversation); replies in it continue it. Needs
        # the im:history scope and the message.im event subscription.
        if _is_dm(channel):
            return bool(getattr(self.cfg, "dm_enabled", True))
        # Channel allowlist (if any): nothing outside it, mention or not.
        if self.allowed_channel_ids and channel not in self.allowed_channel_ids:
            return False
        if mentioned:
            return True
        if event.get("type") != "message":
            return False
        # Follow-up without a mention: only in a thread this process already
        # holds a conversation for, per --thread-followups and the window.
        mode = getattr(self.cfg, "thread_followups", "asker")
        if mode is True:    # pre-mode configs
            mode = "home"
        conv = self.conversations.get(key)
        if mode in ("none", False) or conv is None:
            return False
        if mode == "home":
            ok = channel == self.home_channel_id
        elif mode == "all":
            ok = True
        else:  # asker
            ok = bool(user) and user in self._askers.get(key, set())
        if not ok:
            return False
        window = int(getattr(self.cfg, "followup_window", 0) or 0)
        return not window or (time.monotonic() - conv.last_active) <= window

    def _already_seen(self, event_key: str) -> bool:
        now = time.monotonic()
        for stale in [k for k, seen in self._seen.items() if now - seen > SEEN_TTL]:
            del self._seen[stale]
        if event_key in self._seen:
            return True
        self._seen[event_key] = now
        return False

    # -- turns ----------------------------------------------------------------

    async def _respond(self, event: dict, channel: str, thread_ts: str, key: str, text: str) -> None:
        user = event.get("user", "")
        # Chosen once per turn and remembered, because the reaction has to be
        # removed by the same name it was added with.
        working = random.choice(WORKING_EMOJI)
        try:
            conv = self.conversations.get(key)
            if conv is None:
                conv = await self.backend.new_conversation(
                    key, await self._context(channel, thread_ts, user))
                self.conversations[key] = conv

            text = await self._plain_text(text)

            # One turn at a time per thread: two quick messages must not
            # interleave tool calls in the same message list.
            async with conv.lock:
                if commands.is_command(text, self.cfg.command_prefix):
                    reply = await commands.dispatch(
                        text, self.cfg.command_prefix,
                        lambda args: commands.CommandContext(
                            conv=conv, args=args, cfg=self.cfg, backend=self.backend,
                        ),
                    )
                    await self._post(channel, thread_ts, reply)
                    return

                retry = self._rate_check(user)
                if retry is not None:
                    await self._notify_limit(channel, thread_ts, key, retry)
                    return

                async with self._inflight:
                    await self._react(channel, event.get("ts", ""), working)
                    status = self._tool_notifier(conv, channel, thread_ts)
                    answer = await conv.ask(text, on_tool=status)
                    await status.finish()
                    await self._post(channel, thread_ts, answer)
                usage_log.record_turn(conv, self.cfg, channel, thread_ts, user)
        except Exception as exc:
            log.exception("Turn failed in %s", key)
            try:
                await self._post(channel, thread_ts, f"Sorry, that went wrong: `{exc}`")
            except Exception:
                log.error("Could not deliver the error report either")
        finally:
            await self._react(channel, event.get("ts", ""), working, remove=True)

    def _rate_check(self, user: str) -> float | None:
        """None if a turn may run now; otherwise seconds until it could."""
        ok_user, wait_user = self._rate_user.allow(user or "?")
        if not ok_user:
            log.info("rate limit (user %s): retry in %.0fs", user, wait_user)
            return wait_user
        ok_total, wait_total = self._rate_total.allow("*")
        if not ok_total:
            self._rate_user.refund(user or "?")  # the user's slot was not used
            log.info("rate limit (total): retry in %.0fs", wait_total)
            return wait_total
        return None

    async def _notify_limit(self, channel: str, thread_ts: str, key: str, retry: float) -> None:
        """One notice per thread per window; later messages are dropped silently."""
        now = time.monotonic()
        if now - self._limit_notified.get(key, -1e9) < retry:
            return
        self._limit_notified[key] = now
        mins = max(1, int(retry // 60 + (1 if retry % 60 else 0)))
        await self._post(channel, thread_ts,
                         f"_I'm rate-limited right now — please try again in about {mins} min._")

    async def _context(self, channel: str, thread_ts: str, user: str) -> dict:
        context = {"interface": "slack", "is_dm": _is_dm(channel)}
        try:
            info = await self.web.users_info(user=user)
            context["user_name"] = info["user"].get("real_name") or info["user"].get("name", "")
        except Exception:
            pass
        if not context["is_dm"]:
            try:
                info = await self.web.conversations_info(channel=channel)
                context["slack_channel"] = info["channel"].get("name", "")
            except Exception:
                pass
        if self.workspace_url:
            context["thread_url"] = f"{self.workspace_url}archives/{channel}/p{thread_ts.replace('.', '')}"
        return context

    def _tool_notifier(self, conv, channel: str, thread_ts: str) -> "_ToolStatus":
        return _ToolStatus(self, conv, channel, thread_ts)

    # -- posting --------------------------------------------------------------

    async def _post(self, channel: str, thread_ts: str, text: str) -> None:
        for chunk in _chunks(to_mrkdwn(text)):
            try:
                await self.web.chat_postMessage(channel=channel, thread_ts=thread_ts, text=chunk)
            except SlackApiError as exc:
                # Nearly always a scope or membership problem (missing_scope,
                # not_in_channel, channel_not_found). Name it in the log: the
                # turn already cost a model call, and silently dropping the
                # answer looks identical to never having received the message.
                log.error("chat_postMessage to %s failed: %s", channel,
                          exc.response.get("error", exc))
                raise

    async def _react(self, channel: str, ts: str, emoji: str, remove: bool = False) -> None:
        """Progress feedback. Best effort -- it needs the reactions:write scope,
        and a missing reaction is not worth failing a turn over."""
        if not ts:
            return
        try:
            if remove:
                await self.web.reactions_remove(channel=channel, timestamp=ts, name=emoji)
            else:
                await self.web.reactions_add(channel=channel, timestamp=ts, name=emoji)
        except SlackApiError as exc:
            log.debug("reaction %s on %s refused: %s", emoji, ts,
                      exc.response.get("error", exc))

    # -- housekeeping ---------------------------------------------------------

    async def cleanup(self) -> int:
        """Forget conversations nobody has touched in a while. Their usage
        records are already on disk; the backend releases whatever else it
        holds for them (sessions, subprocesses)."""
        now = time.monotonic()
        stale = [
            key for key, conv in self.conversations.items()
            if now - conv.last_active > self.cfg.idle_timeout and not conv.lock.locked()
        ]
        for key in stale:
            conv = self.conversations.pop(key)
            self._askers.pop(key, None)
            try:
                await self.backend.close_conversation(conv)
            except Exception:  # noqa: BLE001
                log.exception("close_conversation failed for %s", key)
        if stale:
            log.info("Dropped %d idle conversation(s)", len(stale))
        return len(stale)

    async def close_all(self) -> None:
        for key, conv in list(self.conversations.items()):
            try:
                await self.backend.close_conversation(conv)
            except Exception:  # noqa: BLE001
                log.exception("close_conversation failed for %s", key)
        self.conversations.clear()
        self._askers.clear()


class _ToolStatus:
    """One status message per turn, edited in place as tools run.

    A line per tool call buried the actual answer under eleven notifications in
    the first real thread we tried. Slack lets a bot edit its own message, so
    the thread keeps a single "working" line that updates, and ends as a one
    line record of what was used.
    """

    def __init__(self, bot: "SlackBot", conv, channel: str, thread_ts: str):
        self._bot = bot
        self._conv = conv
        self._channel = channel
        self._thread_ts = thread_ts
        self._ts: str | None = None
        self._calls: list[str] = []

    async def __call__(self, name: str, arguments: dict) -> None:
        if not self._conv.tool_notifications:
            return
        detail = ", ".join(f"{k}={_short(v)}" for k, v in list(arguments.items())[:3])
        self._calls.append(name)
        line = f"_:wrench: `{name}`{f' ({detail})' if detail else ''}_"
        await self._render(line)

    async def finish(self) -> None:
        """Collapse to a summary once the answer is ready."""
        if self._ts is None or not self._calls:
            return
        counts: dict[str, int] = {}
        for name in self._calls:
            counts[name] = counts.get(name, 0) + 1
        used = ", ".join(f"`{n}`" + (f" ×{c}" if c > 1 else "") for n, c in counts.items())
        await self._render(f"_:wrench: used {used}_")

    async def _render(self, text: str) -> None:
        try:
            if self._ts is None:
                resp = await self._bot.web.chat_postMessage(
                    channel=self._channel, thread_ts=self._thread_ts, text=text)
                self._ts = resp["ts"]
            else:
                await self._bot.web.chat_update(
                    channel=self._channel, ts=self._ts, text=text)
        except SlackApiError as exc:
            # Progress display is never worth failing a turn over.
            log.debug("tool status update failed: %s", exc.response.get("error", exc))


_RATE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_rate(spec: str) -> tuple[int, float]:
    """``"10/10m"`` → (10, 600.0); ``"0/…"`` or ``""`` disables the limit."""
    spec = (spec or "").strip()
    if not spec:
        return 0, 0.0
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)\s*([smhd])", spec, re.I)
    if not m:
        raise ValueError(f"bad rate {spec!r}; use N/period such as 10/10m or 100/1h")
    return int(m.group(1)), float(m.group(2)) * _RATE_UNITS[m.group(3).lower()]


class RateLimiter:
    """Sliding-window limiter: at most ``n`` events per ``window`` seconds per key."""

    def __init__(self, spec: str):
        self.n, self.window = parse_rate(spec)
        self._events: dict[str, list[float]] = {}

    def allow(self, key: str) -> tuple[bool, float]:
        """(allowed, seconds until the next slot frees up). Records the event if allowed."""
        if self.n <= 0:
            return True, 0.0
        now = time.monotonic()
        stamps = [t for t in self._events.get(key, []) if now - t < self.window]
        if len(stamps) >= self.n:
            self._events[key] = stamps
            return False, self.window - (now - stamps[0])
        stamps.append(now)
        self._events[key] = stamps
        return True, 0.0

    def refund(self, key: str) -> None:
        """Undo the most recent allow() for ``key`` (used when a later check refuses the turn)."""
        if self._events.get(key):
            self._events[key].pop()


def _is_dm(channel: str) -> bool:
    """Slack direct-message channel ids start with D (group DMs are G/C and
    behave like channels: a mention is needed there)."""
    return channel.startswith("D")


def _short(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= 40 else text[:37] + "..."


def _chunks(text: str) -> list[str]:
    """Slack truncates long messages, so split on line boundaries instead."""
    if len(text) <= MAX_MESSAGE_CHARS:
        return [text or "(empty reply)"]

    out, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > MAX_MESSAGE_CHARS:
            out.append(line[:MAX_MESSAGE_CHARS])
            line = line[MAX_MESSAGE_CHARS:]
        if len(current) + len(line) > MAX_MESSAGE_CHARS:
            out.append(current)
            current = ""
        current += line
    if current:
        out.append(current)
    return out


_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_UNDERSCORE_BOLD = re.compile(r"__(.+?)__", re.S)
_HEADER = re.compile(r"^#{1,6}\s+(.+)$", re.M)
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BULLET = re.compile(r"^(\s*)[-*]\s+", re.M)


def to_mrkdwn(text: str) -> str:
    """Convert the markdown models emit into Slack's mrkdwn.

    Slack has no headers or tables and uses single asterisks for bold, so an
    unconverted answer renders as literal `**stars**` and `[text](url)` noise.
    Fenced code blocks are passed through untouched.
    """
    parts = text.split("```")
    for i in range(0, len(parts), 2):  # even indices are outside code fences
        chunk = parts[i]
        chunk = _HEADER.sub(r"*\1*", chunk)
        chunk = _BOLD.sub(r"*\1*", chunk)
        chunk = _UNDERSCORE_BOLD.sub(r"*\1*", chunk)
        chunk = _LINK.sub(r"<\2|\1>", chunk)
        chunk = _BULLET.sub(r"\1• ", chunk)
        parts[i] = chunk
    return "```".join(parts)

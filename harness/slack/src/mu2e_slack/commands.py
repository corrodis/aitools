"""In-thread commands (!help, !model, !compact, ...).

Deliberately not Slack's native slash commands: those are registered
workspace-wide (so they collide with every other app) and their payload
carries no thread_ts, which makes "switch the model for *this* conversation"
impossible to express. Parsing a prefix out of the message text the bot was
already going to receive keeps every command scoped to the thread it was
typed in, and needs no extra Slack app configuration.

Commands only touch the backend through the protocols in backend.py; an
operation a backend lacks (NotSupported) gets a one-line "not here" reply.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from .backend import Backend, Conversation, NotSupported

log = logging.getLogger(__name__)


@dataclass
class CommandContext:
    conv: Conversation
    args: list[str]
    cfg: "object"
    backend: Backend


Handler = Callable[[CommandContext], Awaitable[str]]
_COMMANDS: dict[str, tuple[Handler, str, str]] = {}


def command(name: str, usage: str, help_text: str):
    def register(fn: Handler) -> Handler:
        _COMMANDS[name] = (fn, usage, help_text)
        return fn
    return register


def is_command(text: str, prefix: str) -> bool:
    return text.strip().startswith(prefix)


async def dispatch(text: str, prefix: str, ctx_factory) -> str:
    """Parse and run a command. ctx_factory(args) builds the CommandContext."""
    parts = text.strip()[len(prefix):].split()
    if not parts:
        return _help_text(prefix)

    name, args = parts[0].lower(), parts[1:]
    entry = _COMMANDS.get(name)
    ctx = ctx_factory(args)
    if entry is None:
        return f"Unknown command `{prefix}{name}`. Try `{prefix}help`."

    handler, _, _ = entry
    try:
        return await handler(ctx)
    except NotSupported as exc:
        return f"`{prefix}{name}` is not available with this bot{f': {exc}' if str(exc) else '.'}"
    except Exception as exc:
        log.exception("Command %s failed", name)
        return f"`{name}` failed: {exc}"


def _help_text(prefix: str) -> str:
    lines = ["*Commands* (they apply to this thread only):"]
    for name, (_, usage, help_text) in sorted(_COMMANDS.items()):
        lines.append(f"• `{prefix}{usage}` — {help_text}")
    return "\n".join(lines)


@command("help", "help", "this list")
async def _help(ctx: CommandContext) -> str:
    return _help_text(ctx.cfg.command_prefix)


@command("model", "model [name]", "show or switch the model used in this thread")
async def _model(ctx: CommandContext) -> str:
    endpoint = ctx.conv.status().get("endpoint")
    where = f" at {endpoint}" if endpoint else ""
    if not ctx.args:
        return f"This thread is using `{ctx.conv.model}`{where}."

    wanted = ctx.args[0]
    # Check before switching. An unchecked typo here bricks the thread: every
    # later turn -- and !compact with it -- fails with a 404 from the endpoint,
    # and the user has no reason to connect that to the model they set earlier.
    try:
        available = await ctx.backend.list_models()
    except Exception as exc:
        return f"Could not reach the endpoint to check that model exists: {exc}"
    if available and wanted not in available:
        offered = ", ".join(f"`{m}`" for m in available)
        return f"`{wanted}` is not offered{where}. Available: {offered}"

    ctx.conv.set_model(wanted)
    return f"Switched this thread to `{ctx.conv.model}`. History is kept."


@command("models", "models", "list models the endpoint offers")
async def _models(ctx: CommandContext) -> str:
    names = await ctx.backend.list_models()
    if not names:
        return f"This bot uses a fixed model: `{ctx.conv.model}`."
    return "*Available models:*\n" + "\n".join(f"• `{n}`" for n in names)


@command("tools", "tools", "list the MCP tools available to me")
async def _tools(ctx: CommandContext) -> str:
    by_server, failed = ctx.backend.tools_by_server()
    lines = []
    for server in sorted(by_server):
        lines.append(f"*{server}*: " + ", ".join(f"`{t}`" for t in sorted(by_server[server])))
    for server, reason in sorted(failed.items()):
        lines.append(f"*{server}*: unavailable ({reason})")
    return "\n".join(lines) or "No tools are available right now."


@command("status", "status", "model, token use and tool calls in this thread")
async def _status(ctx: CommandContext) -> str:
    s = ctx.conv.status()
    used = s.get("last_prompt_tokens") or 0
    limit = s.get("context_limit") or 0
    pct = f" ({100 * used / limit:.0f}% of budget)" if used and limit else ""
    where = f" at {s['endpoint']}" if s.get("endpoint") else ""
    lines = [
        f"*Model* `{s.get('model', ctx.conv.model)}`{where}",
        f"*Turns* {s.get('turns', 0)} · *LLM calls* {s.get('llm_calls', 0)} · *Tool calls* {s.get('tool_calls', 0)}",
        f"*Tokens* {s.get('input_tokens', 0)} in / {s.get('output_tokens', 0)} out · last request {used}{pct}",
        f"*Tool notifications* {'on' if ctx.conv.tool_notifications else 'off'}",
    ]
    read, write = s.get("cache_read_tokens"), s.get("cache_write_tokens")
    if read or write:
        total = s.get("input_tokens") or 0
        share = f" ({100 * read / total:.0f}% of input)" if total and read else ""
        lines.insert(3, f"*Prompt cache* {read or 0} read{share} / {write or 0} written")
    known = {"model", "endpoint", "turns", "llm_calls", "tool_calls", "input_tokens", "output_tokens",
             "last_prompt_tokens", "context_limit", "tool_notifications",
             "cache_read_tokens", "cache_write_tokens"}
    for k, v in s.items():
        if k not in known and v not in (None, ""):
            lines.append(f"*{k.replace('_', ' ').capitalize()}* {v}")
    for name, url in ctx.conv.links().items():
        lines.append(f"<{url}|{name}>")
    return "\n".join(lines)


@command("links", "links", "links about this thread (e.g. the web transcript)")
async def _links(ctx: CommandContext) -> str:
    links = ctx.conv.links()
    if not links:
        return "No links for this thread."
    return "\n".join(f"• <{url}|{name}>" for name, url in links.items())


@command("compact", "compact", "summarize this thread to free up context")
async def _compact(ctx: CommandContext) -> str:
    summary = await ctx.conv.compact()
    return f"Compacted. Here is what I kept:\n\n{summary}"


@command("reset", "reset", "forget this thread's history and start over")
async def _reset(ctx: CommandContext) -> str:
    ctx.conv.reset()
    return "History cleared. This thread starts fresh."


@command("verbose", "verbose on|off", "show or hide a line for each tool call")
async def _verbose(ctx: CommandContext) -> str:
    if not ctx.args or ctx.args[0].lower() not in ("on", "off"):
        state = "on" if ctx.conv.tool_notifications else "off"
        return f"Tool notifications are {state}. Use `{ctx.cfg.command_prefix}verbose on|off`."
    ctx.conv.tool_notifications = ctx.args[0].lower() == "on"
    return f"Tool notifications {'on' if ctx.conv.tool_notifications else 'off'}."

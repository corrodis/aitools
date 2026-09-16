"""In-thread commands (!help, !model, !compact, ...).

Deliberately not Slack's native slash commands: those are registered
workspace-wide (so they collide with every other app) and their payload
carries no thread_ts, which makes "switch the model for *this* conversation"
impossible to express. Parsing a prefix out of the message text the bot was
already going to receive keeps every command scoped to the thread it was
typed in, and needs no extra Slack app configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


@dataclass
class CommandContext:
    conv: "object"  # agent.Conversation
    args: list[str]
    cfg: "object"
    client: "object"
    tools: "object"


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
    if not ctx.args:
        return f"This thread is using `{ctx.conv.model}` at {ctx.cfg.endpoint}."

    wanted = ctx.args[0]
    # Check before switching. An unchecked typo here bricks the thread: every
    # later turn -- and !compact with it -- fails with a 404 from the endpoint,
    # and the user has no reason to connect that to the model they set earlier.
    try:
        listing = await ctx.client.models.list()
        available = sorted(m.id for m in listing.data)
    except Exception as exc:
        return f"Could not reach {ctx.cfg.endpoint} to check that model exists: {exc}"

    if wanted not in available:
        offered = ", ".join(f"`{m}`" for m in available) or "(none)"
        return f"`{wanted}` is not offered by {ctx.cfg.endpoint}. Available: {offered}"

    ctx.conv.model = wanted
    return f"Switched this thread to `{ctx.conv.model}`. History is kept."


@command("models", "models", "list models the endpoint offers")
async def _models(ctx: CommandContext) -> str:
    listing = await ctx.client.models.list()
    names = sorted(m.id for m in listing.data)
    if not names:
        return "The endpoint reported no models."
    return "*Available models:*\n" + "\n".join(f"• `{n}`" for n in names)


@command("tools", "tools", "list the MCP tools available to me")
async def _tools(ctx: CommandContext) -> str:
    by_server: dict[str, list[str]] = {}
    for tool in ctx.tools.tools.values():
        by_server.setdefault(tool.server, []).append(tool.name)

    lines = []
    for server in sorted(by_server):
        lines.append(f"*{server}*: " + ", ".join(f"`{t}`" for t in sorted(by_server[server])))
    for server, reason in sorted(ctx.tools.failed.items()):
        lines.append(f"*{server}*: unavailable ({reason})")
    return "\n".join(lines) or "No tools are available right now."


@command("status", "status", "model, token use and tool calls in this thread")
async def _status(ctx: CommandContext) -> str:
    u = ctx.conv.usage
    used = ctx.conv.last_prompt_tokens
    pct = f" ({100 * used / ctx.cfg.context_limit:.0f}% of budget)" if used else ""
    return (
        f"*Model* `{ctx.conv.model}` at {ctx.cfg.endpoint}\n"
        f"*Turns* {u.turns} · *LLM calls* {u.llm_calls} · *Tool calls* {u.tool_calls}\n"
        f"*Tokens* {u.input_tokens} in / {u.output_tokens} out · "
        f"last request {used}{pct}\n"
        f"*Tool notifications* {'on' if ctx.conv.tool_notifications else 'off'}"
    )


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

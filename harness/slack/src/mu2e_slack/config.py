"""Runtime configuration.

Everything that is not a secret arrives as a CLI argument (with an env
fallback) so the systemd unit stays a single self-contained ExecStart line,
the same convention the mcp/* services in this repo use.

Secrets arrive only through the environment, never argv: argv is visible to
every user on a shared machine via `ps`, and these tokens are workspace- and
collaboration-wide.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field

from .mcp_tools import parse_stdio_servers

DEFAULT_ENDPOINT = "https://vllm.fnal.gov/v1/"
DEFAULT_MODEL = "gpt-oss:120b"
DEFAULT_REGISTRY = "http://mu2eaigpvm01.fnal.gov:8000/registry"

DEFAULT_SYSTEM_PROMPT = """\
You are the Mu2e AI assistant, reachable from Slack. You are talking to members \
of the Mu2e collaboration at Fermilab.

You have access to tools backed by Mu2e services (DocDB, ECL logbook, run \
database, DQM metrics, metacat datasets, arXiv, INSPIRE-HEP, project memory). \
Use them whenever a question touches real data rather than answering from \
memory, and ground your answer in what they return. If the tools do not \
support the question, say so plainly instead of guessing.

You have no shell, no filesystem and no ability to run code. If someone asks \
you to run something, explain that you can only call the tools listed above.

When a tool comes back empty, that is itself an answer: say what you searched \
for and what you found nothing in, then ask the user to confirm the name or \
narrow it down. Do not retry the same tool with permuted arguments -- guessing \
at wildcard syntax, dropping filters or re-spelling the search term almost \
never helps, and the user is watching each attempt. If two genuinely different \
approaches both come up empty, stop and report that.

Keep answers short -- this is a chat window, not a report. Cite what you used, \
with links where the tool gives you one. Ask for clarification when a question \
is ambiguous rather than answering the wrong version of it.

Slack renders a restricted markdown: *bold*, _italic_, `code`, ```blocks``` and \
<url|link text> work; headers and tables do not. Write accordingly."""


@dataclass
class Config:
    # --- LLM -----------------------------------------------------------------
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    temperature: float = 0.3
    max_tokens: int = 2000
    # Used only to decide when to auto-compact a conversation; the endpoint
    # enforces the real limit.
    context_limit: int = 128000
    # Cap on model<->tool round trips inside a single user turn. A stuck agent
    # in a Slack thread is expensive and noisy, so it gets a hard stop.
    max_tool_iterations: int = 12

    # --- MCP -----------------------------------------------------------------
    registry_url: str = DEFAULT_REGISTRY
    tool_timeout: int = 120
    # Anthropic prompt caching via cache_control markers: auto = for model
    # names containing "claude", on, off.
    prompt_cache: str = "auto"
    # Local MCP servers spawned over stdio, {name: argv}; their tools sit next
    # to the registry's, namespaced the same way (slack__slack_read_channel).
    stdio_servers: dict[str, list[str]] = field(default_factory=dict)

    # --- Slack ---------------------------------------------------------------
    # Channel name ("mu2e-ai") or id ("C0123ABCD"). The bot only ever joins the
    # channels an admin invites it to; this one is its home channel, the only
    # place it follows thread replies that do not mention it.
    channel: str = ""
    # Who may continue a tracked thread without re-mentioning the bot:
    #   asker  -- anyone who has mentioned the bot in that thread (default)
    #   home   -- anyone, but only in the home channel (the original rule)
    #   all    -- anyone, in any tracked thread (noisy in busy threads)
    #   none   -- nobody; every message needs a mention
    thread_followups: str = "asker"
    # Follow-ups without a mention only within this many seconds of the bot's
    # last answer in the thread; 0 = no limit.
    followup_window: int = 1800
    # Where the bot may act at all. Empty allowlist = any channel it has been
    # invited to (mentions only, outside the home channel). The home channel
    # is always allowed. DMs are governed by dm_enabled only.
    allowed_channels: list[str] = field(default_factory=list)
    dm_enabled: bool = True
    command_prefix: str = "!"
    tool_notifications: bool = True
    # Conversations idle longer than this are dropped from memory (their usage
    # record is already on disk).
    idle_timeout: int = 4 * 3600
    # Rate limits on LLM turns (commands like !help are exempt): "N/period"
    # per Slack user and for the whole bot, plus a cap on turns in flight.
    # Over the limit a thread gets one short "try again in …" reply per
    # window; further messages are dropped silently and never queued.
    rate_user: str = "10/10m"
    rate_total: str = "60/10m"
    max_concurrent: int = 3

    # --- Logging -------------------------------------------------------------
    log_output: str = ""
    # Postgres for the shared usage table; empty = jsonl file only.
    pg_dsn: str = ""
    pg_table: str = "usage.sessions"

    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    # --- Secrets (environment only) ------------------------------------------
    slack_bot_token: str = field(default="", repr=False)
    slack_app_token: str = field(default="", repr=False)
    mcp_token: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no")


def _default_log_output() -> str:
    """Next to goose's usage log when the service account has one, so both
    harnesses land in the same tree (and, later, the same Postgres table).
    See usage_log.py for why the schema is shared."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(xdg, "mu2e-slack-bot/log/usage.jsonl")
    return os.path.expanduser("~/.local/share/mu2e-slack-bot/log/usage.jsonl")


def parse_args(argv: list[str] | None = None) -> tuple[Config, argparse.Namespace]:
    p = argparse.ArgumentParser(
        prog="mu2e-slack-bot",
        description="Slack frontend for a Mu2e AI agent (MCP tools only, no system access).",
    )
    p.add_argument("--endpoint", default=os.environ.get("MU2E_SLACK_ENDPOINT", DEFAULT_ENDPOINT),
                   help="OpenAI-compatible base URL (default: %(default)s)")
    p.add_argument("--model", default=os.environ.get("MU2E_SLACK_MODEL", DEFAULT_MODEL),
                   help="Model to request (default: %(default)s)")
    p.add_argument("--temperature", type=float, default=float(os.environ.get("MU2E_SLACK_TEMPERATURE", 0.3)))
    p.add_argument("--max-tokens", type=int, default=int(os.environ.get("MU2E_SLACK_MAX_TOKENS", 2000)))
    p.add_argument("--context-limit", type=int, default=int(os.environ.get("MU2E_SLACK_CONTEXT_LIMIT", 128000)),
                   help="Token budget before a thread is auto-compacted (default: %(default)s)")
    p.add_argument("--max-tool-iterations", type=int,
                   default=int(os.environ.get("MU2E_SLACK_MAX_TOOL_ITERATIONS", 12)))

    p.add_argument("--registry", default=os.environ.get("MU2E_SLACK_REGISTRY", DEFAULT_REGISTRY),
                   help="MCP registry URL to load tools from (default: %(default)s)")
    p.add_argument("--tool-timeout", type=int, default=int(os.environ.get("MU2E_SLACK_TOOL_TIMEOUT", 120)))
    p.add_argument("--prompt-cache", choices=("auto", "on", "off"),
                   default=os.environ.get("MU2E_SLACK_PROMPT_CACHE", "auto"),
                   help="Mark tools, system prompt and conversation for Anthropic prompt caching: "
                        "auto = only for Claude models (default), on, off.")
    p.add_argument("--stdio-server", action="append", metavar="NAME=COMMAND",
                   help="Also spawn a local MCP server over stdio, e.g. slack=slack-mcp-stdio. "
                        "Repeatable (env MU2E_SLACK_STDIO_SERVERS, ;-separated). The child sees "
                        "only the bot's NAME_* environment variables (SLACK_* for slack) on top "
                        "of HOME/PATH/USER.")

    p.add_argument("--channel", default=os.environ.get("MU2E_SLACK_CHANNEL", ""),
                   help="Home channel name or id. The bot answers mentions anywhere it has "
                        "been invited; this is the one channel where it also follows replies "
                        "in threads it is already part of.")
    p.add_argument("--channels", default=os.environ.get("MU2E_SLACK_CHANNELS", ""),
                   help="Comma-separated channel names or ids the bot may act in (mentions elsewhere "
                        "are dropped). Empty = any channel it has been invited to. The home channel "
                        "is always included.")
    p.add_argument("--no-dm", action="store_true",
                   help="Ignore direct messages entirely (default: DMs are answered when the app "
                        "has the im:history scope and the message.im event).")
    p.add_argument("--thread-followups", choices=("asker", "home", "all", "none"),
                   default=os.environ.get("MU2E_SLACK_THREAD_FOLLOWUPS", "asker"),
                   help="Who may continue a tracked thread without re-mentioning the bot: asker = "
                        "whoever has mentioned it in that thread (default); home = anyone, home "
                        "channel only; all = anyone, any thread; none = a mention every time.")
    p.add_argument("--no-thread-followups", action="store_true",
                   help="Same as --thread-followups none: the app never acts on a message it "
                        "was not tagged in (DMs excepted).")
    p.add_argument("--followup-window", type=int, default=int(os.environ.get("MU2E_SLACK_FOLLOWUP_WINDOW", 1800)),
                   help="Seconds after the bot's last answer during which thread follow-ups "
                        "without a mention are accepted; 0 = no limit (default: %(default)s)")
    p.add_argument("--command-prefix", default=os.environ.get("MU2E_SLACK_COMMAND_PREFIX", "!"),
                   help="Prefix for in-thread commands like !help (default: %(default)s)")
    p.add_argument("--no-tool-notifications", action="store_true",
                   help="Do not post a line in the thread when the agent calls a tool.")
    p.add_argument("--idle-timeout", type=int, default=int(os.environ.get("MU2E_SLACK_IDLE_TIMEOUT", 4 * 3600)),
                   help="Seconds before an inactive thread's conversation is forgotten "
                        "(default: %(default)s)")

    p.add_argument("--rate-user", default=os.environ.get("MU2E_SLACK_RATE_USER", "10/10m"),
                   help="Max LLM turns per Slack user, as N/period e.g. 10/10m, 100/1h (default: %(default)s)")
    p.add_argument("--rate-total", default=os.environ.get("MU2E_SLACK_RATE_TOTAL", "60/10m"),
                   help="Max LLM turns for the whole bot, N/period (default: %(default)s)")
    p.add_argument("--max-concurrent", type=int, default=int(os.environ.get("MU2E_SLACK_MAX_CONCURRENT", 3)),
                   help="Max turns in flight at once; others wait (default: %(default)s)")

    p.add_argument("--log-output", default=os.environ.get("LOG_OUTPUT", _default_log_output()),
                   help="Usage jsonl path (default: %(default)s)")
    p.add_argument("--system-prompt-file", default=os.environ.get("MU2E_SLACK_SYSTEM_PROMPT_FILE", ""),
                   help="Read the system prompt from this file instead of the built-in one.")

    p.add_argument("--check", action="store_true",
                   help="Verify Slack auth, the LLM endpoint and the MCP registry, print what "
                        "was found, and exit without connecting to Slack.")
    p.add_argument("--version", action="store_true", help="Print version and exit.")

    args = p.parse_args(argv)

    system_prompt = DEFAULT_SYSTEM_PROMPT
    if args.system_prompt_file:
        with open(args.system_prompt_file) as fh:
            system_prompt = fh.read().strip()

    cfg = Config(
        endpoint=args.endpoint,
        model=args.model,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        context_limit=args.context_limit,
        max_tool_iterations=args.max_tool_iterations,
        registry_url=args.registry,
        tool_timeout=args.tool_timeout,
        prompt_cache=args.prompt_cache,
        stdio_servers=parse_stdio_servers(
            args.stdio_server if args.stdio_server is not None
            else [s for s in os.environ.get("MU2E_SLACK_STDIO_SERVERS", "").split(";") if s.strip()]),
        channel=args.channel,
        thread_followups="none" if args.no_thread_followups else args.thread_followups,
        followup_window=max(0, args.followup_window),
        allowed_channels=[c.strip().lstrip("#") for c in args.channels.split(",") if c.strip()],
        dm_enabled=not args.no_dm and _env_flag("MU2E_SLACK_DM", True),
        command_prefix=args.command_prefix,
        tool_notifications=not args.no_tool_notifications,
        idle_timeout=args.idle_timeout,
        rate_user=args.rate_user,
        rate_total=args.rate_total,
        max_concurrent=max(1, args.max_concurrent),
        log_output=args.log_output,
        pg_dsn=os.environ.get("LOG_PG_DSN", ""),
        pg_table=os.environ.get("LOG_PG_TABLE", "usage.sessions"),
        system_prompt=system_prompt,
        slack_bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
        slack_app_token=os.environ.get("SLACK_APP_TOKEN", ""),
        mcp_token=os.environ.get("MIKEY_TOKEN", ""),
        api_key=os.environ.get("OPENAI_API_KEY", ""),
    )
    return cfg, args

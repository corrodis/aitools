"""Entry point: wire config, MCP tools and Slack together, then wait."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import httpx2
from openai import AsyncOpenAI

from . import version
from .config import parse_args
from .mcp_tools import ToolRegistry
from .slackbot import SlackBot

log = logging.getLogger("mu2e_slack")

CLEANUP_INTERVAL = 600


def _llm(cfg) -> AsyncOpenAI:
    # Keep-alive is switched off deliberately, and it is not a micro-optimisation
    # knob -- without it the agent cannot complete a single tool-using turn.
    # Measured against vllm.fnal.gov: an LLM call works, an MCP tool call works,
    # and the next LLM call on the SAME client dies with
    # ssl.SSLError("[SSL] passed invalid argument") -- while the same call on a
    # fresh client succeeds, and the whole sequence succeeds with no MCP call in
    # between. An MCP session's teardown invalidates this client's idle pooled
    # TLS connection (mcp and openai share one anyio/httpcore2 runtime, and the
    # transport's task-group cancellation reaches a connection it does not own).
    # No idle connection, nothing to poison. Costs one TLS handshake per request,
    # which is free at chat pace.
    http_client = httpx2.AsyncClient(
        timeout=httpx2.Timeout(600.0, connect=30.0),
        limits=httpx2.Limits(max_keepalive_connections=0),
    )
    # vllm.fnal.gov takes no key today, but the OpenAI client insists on one.
    return AsyncOpenAI(base_url=cfg.endpoint, api_key=cfg.api_key or "EMPTY",
                       timeout=600, http_client=http_client)


async def _check(cfg) -> int:
    """Verify every dependency and print what was found, without joining Slack."""
    ok = True
    print(f"mu2e-slack-bot {version()}")

    print(f"\nLLM endpoint: {cfg.endpoint}")
    try:
        models = await _llm(cfg).models.list()
        names = sorted(m.id for m in models.data)
        print(f"  reachable, {len(names)} model(s); configured: {cfg.model}"
              f"{'' if cfg.model in names else '  <-- NOT offered by this endpoint'}")
    except Exception as exc:
        ok = False
        print(f"  FAILED: {exc}")

    print(f"\nMCP registry: {cfg.registry_url}")
    tools = ToolRegistry(cfg.registry_url, cfg.mcp_token, cfg.tool_timeout)
    try:
        await tools.load()
        print(f"  {len(tools.tools)} tool(s) from {len(tools.servers) - len(tools.failed)}"
              f"/{len(tools.servers)} server(s)")
        for name, reason in sorted(tools.failed.items()):
            print(f"  unavailable: {name} ({reason})")
        if not cfg.mcp_token:
            print("  note: MIKEY_TOKEN unset -- token-gated servers (ecl, runs, memory) will refuse calls")
    except Exception as exc:
        ok = False
        print(f"  FAILED: {exc}")

    print("\nSlack:")
    if not cfg.slack_bot_token or not cfg.slack_app_token:
        ok = False
        print("  FAILED: SLACK_BOT_TOKEN and SLACK_APP_TOKEN must both be set")
    else:
        bot = SlackBot(cfg, _llm(cfg), tools)
        try:
            await bot.connect()
            print(f"  authenticated as {bot.bot_user_id}")
            print(f"  home channel: {cfg.channel or '(none -- mention-only everywhere)'}"
                  f"{f' -> {bot.home_channel_id}' if bot.home_channel_id else ''}")
            print(f"  thread follow-ups: {'on' if cfg.thread_followups else 'off (mention required every time)'}")
        except Exception as exc:
            ok = False
            print(f"  FAILED: {exc}")

    print(f"\nUsage log: {cfg.log_output}{'  (privacy mode)' if cfg.privacy else ''}")
    return 0 if ok else 1


async def _run(cfg) -> int:
    if not cfg.slack_bot_token or not cfg.slack_app_token:
        log.error("SLACK_BOT_TOKEN and SLACK_APP_TOKEN must both be set")
        return 2

    tools = ToolRegistry(cfg.registry_url, cfg.mcp_token, cfg.tool_timeout)
    await tools.load()

    bot = SlackBot(cfg, _llm(cfg), tools)
    await bot.connect()
    await bot.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def housekeeping():
        while not stop.is_set():
            await asyncio.sleep(CLEANUP_INTERVAL)
            bot.cleanup()

    keeper = asyncio.create_task(housekeeping())
    log.info("Ready. Model %s, %d tools.", cfg.model, len(tools.tools))

    await stop.wait()
    log.info("Shutting down")
    keeper.cancel()
    await bot.close()
    return 0


def main(argv: list[str] | None = None) -> None:
    cfg, args = parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    if args.version:
        print(version())
        return
    if args.check:
        sys.exit(asyncio.run(_check(cfg)))
    sys.exit(asyncio.run(_run(cfg)))


if __name__ == "__main__":
    main()

"""Entry point: wire config, a backend and Slack together, then wait.

``run`` and ``check`` take any :class:`~mu2e_slack.backend.Backend`, so a
package that fronts a different agent (daqpy's DAQ bot) reuses this module
with its own backend and gets the same Slack behaviour; ``main`` builds the
default :class:`~mu2e_slack.registry_backend.RegistryBackend`.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from . import usage_log, version
from .backend import Backend
from .config import parse_args
from .slackbot import SlackBot

log = logging.getLogger("mu2e_slack")

CLEANUP_INTERVAL = 600


async def check(cfg, backend: Backend, name: str = "mu2e-slack-bot") -> int:
    """Verify every dependency and print what was found, without joining Slack."""
    print(f"{name} {version()}  (backend: {backend.name})")

    ok, lines = await backend.check()
    print()
    print("\n".join(lines))

    print("\nSlack:")
    if not cfg.slack_bot_token or not cfg.slack_app_token:
        ok = False
        print("  FAILED: SLACK_BOT_TOKEN and SLACK_APP_TOKEN must both be set")
    else:
        bot = SlackBot(cfg, backend)
        try:
            await bot.connect()
            print(f"  authenticated as {bot.bot_user_id}")
            print(f"  home channel: {cfg.channel or '(none -- mention-only everywhere)'}"
                  f"{f' -> {bot.home_channel_id}' if bot.home_channel_id else ''}")
            mode = cfg.thread_followups
            window = getattr(cfg, "followup_window", 0)
            print(f"  thread follow-ups: {mode}"
                  f"{f' (within {window // 60} min of the last answer)' if mode not in ('none', False) and window else ''}"
                  f"{'; DMs off' if not getattr(cfg, 'dm_enabled', True) else ''}"
                  f"{'; channels: ' + ', '.join(cfg.allowed_channels) if getattr(cfg, 'allowed_channels', None) else ''}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  FAILED: {exc}")
        finally:
            await bot.close()  # never connected to Socket Mode, but the HTTP session is open

    if getattr(backend, "adapter_usage_log", True):
        if cfg.pg_dsn:
            print(f"\nUsage table: {cfg.pg_table} at {cfg.pg_dsn}")
            try:
                print(f"  {await asyncio.to_thread(usage_log.check_postgres, cfg.pg_dsn, cfg.pg_table)}")
            except Exception as exc:  # noqa: BLE001
                # Not fatal: rows queue in the backlog until the table is reachable.
                print(f"  NOT WRITABLE (rows will queue in the backlog): {exc}")
            backlog = usage_log.backlog_path(cfg)
            if backlog.exists():
                print(f"  backlog: {len(usage_log._read_backlog(backlog))} row(s) waiting in {backlog}")
        else:
            print(f"\nUsage log: {cfg.log_output}")
    else:
        print(f"\nUsage log: written by the {backend.name} backend itself")
    return 0 if ok else 1


async def run(cfg, backend: Backend) -> int:
    if not cfg.slack_bot_token or not cfg.slack_app_token:
        log.error("SLACK_BOT_TOKEN and SLACK_APP_TOKEN must both be set")
        return 2

    await backend.open()

    bot = SlackBot(cfg, backend)
    await bot.connect()
    await bot.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def housekeeping():
        while not stop.is_set():
            await asyncio.sleep(CLEANUP_INTERVAL)
            await bot.cleanup()

    keeper = asyncio.create_task(housekeeping())
    log.info("Ready (backend %s).", backend.name)

    await stop.wait()
    log.info("Shutting down")
    keeper.cancel()
    await bot.close_all()
    await bot.close()
    await backend.close()
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

    from .registry_backend import RegistryBackend  # noqa: PLC0415  (pulls in httpx2/aiohttp)
    backend = RegistryBackend(cfg)
    if args.check:
        sys.exit(asyncio.run(check(cfg, backend)))
    sys.exit(asyncio.run(run(cfg, backend)))


if __name__ == "__main__":
    main()

# slack-mcp

Read-only MCP server for an **allowlisted** set of Mu2e Slack channels
(e.g. `#mu2e-shift`). It gives agents the human side of operations — what
shifters and experts reported, decided and asked, and when — next to the
machine side served by the DAQ, run-log and DCS servers.

Same packaging and deployment as the other `mcp/*` servers: `pyproject.toml`
installed with `uv` from the git subdirectory, versioned release dirs, a
`systemd --user` unit, optional mikey bearer-token auth over streamable-HTTP.
Additionally it has a stdio entry point so a local agent (daqpy) can spawn it
as a subprocess, like it does with `ecl-mcp-stdio` and `dcs-mcp-stdio`.

## Scope

- **Three read tools, nothing else.** No posting, no reactions, no workspace
  search, no user directory.
- **Allowlist is the universe.** `SLACK_MCP_CHANNELS` names the channels that
  can be listed or read. A channel not in it cannot be reached by name or by
  id — callers are told which channels exist here, not that one was denied.
- **Bounded output.** At most `SLACK_MCP_MAX_MESSAGES` (500) messages per
  call; defaults are the last 24 h and 100 messages.

## Exposed tools

| Tool | Returns |
|---|---|
| `slack_server_info()` | version, allowlist, limits, auth mode (prefixed so clients merging several servers' tools, like daqpy, see no duplicate name) |
| `slack_list_channels(refresh=False)` | the allowlisted channels: id, name, private?, topic, purpose, members |
| `slack_read_channel(channel, since="24h", until=None, limit=100, include_threads=False)` | messages oldest-first: ISO time, author name, text with `@mentions`/links resolved, permalink, reply_count/thread_ts, files, reactions; optionally each thread's replies nested |
| `slack_read_thread(channel, thread_ts, limit=200)` | the parent and all replies of one thread |

Times accept relative (`24h`, `7d`, `30m`), ISO 8601, or unix epoch.

## Slack app

A bot token (`xoxb-…`) with scopes `channels:history`, `channels:read`,
`users:read`; for private channels also `groups:history`, `groups:read`.
Invite the bot to every allowlisted channel (`/invite @<bot>`) — Slack lists
public channels the bot is not in, but refuses their history
(`not_in_channel`). The DAQ bot (`daqbot`) token can be reused; this
server never posts with it. Scopes by use case for all our Slack tools:
`harness/slack/SLACK_APP.md`.

## Environment variables

| Variable | Purpose |
|---|---|
| `SLACK_BOT_TOKEN` | bot token (required) |
| `SLACK_MCP_CHANNELS` | comma-separated channel names (no `#`) or ids — the allowlist (required) |
| `SLACK_MCP_MAX_MESSAGES` | per-call cap, default 500 |
| `SLACK_MCP_HOST` / `SLACK_MCP_PORT` | bind for the HTTP transport (default 0.0.0.0:8009) |
| `SLACK_MCP_LOG_LEVEL` | default INFO (WARNING for stdio) |
| `MIKEY_KEYS_FILE` | enables mikey auth on HTTP; unset = disabled |

## `--check`

`slack-mcp --check` calls `auth.test` and resolves the allowlist (names via
`conversations.list`, ids via `conversations.info`), prints the result and
exits non-zero if a channel cannot be resolved. It reads no messages.

## Local dev

```bash
uv venv && uv pip install -e '.[dev]'
set -a; . ./config/slack-mcp.env.example; set +a   # or your real env file
slack-mcp --check
slack-mcp --transport stdio                        # for an MCP client over stdio
python -m pytest tests                              # no network
```

## Server-account install

On mu2eaigpvm01, as `mu2eai`, next to the other servers (uv comes from
`mu2einit && slc uv` there):

```bash
deploy=/exp/mu2e/app/users/mu2eai/mcp/slack
./scripts/install.sh $deploy <ref>
cp $deploy/current/.venv/share/slack-mcp/slack-mcp.env.example $deploy/slack-mcp.env
$EDITOR $deploy/slack-mcp.env && chmod 600 $deploy/slack-mcp.env
#   SLACK_BOT_TOKEN     -- the askmu2e bot token can be reused (read scopes only are used)
#   SLACK_MCP_CHANNELS  -- the allowlist; over HTTP it is readable by every mikey holder
#   MIKEY_KEYS_FILE=/exp/mu2e/app/users/mu2eai/mcp/mikey/keys
set -a; . $deploy/slack-mcp.env; set +a
$deploy/current/.venv/bin/slack-mcp.sh --check
$deploy/current/.venv/bin/slack-mcp-install-unit.sh --port 8009 --env-file $deploy/slack-mcp.env
$deploy/current/.venv/bin/python scripts/smoke_test_http.py http://127.0.0.1:8009 <mikey-token>
```

Registry entry: port 8009, token `yes` (`../registry/config/ports.json`). It
only shows up once the registry itself is redeployed from a ref that has it
-- deploy this server first, so the registry never lists a dead port.

Once it is in the registry, the Slack bot can drop its local
`--stdio-server slack=...`: the registry's `slack` server then provides the
same tools (a stdio server of the same name would take precedence).

## Client config

HTTP (Claude Code, Goose, the Slack bot via the registry):

```bash
claude mcp add --transport http --scope user slack http://<host>:8009/mcp \
  --header "Authorization: Bearer mikey_<token>"
```

stdio (daqpy): install the package into daqpy's venv; daqpy auto-detects the
sibling `slack-mcp-stdio` and reads `SLACK_BOT_TOKEN` / `SLACK_MCP_CHANNELS`
from its own environment.

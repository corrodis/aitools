# mu2e-slack-bot (prototype)

A Slack frontend for a Mu2e AI agent. One Slack thread is one conversation.
The agent talks to an OpenAI-compatible endpoint (`vllm.fnal.gov` + `gpt-oss`
by default) and can call the MCP servers in the internal registry — and
nothing else.

Runs as a service, installed and deployed exactly like the `mcp/*` servers in
this repo: a `pyproject.toml` package installed with plain `uv` primitives
into `<deploy-root>/releases/<ref>/.venv`, a `current` symlink, and a
`systemd --user` unit. It is not an MCP server, which is why it lives under
`harness/` next to `goose/` rather than under `mcp/`.

## Scope

- **No system access.** There is no shell, filesystem or code-execution tool
  anywhere in this package. That is a property of what exists here, not a
  setting — see `mcp_tools.py`.
- **Reads only what it is tagged in.** The bot acts on a message that
  @-mentions it (in any channel it has been invited to), on follow-ups in a
  thread it already holds a conversation for (see below), and on direct
  messages. Anything else is dropped at the adapter before it is logged or
  sent to the model.
- **Thread follow-ups.** By default (`--thread-followups asker`) only people
  who have mentioned the bot in that thread may continue it without
  re-mentioning, and only within `--followup-window` (30 min) of its last
  answer; everyone else must mention it — so a busy human thread never turns
  every reply into a model call. `home` restores the original rule (anyone,
  home channel only), `all` allows anyone in any tracked thread, `none`
  requires a mention every time.
- **Direct messages.** In a DM every message is for the bot: a top-level
  message starts a thread, which is the conversation; replies in that
  thread continue it. Needs the `im:history` scope and the `message.im`
  event subscription (plus `im:read`/`im:write` if the app should be able
  to open DMs itself). `--no-dm` (or `MU2E_SLACK_DM=0`) ignores DMs entirely.
- **Channel allowlist.** `--channels a,b` (or `MU2E_SLACK_CHANNELS`) limits
  the bot to those channels (plus the home channel): mentions anywhere else
  are dropped. Empty (default) = any channel it has been invited to. DMs are
  governed by `--no-dm` only.
- **One thread, one conversation.** Thread state lives in memory and is
  dropped after `--idle-timeout`. The durable artifact is the usage record,
  not the transcript.

The follow-up rule is what makes a thread read as a conversation instead of
a sequence of @-prefixed commands. The cost is that Slack delivers every
message in subscribed channels to this process (the Events API has no
per-thread subscription); the filter runs before anything is logged.
`--no-thread-followups` (= `none`) is the strictest setting.

## Commands

Typed in a thread, they apply to that thread only:

| Command | Effect |
|---|---|
| `!help` | list commands |
| `!model [name]` | show or switch this thread's model (history is kept) |
| `!models` | list what the endpoint offers |
| `!tools` | list the MCP tools currently available |
| `!status` | model, token use, tool calls in this thread |
| `!compact` | summarize the thread to free up context |
| `!reset` | forget this thread's history |
| `!verbose on\|off` | show or hide a line per tool call |
| `!links` | links about this thread, when the backend has any (e.g. a web transcript) |

Not Slack's native slash commands: those are registered workspace-wide and
carry no `thread_ts`, so they cannot target *this* conversation. Change the
prefix with `--command-prefix`.

Compaction also happens automatically once a request reaches 80% of
`--context-limit`.

## Rate limits

Three brakes keep a tool-calling bot from running away on a burst of
messages, a mention storm or a misbehaving client:

| Flag / env | Default | Meaning |
|---|---|---|
| `--rate-user` / `MU2E_SLACK_RATE_USER` | `10/10m` | LLM turns per Slack user per window |
| `--rate-total` / `MU2E_SLACK_RATE_TOTAL` | `60/10m` | LLM turns for the whole bot per window |
| `--max-concurrent` / `MU2E_SLACK_MAX_CONCURRENT` | `3` | turns in flight at once; others wait their turn |

`N/period` with `s`, `m`, `h`, `d`; `0/…` or empty disables a limit. `!`
commands are exempt (no model call). Over a limit, the thread gets one
"_I'm rate-limited right now — please try again in about N min_" reply per
window and further messages are dropped — never queued. The per-turn cost is
bounded separately by the backend (`--max-tool-iterations` here, the tool
budget in daqpy).

## Slack app setup

See [SLACK_APP.md](SLACK_APP.md) for scopes and events by use case (mentions,
thread follow-ups, private channels, DMs, reading channels for `slack-mcp`,
posting reports). The short version for this bot:

Create an app at <https://api.slack.com/apps>, enable **Socket Mode**, and
generate an app-level token with `connections:write`. Bot token scopes:

```
app_mentions:read   chat:write        users:read
channels:history    channels:read     reactions:write
groups:history      groups:read       # only if the home channel is private
```

Event subscriptions: `app_mention`, `message.channels` (plus
`message.groups` for a private home channel), and `message.im` with the
`im:history` scope for direct messages. Subscribe to `message.*` only
if you want thread follow-ups without a mention; with
`--no-thread-followups`, `app_mention` alone is enough (DMs still work).

Then invite the bot to its channel: `/invite @mu2e-ai`.

## Install

```bash
./scripts/install.sh /exp/mu2e/app/home/mu2eai/slack/deploy v0.1.0
```

Then, as the service account:

```bash
deploy=/exp/mu2e/app/home/mu2eai/slack/deploy
cp $deploy/current/.venv/share/mu2e-slack-bot/mu2e-slack-bot.env.example $deploy/mu2e-slack-bot.env
$EDITOR $deploy/mu2e-slack-bot.env && chmod 600 $deploy/mu2e-slack-bot.env

# verify everything before joining Slack
set -a; . $deploy/mu2e-slack-bot.env; set +a
$deploy/current/.venv/bin/mu2e-slack-bot.sh --check --channel mu2e-ai

# register and start the unit
$deploy/current/.venv/bin/mu2e-slack-bot-install-unit.sh \
    --env-file $deploy/mu2e-slack-bot.env -- --channel mu2e-ai
```

`--check` verifies the LLM endpoint, the MCP registry (listing what each
server offers and what is down) and Slack auth, then exits without
connecting.

Secrets live only in the env file, never in argv — argv is visible to every
user on a shared machine via `ps`, and the Slack and MIKEY tokens are
workspace- and collaboration-wide. The env file sits at the deploy root,
outside any one release, so upgrades and rollbacks leave it alone.

## Configuration

All non-secret settings are CLI flags (each with an env fallback), so the
unit's `ExecStart` is self-contained. The ones worth knowing:

| Flag | Default | Purpose |
|---|---|---|
| `--endpoint` | `https://vllm.fnal.gov/v1/` | OpenAI-compatible base URL |
| `--model` | `gpt-oss:120b` | default model (per-thread override with `!model`) |
| `--channel` | — | home channel name or id |
| `--registry` | `http://mu2eaigpvm01.fnal.gov:8000/registry` | where tools come from |
| `--stdio-server` | — | `NAME=COMMAND`, repeatable: also spawn a local stdio MCP server (see below) |
| `--tool-timeout` | 120 | seconds before a single tool call is abandoned |
| `--thread-followups` | `asker` | who may continue a thread without a mention: `asker`, `home`, `all`, `none` |
| `--followup-window` | 1800 | seconds after the bot's last answer during which follow-ups count (0 = no limit) |
| `--no-thread-followups` | off | same as `--thread-followups none` |
| `--channels` | — | allowlist of channels the bot may act in (home channel always included) |
| `--no-dm` | off | ignore direct messages |
| `--context-limit` | 128000 | token budget before auto-compaction |
| `--max-tool-iterations` | 12 | hard stop on model↔tool round trips per turn |
| `--idle-timeout` | 14400 | seconds before an inactive thread is forgotten |
| `--system-prompt-file` | — | override the built-in system prompt |

## Local stdio servers

Tools can also come from MCP servers the bot spawns itself over stdio, next
to the registry's: `--stdio-server slack=slack-mcp-stdio` (or
`MU2E_SLACK_STDIO_SERVERS="slack=slack-mcp-stdio;other=..."`). Their tools are
namespaced like the registry's (`slack__slack_read_channel`), and the process
is spawned per call, exactly as the HTTP servers are connected per call.

- A bare command is looked up in the bot's own venv first, so install the
  server into it: `scripts/install.sh --with-slack-mcp <deploy-root> <ref>`
  does that for `slack-mcp`, from the same ref.
- The child sees mcp's safe defaults (HOME, PATH, USER, ...) plus only the
  bot's variables prefixed with the server name: `SLACK_*` for `slack`. Put
  its settings in the bot's env file, e.g. `SLACK_MCP_CHANNELS=mu2e-ai-test`;
  `MIKEY_TOKEN` and `OPENAI_API_KEY` never reach it.
- For `slack-mcp` the bot's own `SLACK_BOT_TOKEN` is reused (it needs
  `channels:history`, `channels:read`, `users:read`, which this bot has), and
  the bot must be in every allowlisted channel.

## Usage log

After every turn, the thread's running totals, in the shape of the shared
table `usage.sessions` (`../usage/sessions.sql`) that goose and claude-code
feed too; `interface` is `slack` here. Counts, timings, model, provider and
tool names only -- no Slack user, channel, thread or content. `session_id` is
a hash of channel and thread, so one thread stays one row.

- `LOG_OUTPUT` -- jsonl file, always written (default
  `~/.local/share/mu2e-slack-bot/log/usage.jsonl`).
- `LOG_PG_DSN` -- also append a row to Postgres (insert-only, one row per
  turn; query the view `usage.sessions_current`, the latest row per session), e.g.
  `postgresql://ifdb11:5477/mu2e_ai_prd` (URI form: no spaces, so the env
  file reads the same in systemd and in `set -a; . file`), authenticated by Kerberos
  (`KRB5CCNAME` pointing at the service account's auto-renewed ticket).
  `LOG_PG_TABLE` overrides the table. A database failure is logged and the
  file still has the record.

`--check` says whether the table exists and is writable for this account.

## Backends: the same Slack behaviour in front of a different agent

Everything Slack-specific (thread rules, de-duplication, the per-tool status
message, mrkdwn conversion, chunking, `!` commands, idle cleanup, the usage
log) lives in the adapter and only talks to a backend through the protocols
in `backend.py`:

- `Backend` — `open/close`, `new_conversation(key, context)`,
  `close_conversation`, `list_models`, `tools_by_server`, `check`
- `Conversation` — `ask(text, on_tool)`, `status()`, `links()`, `reset()`,
  `compact()`, `set_model()`, `usage_snapshot()`

The default is `registry_backend.RegistryBackend` (the chat loop in
`agent.py` over the MCP registry's tools). Another package supplies its own
backend and reuses `cli.run(cfg, backend)` / `cli.check(cfg, backend)` — daqpy
does this for the DAQ-side bot, whose conversations are persistent daqpy chat
sessions. A backend that lacks an operation raises `NotSupported` and the
command replies "not available with this bot"; one that keeps its own usage
log returns `None` from `usage_snapshot()` and the adapter writes nothing.

Tests (`tests/`) exercise the adapter with a fake backend and need no
network: `python -m pytest harness/slack/tests`.

## Files

| Path | Purpose |
|---|---|
| `src/mu2e_slack/config.py` | CLI/env configuration |
| `src/mu2e_slack/backend.py` | the `Backend` / `Conversation` protocols |
| `src/mu2e_slack/registry_backend.py` | default backend: LLM client + MCP registry |
| `src/mu2e_slack/mcp_tools.py` | MCP registry discovery and tool dispatch |
| `src/mu2e_slack/agent.py` | the chat loop, one `Conversation` per thread |
| `src/mu2e_slack/commands.py` | in-thread `!` commands |
| `src/mu2e_slack/slackbot.py` | Socket Mode adapter, event filtering, mrkdwn |
| `src/mu2e_slack/usage_log.py` | per-thread usage records |
| `scripts/install.sh` | uv install into a versioned release dir |
| `scripts/mu2e-slack-bot-install-unit.sh` | render + link the systemd unit |
| `config/mu2e-slack-bot.env.example` | secrets template |

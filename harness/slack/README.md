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
  @-mentions it (in any channel it has been invited to), and on replies in a
  thread it is already part of, in its home channel only. Anything else is
  dropped at the adapter before it is logged or sent to the model.
- **One thread, one conversation.** Thread state lives in memory and is
  dropped after `--idle-timeout`. The durable artifact is the usage record,
  not the transcript.

The thread follow-up rule is what makes a thread read as a conversation
instead of a sequence of @-prefixed commands. The cost is that Slack delivers
every message in the home channel to this process (the Events API has no
per-thread subscription). `--no-thread-followups` gives up the convenience
and requires a mention on every message, which is the strictest possible
setting.

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

## Slack app setup

Create an app at <https://api.slack.com/apps>, enable **Socket Mode**, and
generate an app-level token with `connections:write`. Bot token scopes:

```
app_mentions:read   chat:write        users:read
channels:history    channels:read     reactions:write
groups:history      groups:read       # only if the home channel is private
```

Event subscriptions: `app_mention`, `message.channels` (plus
`message.groups` for a private home channel). Subscribe to `message.*` only
if you want thread follow-ups without a mention; with
`--no-thread-followups`, `app_mention` alone is enough.

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
| `--no-thread-followups` | off | require an @mention on every message |
| `--context-limit` | 128000 | token budget before auto-compaction |
| `--max-tool-iterations` | 12 | hard stop on model↔tool round trips per turn |
| `--idle-timeout` | 14400 | seconds before an inactive thread is forgotten |
| `--system-prompt-file` | — | override the built-in system prompt |

## Usage log

One line per thread, upserted after every turn, in the same schema and with
the same env knobs (`LOG_OUTPUT`, `LOG_PRIVACY`, and later `LOG_PG_DSN`) as
`../goose/log-session.py` — so both harnesses read as one dataset and the
Postgres backend only has to be written once. Counts, timings, model and tool
names only; no conversation content. `LOG_PRIVACY=1` drops the Slack user,
channel and thread, and hashes the session id.

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

# goose harness

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](../../LICENSE)

AI assistant for the [Mu2e experiment](https://mu2e.fnal.gov), built on top of
[goose](https://github.com/block/goose). This harness handles binary installation,
XDG relocation (shared home-dir quotas are small), MCP server sync, Kerberos
scrubbing, guardrails, and optional local tracing.

## Clone

```bash
git clone https://github.com/Mu2e/aitools ~/aitools
cd ~/aitools/harness/goose
```

## Install

```bash
. setup.sh           # source to also put goose on PATH in this shell
```

`setup.sh` will:

1. Redirect XDG dirs to group storage (`/exp/mu2e/app/users/$USER/goose/`)
2. Seed `config.yaml` from `config.yaml.example` if none exists yet
3. Download the goose binary (skips if already present)
4. Sync MCP servers from the internal registry

For a pinned version: `. setup.sh 1.0.25`

> **Every new shell** needs `. env.sh` (or `. setup.sh`) to put goose on PATH.
> Add it to your `.bashrc` to make this automatic.

## Use

```bash
mu2eai               # start an interactive session
```

### Switch models

```bash
mu2eai-model         # interactive fuzzy picker (requires fzf)
```

Backends: internal `vllm.fnal.gov` (default), ALCF sophia/metis, `litellm.fnal.gov`.

### Sync MCP servers

MCP servers are synced automatically on install. Re-run only if the registry has changed:

```bash
./sync-mcp.sh
```

### Session usage log

Token counts, model, and tool usage are automatically logged after every turn to
`/exp/mu2e/app/users/$USER/goose/log/usage.jsonl` (one line per session, updated live).
Set `LOG_PRIVACY=1` to omit user-identifying fields.

### Optional: local tracing (OpenTelemetry → Jaeger)

```bash
./otel.sh start      # start a local Jaeger container
./otel.sh status     # check it's up
./otel.sh stop       # stop it
```

Tracing is auto-enabled when Jaeger is running — no flag needed. Trace data is
ephemeral (lost on container stop), which is fine for a local debugging aid.

To view from your laptop:

```bash
ssh -L 16686:localhost:16686 <this-host>
# then open http://localhost:16686
```

## Files

| File | Purpose |
|---|---|
| `env.sh` | Source this to put goose on PATH and set XDG dirs |
| `setup.sh` | Idempotent install + env setup (source or run) |
| `install.sh` | Raw binary installer (called by setup.sh) |
| `sync-mcp.sh` | Sync MCP servers from the internal registry |
| `goose-model.sh` | Interactive model/backend picker (`mu2eai-model`) |
| `otel.sh` | Start/stop/status local Jaeger for tracing |
| `alcf.sh` | Switch to an ALCF inference endpoint |
| `setup-litellm.sh` | Switch to the litellm.fnal.gov endpoint |
| `config.yaml.example` | Tracked config template (live config stays outside this repo) |
| `log-session.py` | Log session usage from goose's SQLite → jsonl or PostgreSQL |
| `guardrails-plugin/` | goose plugin: guardrails, session banner (⚛ mu2eai), usage logging |

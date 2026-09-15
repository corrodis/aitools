# goose harness

A thin wrapper around the [goose](https://github.com/block/goose) CLI for shared
Mu2e/AI machines. Handles binary installation, XDG relocation (home-dir quotas
are small), MCP server sync, Kerberos scrubbing, guardrails, and optional local
tracing via OpenTelemetry.

## Clone

```bash
git clone <repo-url> ~/aitools   # or wherever you want the checkout
cd ~/aitools/harness/goose
```

## Install

```bash
. setup.sh           # source to also put goose on PATH in this shell
```

That's it. `setup.sh` will:

1. Source `env.sh` to redirect XDG dirs to group storage (`/exp/mu2e/app/users/$USER/goose/`)
2. Seed `config.yaml` from `config.yaml.example` if none exists yet
3. Download the goose binary via the upstream installer (skips if already present)
4. Sync MCP servers from the internal registry

For a pinned version: `. setup.sh 1.0.25`

> **Every new shell** needs `. env.sh` (or `. setup.sh`) to put goose on PATH.
> Add it to your `.bashrc` if you want it automatic.

## Use

```bash
goose session        # start an interactive session
mu2eai session       # same thing, alternate alias
```

### Switch models

```bash
goose-model          # interactive fuzzy picker (requires fzf)
```

Backends available: internal `vllm.fnal.gov` (default), ALCF sophia/metis, `litellm.fnal.gov`.

### Sync MCP servers

```bash
./sync-mcp.sh        # re-fetch the registry and update config.yaml
```

### Optional: local tracing (OpenTelemetry → Jaeger)

```bash
./otel.sh start      # start a local Jaeger container (OTLP/HTTP on 127.0.0.1:4318)
./otel.sh status     # check it's up
./otel.sh stop       # stop it
```

`env.sh`'s `goose()` wrapper auto-detects whether Jaeger is listening on every
invocation and enables tracing only when it is — no flag needed.

> **Note:** Trace data lives in a container-internal tmpfs and is lost when the
> container stops (CephFS flock limitations prevent persistent storage here).
> That's fine for a local debugging aid — just restart with `./otel.sh start` for
> the next session.

To view the UI from your laptop:

```bash
ssh -L 16686:localhost:16686 <this-host>
# then open http://localhost:16686 in your browser
```

## Files

| File | Purpose |
|---|---|
| `env.sh` | Source this to put goose on PATH and set XDG dirs |
| `setup.sh` | Idempotent install + env setup (source or execute) |
| `install.sh` | Raw binary installer (called by setup.sh) |
| `sync-mcp.sh` | Sync MCP servers from the internal registry |
| `goose-model.sh` | Interactive model/backend picker |
| `otel.sh` | Start/stop/status local Jaeger for tracing |
| `alcf.sh` | Switch to an ALCF inference endpoint |
| `setup-litellm.sh` | Switch to the litellm.fnal.gov endpoint |
| `config.yaml.example` | Tracked config template (your live config stays outside this repo) |
| `guardrails-plugin/` | goose plugin that blocks ssh/sudo/kinit and scrubs Kerberos |

# claude-code harness

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](../../LICENSE)

Claude Code for the [Mu2e experiment](https://mu2e.fnal.gov), with
home-directory relocation (shared home quotas), MCP server sync, Kerberos
scrubbing, and guardrails hooks. Parallel to `../goose/`, sharing the same
MCP registry and the same `../shared/mu2e.md` context file.

## Install

```bash
cd ~/aitools/harness/claude-code
. setup.sh
```

`setup.sh` will:

1. Check for Claude Code (`npm install -g @anthropic-ai/claude-code` if missing)
2. Redirect `$HOME` for claude to `/exp/mu2e/app/users/$USER/claude-code/home/`
   (keeps `~/.claude.json` and `~/.claude/` off the quota-constrained `/nashome`)
3. Prompt for an API backend if none is configured:
   - **litellm.fnal.gov** (recommended — shared Fermilab endpoint, MIKEY token)
   - **Personal Anthropic API key** (`console.anthropic.com`)
4. Seed `~/.claude/settings.json` (hooks: guardrails, session logging)
5. Symlink `../shared/mu2e.md` → `$CLAUDE_HOME/CLAUDE.md` (auto-discovered context)
6. Sync MCP servers from the internal registry

> **Every new shell** needs `. env.sh` (or `. setup.sh`) to put claude on PATH
> and activate the HOME override.

## Use

```bash
claude          # or: mu2eai-claude
```

## Backends

`setup.sh` prompts for a backend on first run:

| Option | How | Best for |
|--------|-----|---------|
| **litellm.fnal.gov** | Personal API key from https://litellm.fnal.gov | Most users — shared Fermilab-hosted endpoint |
| **Anthropic API key** | Key from console.anthropic.com | Pay-as-you-go direct access |
| **claude.ai subscription** | Skip setup, then `/login` inside claude | Pro/Max/Team/Enterprise subscribers |

To switch to or update the LiteLLM backend later:

```bash
./setup-litellm.sh                 # re-prompts for key if needed, auto-detects model
./setup-litellm.sh --list-models   # see what's available
./setup-litellm.sh --model claude-opus-4-5
```

To use a claude.ai subscription instead, skip the key prompt during setup (option 3)
then run `claude` and type `/login` at the prompt.

## Sync MCP servers

MCP servers are synced automatically on install. Re-run only if the registry
has changed:

```bash
./sync-mcp.sh
```

## Shared context (CLAUDE.md / TOM)

Both Claude Code and the goose harness draw from the same file:

```
../shared/mu2e.md
```

- **Claude Code**: auto-discovered as `$HOME/CLAUDE.md` (symlinked by setup.sh).
  Injected into every session automatically.
- **goose**: injected via the TOM (Top Of Mind) extension using
  `GOOSE_MOIM_MESSAGE_FILE`, set automatically by `../goose/env.sh`.

Edit `../shared/mu2e.md` to update context for both tools at once.

## Hooks / guardrails

Configured in `$CLAUDE_HOME/.claude/settings.json` (seeded from
`settings.json.example`):

| Hook | Trigger | Effect |
|------|---------|--------|
| `block-commands.sh` | PreToolUse (Bash) | Blocks `ssh`, `scp`, `sftp`, `rsync`, `kinit`, `sudo`, `su` |
| `log-session.sh` | UserPromptSubmit + Stop | Usage logging stub (active once `log-session-claude.py` is written) |

Blocking is a heuristic guard rail, not a hard sandbox. The `permissions.deny`
rules in `settings.json` add a second layer (Claude Code's own permission
system), but the agent still runs with your full Unix permissions.

## Session usage logging

Logging is stubbed in `hooks/log-session.sh` pending a `log-session-claude.py`
implementation. Claude Code stores session transcripts as JSONL files under
`$CLAUDE_HOME/.claude/projects/<dir-hash>/<session-id>.jsonl`. Token counts
and tool use are in each message's `usage` field. The full implementation will
mirror what `../goose/log-session.py` does for goose.

## Files

| File | Purpose |
|------|---------|
| `env.sh` | Source to put claude on PATH and activate HOME override |
| `setup.sh` | Idempotent install + config setup (source or run) |
| `setup-litellm.sh` | Configure the litellm.fnal.gov backend |
| `sync-mcp.sh` | Sync MCP servers from the internal registry |
| `sync_mcp_claude.py` | Python worker for sync-mcp.sh |
| `settings.json.example` | Tracked settings template (live settings outside this repo) |
| `hooks/block-commands.sh` | PreToolUse guardrail |
| `hooks/log-session.sh` | Usage logging hook stub |
| `../shared/mu2e.md` | Shared experiment context (CLAUDE.md + goose TOM) |

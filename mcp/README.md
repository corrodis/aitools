# Mu2e MCP Workspace (AI-facing)

This directory contains MCP server source projects only.

## Goals

- Keep MCP source under git in one place.
- Keep runtime/deployed artifacts out of git.
- Install each MCP independently (different schedules, dependencies, and ownership windows).
- Keep one per-MCP registry plus an optional combined registry for clients like Cline.

## Directory structure

- `registry/` (prototype)
  - Streamable-HTTP MCP server, proof of principle for the newer pattern:
    `pyproject.toml` package installed with `uv` (git subdirectory syntax)
    into a server account, run persistently under `systemd --user`, reached
    over HTTP instead of spawned per-connection over stdio. Also serves a
    live MCP-server registry (`GET /registry`) for client config bootstrap.
  - Own install script: `registry/scripts/install.sh`
  - See `registry/README.md` for the full pattern; other MCPs will migrate
    to this shape over time.
- `metacat/`
  - Source for the read-only metacat MCP server.
  - Own install script: `metacat/scripts/install.sh`
  - Own registry output target (in shared deploy tree):
    - `<deploy-root>/metacat/registry/mcp-servers.json`
- `sim-epochs/`
  - Source for the sim-epochs MCP server.
  - Own install script: `sim-epochs/scripts/install.sh`
  - Own registry output target (in shared deploy tree):
    - `<deploy-root>/sim-epochs/registry/mcp-servers.json`
- `dqm/`
  - Source for the DQM Query Engine read-only MCP server.
  - Own install script: `dqm/scripts/install.sh`
  - Own registry output target (in shared deploy tree):
    - `<deploy-root>/dqm/registry/mcp-servers.json`
- `slack/`
  - Read-only MCP server for an allowlisted set of Slack channels
    (`#mu2e-shift`): `slack_list_channels`, `slack_read_channel`,
    `slack_read_thread`. HTTP (port 8009, mikey; not in the registry yet) and
    a `slack-mcp-stdio` entry point for local agents (daqpy, the Slack bot).
  - Own install script: `slack/scripts/install.sh`
- `code-index/` (optional)
  - Source wrapper for upstream `code-index-mcp`.
  - Own install script: `code-index/scripts/install.sh`
  - Logical server names in registries:
    - `py-index-central` for shared repos and shared cached index
    - `py-index-local` for user-local indexing on demand
  - Physical deploy path remains `<deploy-root>/code-index`.
- `scripts/`
  - Cross-MCP helper scripts.
  - `write_joint_registry.sh` creates one registry with all MCP servers.
    - Adds `py-index-central`/`py-index-local` when `<deploy-root>/code-index/current` exists.
  - `install_user_mcp_config.sh` links or copies a user-local MCP config to the joint registry.
- `.gitignore`
  - Excludes virtual environments and runtime artifacts (`.venv`, `venv`, caches, local deploy sandbox).

## Shared deploy layout (outside git, group account)

Recommended target root example:

- `/exp/mu2e/app/users/mu2epro/mcp/deploy`

Installed MCP subtrees:

- `/exp/mu2e/app/users/mu2epro/mcp/deploy/metacat`
- `/exp/mu2e/app/users/mu2epro/mcp/deploy/sim-epochs`
- `/exp/mu2e/app/users/mu2epro/mcp/deploy/dqm`
- `/exp/mu2e/app/users/mu2epro/mcp/deploy/code-index` (optional; serves `py-index-*`)

Each subtree follows:

- `releases/<version>/`
- `current` (symlink)
- `registry/mcp-servers.json`
- optional `catalog/` (sim-epochs)

## Install flow

1. Install sim-epochs (as `mu2epro`):
  - `sim-epochs/scripts/install.sh /exp/mu2e/app/users/mu2epro/mcp/deploy/sim-epochs <version> [catalog] [group]`
2. Install metacat (as `mu2epro`):
  - `metacat/scripts/install.sh /exp/mu2e/app/users/mu2epro/mcp/deploy/metacat <version> [group]`
3. Install dqm (as `mu2epro`):
  - `dqm/scripts/install.sh /exp/mu2e/app/users/mu2epro/mcp/deploy/dqm <version> [group]`
4. Install Python/general code index (optional, as `mu2epro`):
  - `code-index/scripts/install.sh /exp/mu2e/app/users/mu2epro/mcp/deploy/code-index <tag|latest> [group]`
  - Default central repo root: `/exp/mu2e/app/users/mu2epro/mcp/deploy/code-index/repos`
  - Default central index root: `/exp/mu2e/app/users/mu2epro/mcp/deploy/code-index/indexer`
  - Registry entries are named `py-index-central` and `py-index-local`
5. Generate combined registry:
   - `scripts/write_joint_registry.sh /exp/mu2e/app/users/mu2epro/mcp/deploy`
6. Point a user config at the joint registry:
  - `scripts/install_user_mcp_config.sh /exp/mu2e/app/users/mu2epro/mcp/deploy/registry/mcp-servers.json ~/.config/llm-harness/mcp.json link`

## Python/General index behavior

- `py-index-central` is intended for shared centrally cached repos/indexes.
- `py-index-local` is intended for user-local indexing on demand.
- Keep deep/local indexing user-driven, not automatic.

Default combined registry output:

- `/exp/mu2e/app/users/mu2epro/mcp/deploy/registry/mcp-servers.json`

## Dependency policy

- Do not commit virtual environments.
- Each MCP keeps a `requirements.txt` for dependency bootstrap in install scripts.
- Install scripts create a fresh `venv` in the release directory and run pip there.

## AI guidance

- Treat this directory as source-of-truth for MCP code.
- Do not write runtime data or deploy trees inside git paths.
- Preserve per-MCP install independence.
- Keep tool names stable once published; avoid silent renames.

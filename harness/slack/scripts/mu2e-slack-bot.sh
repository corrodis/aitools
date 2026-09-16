#!/usr/bin/env bash
set -euo pipefail

# --- Server-side environment setup -----------------------------------------
# Placeholder for anything the bot needs loaded on the host before it can run.
# It needs nothing today: it is a pure HTTP client (Slack, the LLM endpoint,
# the MCP servers) with no mu2e/cvmfs offline-software dependency -- and
# sourcing the offline environment here would actively hurt, since `muse setup`
# exports a PYTHONPATH that shadows this venv's pinned deps (the same trap
# documented in mcp/dqm/README.md).
# -----------------------------------------------------------------------------

# Installed by `uv pip install` as a sibling of the `mu2e-slack-bot` console
# script, so this resolves regardless of which release's venv it runs from.
exec "$(dirname "$0")/mu2e-slack-bot" "$@"

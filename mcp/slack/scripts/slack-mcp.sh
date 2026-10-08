#!/usr/bin/env bash
set -euo pipefail

# --- Server-side environment setup -----------------------------------------
# Placeholder for anything slack-mcp needs loaded on the host before it can
# run. It needs nothing: slack_sdk is a plain HTTPS client, so there is no
# mu2e/cvmfs offline-software environment to source (and sourcing one would
# hurt -- see dqm-mcp.sh on muse's PYTHONPATH shadowing pinned deps).
#
# SLACK_BOT_TOKEN, SLACK_MCP_CHANNELS and MIKEY_KEYS_FILE come from the
# environment, not from flags -- see slack-mcp-install-unit.sh --env-file.
# -----------------------------------------------------------------------------

# Installed by `uv pip install` as a sibling of the `slack-mcp` console
# script, so this resolves regardless of which release's venv it runs from.
exec "$(dirname "$0")/slack-mcp" "$@"

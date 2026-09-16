#!/usr/bin/env bash
# Checked before `set -euo pipefail` below -- see goose/install.sh for why.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  echo "Error: run this script, don't source it. Use: ./sync-mcp.sh" >&2
  return 1 2>/dev/null || exit 1
fi

set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  ./sync-mcp.sh [registry-url]

Fetches the MCP server registry (default:
http://mu2eaigpvm01.fnal.gov:8000/registry) and upserts every server
listed there into Claude Code's ~/.claude.json as user-scope HTTP MCP
servers. Existing entries are overwritten; anything else in ~/.claude.json
is left unchanged. Safe to re-run as the registry changes.

For servers whose registry entry has "token": "yes", the Authorization
header is filled in from (in order):
  1. $MIKEY_TOKEN, if set
  2. /exp/mu2e/app/users/$USER/mikey_token, if that file exists (private,
     0600, shared between the goose and claude-code harnesses -- see
     mikey/README.md at the repo root for how to get a token)
If neither is available, that server is added with a disabled marker
rather than skipped or left half-configured.

Requires env.sh to have been sourced (for CLAUDE_HOME).
USAGE
  exit 2
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/env.sh"

registry_url="${1:-http://mu2eaigpvm01.fnal.gov:8000/registry}"
claude_json="$CLAUDE_HOME/.claude.json"
token_file="/exp/mu2e/app/users/$USER/mikey_token"

if [[ ! -f "$claude_json" ]]; then
  # Create a minimal ~/.claude.json so sync_mcp_claude.py can update it.
  # A real first-run creates this on first `claude` invocation, but sync
  # may run before that.
  ( umask 077 && printf '{}\n' > "$claude_json" )
  echo "Created $claude_json (minimal skeleton)"
fi

if [[ -z "${MIKEY_TOKEN:-}" && -f "$token_file" ]]; then
  MIKEY_TOKEN="$(cat "$token_file")"
fi
export MIKEY_TOKEN="${MIKEY_TOKEN:-}"

tmp_json="$(mktemp)"
trap 'rm -f "$tmp_json"' EXIT

echo "Fetching registry: $registry_url"
curl -fsSL "$registry_url" -o "$tmp_json"

python3 "$script_dir/sync_mcp_claude.py" "$tmp_json" "$claude_json"

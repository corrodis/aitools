#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  install.sh <deploy-root> <ref> [repo-url]

Installs slack-mcp into <deploy-root>/releases/<ref>/.venv using uv, pinned
to the given git ref (tag/branch/commit) of the aitools repo:

  uv venv <deploy-root>/releases/<ref>/.venv
  uv pip install --python <...>/.venv/bin/python \
    "slack-mcp @ git+<repo-url>@<ref>#subdirectory=mcp/slack"

mikey (this same repo, mcp/mikey) comes in transitively. No source tree is
copied; the venv then has:

  <...>/.venv/bin/slack-mcp                    (python entry point, HTTP)
  <...>/.venv/bin/slack-mcp-stdio              (stdio transport, for local agents)
  <...>/.venv/bin/slack-mcp.sh                 (bash wrapper, execs slack-mcp)
  <...>/.venv/bin/slack-mcp-install-unit.sh    (renders + links the systemd --user unit)
  <...>/.venv/share/slack-mcp/slack-mcp.env.example

<deploy-root>/current is symlinked to the new release. This script does not
touch systemd -- run the printed install-unit command when ready.

Examples:
  ./scripts/install.sh /exp/mu2e/app/users/mu2epro/mcp/deploy/slack v0.1.0
  ./scripts/install.sh /exp/mu2e/app/users/mu2epro/mcp/deploy/slack main \
      https://github.com/Mu2e/aitools

Notes:
  - Run as the account that will run the systemd --user service.
  - Requires `uv` on PATH.
  - Needs a Slack bot token (SLACK_BOT_TOKEN) with channels:history,
    channels:read, users:read (+ groups:history, groups:read for private
    channels), invited to every channel in SLACK_MCP_CHANNELS. Credentials go
    in the env file (slack-mcp-install-unit.sh --env-file), never in flags.
USAGE
  exit 2
}

if [[ $# -lt 2 || $# -gt 3 ]]; then
  usage
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv not found on PATH" >&2
  exit 2
fi

deploy_root="$1"
ref="$2"
repo_url="${3:-https://github.com/Mu2e/aitools}"

release_dir="$deploy_root/releases/$ref"
current_link="$deploy_root/current"
venv_dir="$release_dir/.venv"

mkdir -p "$release_dir"

echo "[1/2] Creating venv: $venv_dir"
uv venv "$venv_dir"

echo "[2/2] Installing slack-mcp from ${repo_url}@${ref} (subdirectory: mcp/slack)"
uv pip install --python "$venv_dir/bin/python" \
  "slack-mcp @ git+${repo_url}@${ref}#subdirectory=mcp/slack"

ln -sfn "$release_dir" "$current_link"

echo "Done."
echo "Current release: $current_link -> $release_dir"
echo
echo "Next steps:"
echo "  1. cp $venv_dir/share/slack-mcp/slack-mcp.env.example $deploy_root/slack-mcp.env; edit; chmod 600"
echo "  2. set -a; . $deploy_root/slack-mcp.env; set +a; $venv_dir/bin/slack-mcp.sh --check"
echo "  3. $venv_dir/bin/slack-mcp-install-unit.sh --port 8009 --env-file $deploy_root/slack-mcp.env"

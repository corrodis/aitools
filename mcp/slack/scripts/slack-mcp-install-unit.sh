#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  slack-mcp-install-unit.sh [--port <port>] [--host <host>] [--env-file <path>]
                            [--no-enable]

Renders a systemd --user unit for slack-mcp into THIS install's own share/
directory (share/slack-mcp/slack-mcp.service) and registers it with
`systemctl --user link`, so the unit content stays with the installed code
and ~/.config only holds a pointer (same pattern as ecl-mcp).

--env-file points at a file of KEY=VALUE lines (SLACK_BOT_TOKEN,
SLACK_MCP_CHANNELS, MIKEY_KEYS_FILE, optionally SLACK_MCP_MAX_MESSAGES)
that becomes this unit's EnvironmentFile=. The bot token never goes into
ExecStart or a flag: argv is visible to every user via `ps` and would be
baked into the rendered unit. Keep the env file 0600, owned by the account
that runs this unit.

Safe to re-run (after changing --port, or after a redeploy).

Examples:
  <venv>/bin/slack-mcp-install-unit.sh --env-file /path/to/slack-mcp.env
  <venv>/bin/slack-mcp-install-unit.sh --port 8009 --env-file /path/to/slack-mcp.env --no-enable
USAGE
  exit 2
}

port=8009
host=0.0.0.0
env_file=""
do_enable=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) port="$2"; shift 2 ;;
    --host) host="$2"; shift 2 ;;
    --env-file) env_file="$2"; shift 2 ;;
    --no-enable) do_enable=0; shift ;;
    --help|-h) usage ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage ;;
  esac
done

if [[ -z "$env_file" ]]; then
  echo "WARNING: no --env-file given -- slack-mcp will start with whatever" >&2
  echo "         SLACK_BOT_TOKEN/SLACK_MCP_CHANNELS/MIKEY_KEYS_FILE are already" >&2
  echo "         in this systemd --user session's environment (rarely intended)." >&2
elif [[ ! -f "$env_file" ]]; then
  echo "ERROR: --env-file not found: $env_file" >&2
  exit 2
elif [[ "$(stat -c '%a' "$env_file")" != "600" ]]; then
  # Holds a workspace-wide bot token; a world-readable copy on a shared
  # machine hands it to every account on the host.
  echo "ERROR: $env_file must be mode 600 (chmod 600 '$env_file')" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "$0")" && pwd)"
venv_root="$(cd "$script_dir/.." && pwd)"
share_dir="$venv_root/share/slack-mcp"
unit_file="$share_dir/slack-mcp.service"

mkdir -p "$share_dir"

exec_start="$script_dir/slack-mcp.sh --host=$host --port=$port"

environment_line=""
[[ -n "$env_file" ]] && environment_line="EnvironmentFile=$env_file"

cat > "$unit_file" <<EOF
[Unit]
Description=slack-mcp (read-only MCP server for allowlisted Mu2e Slack channels, mikey-authenticated)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
$environment_line
ExecStart=$exec_start
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

echo "Wrote unit: $unit_file"

mkdir -p "$HOME/.config/systemd/user"
systemctl --user link --force "$unit_file"
systemctl --user daemon-reload

if [[ $do_enable -eq 1 ]]; then
  # Enable by path, not by name, so default.target.wants points through
  # <deploy-root>/current rather than at one release dir.
  systemctl --user enable --force "$unit_file"
  systemctl --user daemon-reload
  systemctl --user restart slack-mcp
  systemctl --user status slack-mcp --no-pager
else
  echo "Run manually:"
  echo "  systemctl --user enable --now slack-mcp"
fi

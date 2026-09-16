#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  mu2e-slack-bot-install-unit.sh --env-file <path> [--no-enable] [-- <bot args...>]

Renders a systemd --user unit for mu2e-slack-bot into THIS install's own
share/ directory (share/mu2e-slack-bot/mu2e-slack-bot.service) -- not into
~/.config -- and registers it with `systemctl --user link`, which creates a
symlink in ~/.config/systemd/user pointing back here. The real unit content
stays with the installed code; ~/.config only ever holds a pointer to it.
Same mechanism as mcp/registry's installer.

ExecStart is resolved from this script's own location, so no path needs to be
hand-edited.

  --env-file <path>  File holding the secrets (SLACK_BOT_TOKEN,
                     SLACK_APP_TOKEN, MIKEY_TOKEN, optional OPENAI_API_KEY).
                     Must be mode 0600 and owned by this account -- these are
                     workspace- and collaboration-wide tokens. Start from
                     share/mu2e-slack-bot/mu2e-slack-bot.env.example.
  --no-enable        Render and link the unit but do not start it.
  -- <bot args...>   Everything after `--` is appended to ExecStart verbatim,
                     e.g. --channel mu2e-ai --model gpt-oss:120b.

Example:
  <venv>/bin/mu2e-slack-bot-install-unit.sh \
      --env-file /exp/mu2e/app/home/mu2eai/slack/mu2e-slack-bot.env \
      -- --channel mu2e-ai
USAGE
  exit 2
}

env_file=""
do_enable=1
bot_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) env_file="$2"; shift 2 ;;
    --no-enable) do_enable=0; shift ;;
    --) shift; bot_args=("$@"); break ;;
    --help|-h) usage ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage ;;
  esac
done

if [[ -z "$env_file" ]]; then
  echo "ERROR: --env-file is required" >&2
  usage
fi
if [[ ! -f "$env_file" ]]; then
  echo "ERROR: env file not found: $env_file" >&2
  exit 1
fi

# The tokens in this file are shared across the collaboration; a world-readable
# copy on a multi-user machine leaks them to every account on the host.
perms="$(stat -c '%a' "$env_file")"
if [[ "$perms" != "600" ]]; then
  echo "ERROR: $env_file is mode $perms -- must be 600 (chmod 600 '$env_file')" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "$0")" && pwd)"
venv_root="$(cd "$script_dir/.." && pwd)"
share_dir="$venv_root/share/mu2e-slack-bot"
unit_file="$share_dir/mu2e-slack-bot.service"

mkdir -p "$share_dir"

cat > "$unit_file" <<EOF
[Unit]
Description=mu2e-slack-bot (Slack frontend for the Mu2e AI agent)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$env_file
ExecStart=$script_dir/mu2e-slack-bot.sh ${bot_args[*]}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

echo "Wrote unit: $unit_file"

mkdir -p "$HOME/.config/systemd/user"
systemctl --user link --force "$unit_file"
systemctl --user daemon-reload

if [[ $do_enable -eq 1 ]]; then
  systemctl --user enable --now mu2e-slack-bot
  systemctl --user status mu2e-slack-bot --no-pager
else
  echo "Run manually:"
  echo "  systemctl --user enable --now mu2e-slack-bot"
fi

#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  install.sh <deploy-root> <ref> [repo-url]

Installs mu2e-slack-bot into <deploy-root>/releases/<ref>/.venv using uv,
pinned to the given git ref (tag/branch/commit) of the aitools repo, via uv's
git subdirectory install syntax:

  uv venv <deploy-root>/releases/<ref>/.venv
  uv pip install --python <...>/.venv/bin/python \
    "mu2e-slack-bot @ git+<repo-url>@<ref>#subdirectory=harness/slack"

That's the entire install -- no source tree is copied. It produces, in the
venv:

  <...>/.venv/bin/mu2e-slack-bot                 (python entry point)
  <...>/.venv/bin/mu2e-slack-bot.sh              (bash wrapper, execs it)
  <...>/.venv/bin/mu2e-slack-bot-install-unit.sh (renders + links the systemd
                                                   --user unit)
  <...>/.venv/share/mu2e-slack-bot/mu2e-slack-bot.env.example

<deploy-root>/current is symlinked to the new release. This script does not
touch systemd -- run the printed install-unit command when you are ready.

Rollback is repointing <deploy-root>/current (or re-running this with an older
ref) and restarting the unit. The env file lives at the deploy root, outside
any one release, so it survives upgrades.

Examples:
  ./scripts/install.sh /exp/mu2e/app/home/mu2eai/slack/deploy v0.1.0
  ./scripts/install.sh /exp/mu2e/app/home/mu2eai/slack/deploy main \
      https://github.com/Mu2e/aitools

Notes:
  - Run as the account that will run the systemd --user service (e.g. mu2eai).
  - Requires `uv` on PATH.
  - Secrets never go in argv or in the release; they belong in the env file
    referenced by the unit (see mu2e-slack-bot-install-unit.sh --help).
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

echo "[2/2] Installing mu2e-slack-bot from ${repo_url}@${ref} (subdirectory: harness/slack)"
uv pip install --python "$venv_dir/bin/python" \
  "mu2e-slack-bot @ git+${repo_url}@${ref}#subdirectory=harness/slack"

ln -sfn "$release_dir" "$current_link"

echo "Done."
echo "Current release: $current_link -> $release_dir"
echo
echo "Next steps:"
echo "  1. cp $venv_dir/share/mu2e-slack-bot/mu2e-slack-bot.env.example $deploy_root/mu2e-slack-bot.env"
echo "     \$EDITOR $deploy_root/mu2e-slack-bot.env && chmod 600 $deploy_root/mu2e-slack-bot.env"
echo "  2. set -a; . $deploy_root/mu2e-slack-bot.env; set +a; $venv_dir/bin/mu2e-slack-bot.sh --check --channel <channel>"
echo "  3. $venv_dir/bin/mu2e-slack-bot-install-unit.sh --env-file $deploy_root/mu2e-slack-bot.env -- --channel <channel>"

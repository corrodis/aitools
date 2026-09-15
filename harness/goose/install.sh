#!/usr/bin/env bash

# Checked before `set -euo pipefail` below: those options would otherwise
# leak into the caller's interactive shell if this is sourced by mistake
# (`.`/`source` don't revert shell option changes on return/error), which
# can silently kill an SSH session on the next unbound-variable or
# nonzero-status command it hits.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  echo "Error: run this script, don't source it (it calls exit, which would" >&2
  echo "terminate your current shell). Use: ./install.sh" >&2
  return 1 2>/dev/null || exit 1
fi

set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  install.sh [version]

Installs the goose CLI (https://github.com/block/goose) as a prebuilt
binary from GitHub releases via the upstream download_cli.sh -- no build
step, no sudo.

  version   Optional goose version to pin (e.g. "1.0.25" or "v1.0.25").
            Defaults to the latest "stable" release.

Installs to:
  /exp/mu2e/app/users/$USER/goose/bin/goose

(Not $HOME/.local/bin -- the default -- so the binary sits on group
storage instead of a small-quota home dir, next to this user's goose
config/data set up separately under harness/goose.)

Skips the upstream installer's interactive 'goose configure' step
(CONFIGURE=false): config.yaml is written by this harness instead of by
answering prompts.

Examples:
  ./install.sh            # latest stable
  ./install.sh 1.0.25     # pinned version
USAGE
  exit 2
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -gt 1 ]]; then
  usage
fi

version="${1:-}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/env.sh"
mkdir -p "$GOOSE_BIN_DIR"

echo "Installing goose to $GOOSE_BIN_DIR"

env_args=(CONFIGURE=false)
if [[ -n "$version" ]]; then
  env_args+=(GOOSE_VERSION="$version")
fi

curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh \
  | env "${env_args[@]}" bash

echo
echo "Installed: $GOOSE_BIN_DIR/goose"
echo
echo "PATH already updated for this shell (via env.sh). For a new shell:"
echo "  . $script_dir/env.sh"

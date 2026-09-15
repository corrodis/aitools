#!/usr/bin/env bash
# Usage:
#   . setup.sh [version]     (sourced -- also leaves goose on PATH in your shell)
#   ./setup.sh [version]     (executed -- installs only, doesn't touch your shell)
#
# Idempotent: installs goose only if it isn't already present at
# $GOOSE_BIN_DIR/goose (see env.sh, which this always sources). Safe to
# re-run either way.
#
#   version   Passed through to install.sh if an install is actually needed.
#
# To force a reinstall/upgrade even though goose is already present, run
# install.sh directly instead.
#
# Deliberately does NOT `set -e`/`set -u` at this file's top level: this
# script is meant to be sourceable, and those options would otherwise
# leak into the caller's interactive shell and can silently kill it (see
# install.sh's comment on this same issue) -- so errors are checked
# explicitly below instead. install.sh keeps its own `set -euo pipefail`
# safely, because it's always run here as a plain subprocess (`bash
# install.sh`), never sourced.

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat >&2 <<'USAGE'
Usage:
  . setup.sh [version]
  ./setup.sh [version]
USAGE
  return 2 2>/dev/null || exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$script_dir" ]]; then
  echo "Error: could not resolve script directory" >&2
  return 1 2>/dev/null || exit 1
fi

if ! source "$script_dir/env.sh"; then
  echo "Error: failed to source env.sh" >&2
  unset script_dir
  return 1 2>/dev/null || exit 1
fi

config_file="$XDG_CONFIG_HOME/goose/config.yaml"
if [[ ! -e "$config_file" && -f "$script_dir/config.yaml.example" ]]; then
  echo "No goose config yet -- seeding from harness template: $config_file"
  mkdir -p "$(dirname "$config_file")"
  if ! ( umask 077 && cp "$script_dir/config.yaml.example" "$config_file" ); then
    echo "Error: failed to seed $config_file" >&2
    unset script_dir config_file
    return 1 2>/dev/null || exit 1
  fi
fi
unset config_file

if [[ -x "$GOOSE_BIN_DIR/goose" ]]; then
  echo "goose already installed: $GOOSE_BIN_DIR/goose"
  "$GOOSE_BIN_DIR/goose" --version
else
  echo "goose not found at $GOOSE_BIN_DIR/goose -- installing"
  if ! bash "$script_dir/install.sh" "$@"; then
    echo "Error: install.sh failed" >&2
    unset script_dir
    return 1 2>/dev/null || exit 1
  fi
fi

# Non-fatal: no network / registry down shouldn't block getting goose
# itself usable. Gated servers just come in disabled if there's no mikey
# token yet (see sync_mcp.py) -- rerun sync-mcp.sh by hand once you have one.
echo "Syncing MCP servers from the registry..."
if ! bash "$script_dir/sync-mcp.sh"; then
  echo "Warning: sync-mcp.sh failed -- goose is installed, but MCP servers weren't synced. Run ./sync-mcp.sh by hand once the registry is reachable." >&2
fi

unset script_dir

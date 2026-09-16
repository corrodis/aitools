#!/usr/bin/env bash
# Checked before `set -euo pipefail` below -- see setup.sh for why.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  echo "Error: run this script, don't source it. Use: ./claude-model.sh" >&2
  return 1 2>/dev/null || exit 1
fi

set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  ./claude-model.sh

Interactive model picker for Claude Code + litellm.fnal.gov: queries the
live model list, lets you pick one via fzf, and writes ANTHROPIC_MODEL to
$CLAUDE_HOME/.env so every subsequent `claude` invocation uses it.

Why not use Claude Code's built-in /model command?
  /model resolves shortnames like "claude-haiku-4-5" to their full
  Anthropic versioned IDs (e.g. "claude-haiku-4-5-20251001"), which the
  litellm.fnal.gov endpoint doesn't recognize -- it uses "azure/..." names.
  This picker only shows models actually available on litellm.fnal.gov.
USAGE
  exit 2
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/env.sh"

if ! command -v fzf >/dev/null 2>&1; then
  echo "Error: fzf not found on PATH -- required for the picker" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Resolve litellm API key (same precedence as setup-litellm.sh)
# ---------------------------------------------------------------------------
key_file="$CLAUDE_HOME/litellm_key"

if [[ -z "${LITELLM_KEY:-}" && -f "$key_file" ]]; then
  LITELLM_KEY="$(cat "$key_file")"
fi

if [[ -z "${LITELLM_KEY:-}" ]]; then
  echo "No litellm.fnal.gov API key found." >&2
  echo "Run:  ./setup-litellm.sh   to configure the litellm backend first." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Fetch and filter model list -- only Claude models by default
# ---------------------------------------------------------------------------
host="https://litellm.fnal.gov"
env_file="$CLAUDE_HOME/.env"

echo "Fetching model list from $host..." >&2
models_json="$(curl -fsSL \
  -H "Authorization: Bearer $LITELLM_KEY" \
  "$host/v1/models" 2>/dev/null || echo '{}')"

all_models="$(printf '%s' "$models_json" \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
for m in d.get('data', []):
    print(m.get('id',''))
" 2>/dev/null | grep -v '^$')"

if [[ -z "$all_models" ]]; then
  echo "Error: could not fetch model list from $host" >&2
  exit 1
fi

# Default: show only claude models; show all with --all
if [[ "${1:-}" == "--all" ]]; then
  model_list="$all_models"
  prompt="all models> "
else
  model_list="$(printf '%s\n' "$all_models" | grep -i "claude" || true)"
  if [[ -z "$model_list" ]]; then
    echo "No Claude models found; showing all models." >&2
    model_list="$all_models"
  fi
  prompt="claude model> "
fi

# ---------------------------------------------------------------------------
# Pick
# ---------------------------------------------------------------------------
picked="$(printf '%s\n' "$model_list" | fzf --prompt="$prompt" --tac)"

if [[ -z "$picked" ]]; then
  echo "No model selected." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Write to .env -- update or add ANTHROPIC_MODEL line
# ---------------------------------------------------------------------------
if [[ -f "$env_file" ]]; then
  if grep -q "^export ANTHROPIC_MODEL=" "$env_file"; then
    # Update existing line
    sed -i "s|^export ANTHROPIC_MODEL=.*|export ANTHROPIC_MODEL=\"$picked\"|" "$env_file"
  else
    # Append
    printf '\n# Model selected by claude-model.sh\nexport ANTHROPIC_MODEL="%s"\n' "$picked" >> "$env_file"
  fi
else
  # No .env yet -- create minimal one
  ( umask 077 && printf 'export ANTHROPIC_MODEL="%s"\n' "$picked" > "$env_file" )
fi
chmod 600 "$env_file"

echo "Model set to: $picked"
echo "Saved to: $env_file"
echo ""
echo "Re-source env.sh to pick it up in your current shell:"
echo "  . $(dirname "$script_dir")/claude-code/env.sh"

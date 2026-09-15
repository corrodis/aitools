#!/usr/bin/env bash
# Checked before `set -euo pipefail` below -- see install.sh for why.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  echo "Error: run this script, don't source it. Use: ./goose-model.sh" >&2
  return 1 2>/dev/null || exit 1
fi

set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  ./goose-model.sh

Interactive model picker: pick a backend (the internal vllm.fnal.gov
default, ALCF sophia, ALCF metis, or litellm.fnal.gov), see its live
model list in an fzf fuzzy-picker, and switch goose to it.

Thin orchestrator, not a reimplementation -- it just calls alcf.sh /
setup-litellm.sh under the hood with the model you pick, reusing their
existing auth and --list-models logic. goose itself has no CLI command
to list a remote provider's models (confirmed against goose-docs.ai) --
this only knows about the backends we've explicitly wired in above.
USAGE
  exit 2
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
fi

if ! command -v fzf >/dev/null 2>&1; then
  echo "Error: fzf not found on PATH -- required for the picker" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/env.sh"

backend="$(printf '%s\n' \
  "default   -- internal vllm.fnal.gov (no auth, single model)" \
  "alcf      -- Argonne (asks sophia/metis next)" \
  "litellm   -- litellm.fnal.gov" \
  | fzf --prompt="backend> " --with-nth=1,2 --delimiter=' -- ' | awk '{print $1}')"

if [[ -z "$backend" ]]; then
  echo "No backend selected." >&2
  exit 1
fi

case "$backend" in
  default)
    exec "$script_dir/alcf.sh" --restore-default
    ;;
  alcf)
    cluster="$(printf '%s\n' metis sophia | fzf --prompt="ALCF cluster> ")"
    if [[ -z "$cluster" ]]; then
      echo "No cluster selected." >&2
      exit 1
    fi
    source "$script_dir/alcf_ensure_token.sh"
    echo "Fetching model list for ALCF $cluster..." >&2
    models="$("$script_dir/alcf.sh" --list-models --cluster "$cluster" | tail -n +2 | sed -E 's/^[[:space:]]+//')"
    picked="$(printf '%s\n' "$models" | fzf --prompt="ALCF $cluster model> ")"
    if [[ -z "$picked" ]]; then
      echo "No model selected." >&2
      exit 1
    fi
    exec "$script_dir/alcf.sh" --cluster "$cluster" --model "$picked"
    ;;
  litellm)
    source "$script_dir/litellm_ensure_key.sh"
    echo "Fetching model list for litellm.fnal.gov..." >&2
    models="$(LITELLM_KEY="$LITELLM_KEY" "$script_dir/setup-litellm.sh" --list-models | tail -n +2 | sed -E 's/^[[:space:]]+//')"
    picked="$(printf '%s\n' "$models" | fzf --prompt="litellm model> ")"
    if [[ -z "$picked" ]]; then
      echo "No model selected." >&2
      exit 1
    fi
    exec env LITELLM_KEY="$LITELLM_KEY" "$script_dir/setup-litellm.sh" --model "$picked"
    ;;
esac

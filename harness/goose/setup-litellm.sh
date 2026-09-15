#!/usr/bin/env bash
# Checked before `set -euo pipefail` below -- see install.sh for why.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  echo "Error: run this script, don't source it. Use: ./setup-litellm.sh" >&2
  return 1 2>/dev/null || exit 1
fi

set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  ./setup-litellm.sh --model NAME
  ./setup-litellm.sh --list-models

Configures goose's `litellm` provider slot to point at litellm.fnal.gov.
Prompts for the API key with input hidden (not taken as a CLI argument --
argv is visible to other users via `ps` on a shared machine).

Independent of the `openai` slot (vllm.fnal.gov by default, or ALCF via
alcf.sh) -- both stay configured at once; this only makes litellm the
active provider. To switch back: `./alcf.sh --restore-default` (that
resets active_provider to openai regardless of what's currently active,
despite the name -- it doesn't touch litellm's settings either way).

  --model NAME       Model to request. Required unless --list-models.
  --list-models      Query litellm.fnal.gov/v1/models with the given key
                      and print what's available, without changing config.
USAGE
  exit 2
}

model=""
list_only=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)
      model="${2:-}"
      shift 2
      ;;
    --list-models)
      list_only=1
      shift
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Error: unknown argument '$1'" >&2
      usage
      ;;
  esac
done

if [[ "$list_only" == "0" && -z "$model" ]]; then
  echo "Error: --model NAME is required (or pass --list-models)" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/env.sh"
config_file="$XDG_CONFIG_HOME/goose/config.yaml"

if [[ ! -f "$config_file" ]]; then
  echo "Error: no live config.yaml yet at $config_file -- run setup.sh first" >&2
  exit 1
fi

host="https://litellm.fnal.gov"

source "$script_dir/litellm_ensure_key.sh"

if [[ "$list_only" == "1" ]]; then
  echo "Available models on litellm.fnal.gov:"
  curl -sS -X GET "$host/v1/models" -H "Authorization: Bearer ${LITELLM_KEY}" \
    | python3 -c "
import sys, json
data = json.load(sys.stdin)
for m in data.get('data', []):
    print(f\"  {m.get('id')}\")
"
  exit 0
fi

# Try to capture the model's real context limit from litellm.fnal.gov's
# own /v1/models response (if the proxy publishes it) -- goose fetches
# this same data itself for LiteLLM-style providers but has a documented
# bug that discards it (aaif-goose/goose issue #8835), silently falling
# back to a hardcoded 128k. We do our own parsing here to work around
# that, rather than relying on goose's (currently broken) auto-detection.
context_limit="$(curl -sS -X GET "$host/v1/models" -H "Authorization: Bearer ${LITELLM_KEY}" \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
model = '$model'
for m in data.get('data', []):
    if m.get('id') != model:
        continue
    info = m.get('model_info') or {}
    for key in ('max_input_tokens', 'context_length', 'max_context_length', 'max_tokens'):
        v = m.get(key) or info.get(key)
        if v:
            print(int(v))
            sys.exit(0)
    break
" 2>/dev/null || true)"

LITELLM_API_KEY_VALUE="$LITELLM_KEY" python3 "$script_dir/litellm_set_provider.py" set "$config_file" "$host" "$model" "$context_limit"

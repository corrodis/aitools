#!/usr/bin/env bash
# Checked before `set -euo pipefail` below -- see install.sh for why.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  echo "Error: run this script, don't source it. Use: ./alcf.sh" >&2
  return 1 2>/dev/null || exit 1
fi

set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  ./alcf.sh --model NAME [--cluster sophia|metis]
  ./alcf.sh --list-models [--cluster sophia|metis]
  ./alcf.sh --restore-default

Gets an ALCF (Argonne Leadership Computing Facility) inference-service
access token -- silently renewing a cached one if possible, otherwise
printing a URL to open in a browser and prompting you to paste back the
resulting authorization code -- and writes it into the LIVE, private
goose config.yaml under the `openai` provider slot (OPENAI_BASE_URL,
providers.openai.model), the same slot setup.sh seeds with the internal
vllm.fnal.gov default. This *overwrites* that slot; --restore-default
puts the template's vllm.fnal.gov settings back.

(ALCF doesn't use the `litellm` slot: that's reserved for the actual
litellm.fnal.gov proxy, configured separately by setup-litellm.sh.)

  --cluster sophia|metis   Which ALCF cluster to use (default: metis).
  --model NAME             Model to request. Required unless
                            --list-models or --restore-default is given.
  --list-models            Print available models for the cluster and
                            exit, without changing anything.
  --restore-default        Put back the openai provider settings from
                            config.yaml.example (undoes a previous run).
USAGE
  exit 2
}

cluster="metis"
model=""
list_only=0
restore_default=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cluster)
      cluster="${2:-}"
      shift 2
      ;;
    --model)
      model="${2:-}"
      shift 2
      ;;
    --list-models)
      list_only=1
      shift
      ;;
    --restore-default)
      restore_default=1
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

if [[ "$cluster" != "sophia" && "$cluster" != "metis" ]]; then
  echo "Error: --cluster must be 'sophia' or 'metis', got '$cluster'" >&2
  exit 2
fi

if [[ "$restore_default" == "1" && ( "$list_only" == "1" || -n "$model" ) ]]; then
  echo "Error: --restore-default can't be combined with --model/--list-models" >&2
  exit 2
fi

if [[ "$restore_default" == "0" && "$list_only" == "0" && -z "$model" ]]; then
  echo "Error: --model NAME is required (or pass --list-models / --restore-default)" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/env.sh"
config_file="$XDG_CONFIG_HOME/goose/config.yaml"

if [[ ! -f "$config_file" ]]; then
  echo "Error: no live config.yaml yet at $config_file -- run setup.sh first" >&2
  exit 1
fi

if [[ "$restore_default" == "1" ]]; then
  python3 "$script_dir/alcf_set_provider.py" restore-default "$config_file" "$script_dir/config.yaml.example"
  exit $?
fi

source "$script_dir/alcf_ensure_token.sh"

if [[ "$cluster" == "metis" ]]; then
  base_url="https://inference-api.alcf.anl.gov/resource_server/metis/api/v1"
else
  base_url="https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1"
fi

if [[ "$list_only" == "1" ]]; then
  echo "Available models on $cluster:"
  curl -sS -X GET "https://inference-api.alcf.anl.gov/resource_server/list-endpoints" -H "Authorization: Bearer ${ALCF_TOKEN}" \
    | "$ALCF_VENV/bin/python" -c "
import sys, json
data = json.load(sys.stdin)
cluster = '$cluster'
framework = 'api' if cluster == 'metis' else 'vllm'
models = data.get('clusters', {}).get(cluster, {}).get('frameworks', {}).get(framework, {}).get('models', [])
for m in models:
    print(f'  {m}')
"
  exit 0
fi

OPENAI_API_KEY_VALUE="$ALCF_TOKEN" python3 "$script_dir/alcf_set_provider.py" set "$config_file" "$base_url" "$model"
echo "Run './alcf.sh --restore-default' to switch back to the internal vllm default."

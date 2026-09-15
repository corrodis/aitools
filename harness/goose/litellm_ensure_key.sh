# Sourced helper (not a standalone script): resolves a litellm.fnal.gov
# API key, preferring in order (1) an already-set $LITELLM_KEY -- a
# caller reusing it across multiple calls in one run, e.g.
# goose-model.sh's list-then-set -- (2) a previously-saved key in
# secrets.yaml (from a prior successful `set`), (3) an interactive
# silent prompt as a last resort. Leaves the result in LITELLM_KEY, or
# exits 1 if none could be obtained.
#
# Requires the caller to have already sourced env.sh (for
# XDG_CONFIG_HOME).

if [[ -z "${LITELLM_KEY:-}" ]]; then
  litellm_secrets_file="$XDG_CONFIG_HOME/goose/secrets.yaml"
  if [[ -f "$litellm_secrets_file" ]]; then
    LITELLM_KEY="$(python3 -c "
import yaml
d = yaml.safe_load(open('$litellm_secrets_file')) or {}
print(d.get('LITELLM_API_KEY', ''))
" 2>/dev/null)"
  fi
  if [[ -n "${LITELLM_KEY:-}" ]]; then
    echo "Using previously-saved litellm.fnal.gov API key." >&2
  fi
  unset litellm_secrets_file
fi

if [[ -z "${LITELLM_KEY:-}" ]]; then
  read -rsp "litellm.fnal.gov API key: " LITELLM_KEY
  echo >&2
fi

if [[ -z "${LITELLM_KEY:-}" ]]; then
  echo "Error: no key entered" >&2
  exit 1
fi

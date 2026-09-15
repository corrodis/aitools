# Sourced helper (not a standalone script): ensures the ALCF auth venv
# and a usable access token exist, printing clear status/guidance either
# way, and sets ALCF_VENV / ALCF_TOKEN for the caller. Used by both
# alcf.sh and goose-model.sh so "check token, guide through auth if
# needed" lives in exactly one place instead of being duplicated.
#
# Requires the caller to have already set `script_dir` to its own
# directory (both alcf.sh and goose-model.sh do this near the top).

alcf_root="/exp/mu2e/app/users/$USER/goose/alcf"
ALCF_VENV="$alcf_root/venv"
alcf_auth_script="$script_dir/alcf_auth.py"
mkdir -p "$alcf_root"

if [[ ! -x "$ALCF_VENV/bin/python" ]]; then
  echo "Setting up ALCF auth environment (one-time)..." >&2
  /usr/bin/python3 -m venv "$ALCF_VENV"
  "$ALCF_VENV/bin/python" -m pip install --quiet globus_sdk
fi

ALCF_TOKEN="$("$ALCF_VENV/bin/python" "$alcf_auth_script" get_access_token 2>/dev/null || true)"
if [[ -z "$ALCF_TOKEN" ]]; then
  echo "No active ALCF token found -- let's get you authenticated." >&2
  "$ALCF_VENV/bin/python" "$alcf_auth_script" authenticate
  ALCF_TOKEN="$("$ALCF_VENV/bin/python" "$alcf_auth_script" get_access_token)"
  if [[ -z "$ALCF_TOKEN" ]]; then
    echo "Error: still no ALCF token after authenticating" >&2
    exit 1
  fi
  echo "ALCF authentication succeeded." >&2
else
  echo "ALCF token is active." >&2
fi

unset alcf_root alcf_auth_script

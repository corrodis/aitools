#!/usr/bin/env bash
# UserPromptSubmit + SessionEnd hook -- upsert a usage summary for the
# current session into the log after every user turn and on clean exit.
#
# Goose passes a JSON payload on stdin with at least:
#   { "event": "UserPromptSubmit"|"SessionEnd", "session_id": "20260915_2" }
#
# For UserPromptSubmit the previous turn's token counts are already flushed
# to SQLite before this hook fires, so no sleep is needed.
#
# For SessionEnd a 1-second sleep is kept as a conservative guard for the
# final flush before the DB connection closes.
#
# Non-fatal: exits 0 on any failure so goose is never blocked.

set -euo pipefail

payload="$(cat)"

session_id="$(printf '%s' "$payload" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('session_id', ''))
" 2>/dev/null)"

event="$(printf '%s' "$payload" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('event', ''))
" 2>/dev/null)"

if [[ -z "$session_id" ]]; then
  echo "log-session.sh: warning: could not parse session_id from hook payload" >&2
  exit 0
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
harness_root="$(cd "$script_dir/../.." && pwd)"
log_script="$harness_root/log-session.py"

if [[ ! -f "$log_script" ]]; then
  echo "log-session.sh: warning: log-session.py not found at $log_script" >&2
  exit 0
fi

# Only sleep on SessionEnd -- the final DB flush may still be in flight.
if [[ "$event" == "SessionEnd" ]]; then
  sleep 1
fi

if ! python3 "$log_script" "$session_id" >/dev/null 2>&1; then
  echo "log-session.sh: warning: log-session.py failed for session $session_id" >&2
fi

exit 0

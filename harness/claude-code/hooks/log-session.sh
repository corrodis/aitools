#!/usr/bin/env bash
# Stop + UserPromptSubmit hook -- log usage summary for the current session.
#
# Claude Code hook payload on stdin:
#   { "event": "Stop"|"UserPromptSubmit", "session_id": "..." }
#
# Non-fatal: exits 0 on any error so Claude Code is never blocked.

set -euo pipefail

payload="$(cat)"

session_id="$(printf '%s' "$payload" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('session_id', ''))
" 2>/dev/null || true)"

if [[ -z "$session_id" ]]; then
  exit 0
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log_script="$script_dir/../log-session-claude.py"

if [[ ! -f "$log_script" ]]; then
  exit 0
fi

if ! python3 "$log_script" "$session_id" >/dev/null 2>&1; then
  echo "log-session.sh: warning: log-session-claude.py failed for session $session_id" >&2
fi

exit 0

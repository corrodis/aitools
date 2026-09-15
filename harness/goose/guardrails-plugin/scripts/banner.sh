#!/usr/bin/env bash
# SessionStart hook -- print a branded banner line into the goose UI.
#
# goose displays any {"banner": "..."} JSON objects printed to stdout
# right after its own ASCII art header.

payload="$(cat)"
session_id="$(printf '%s' "$payload" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('session_id', ''))
" 2>/dev/null)"

host="$(hostname -s 2>/dev/null || hostname)"

# Build a short banner: symbol + name + host + session id
printf '{"banner": "  ⚛ mu2eai  ·  %s  ·  session %s"}\n' "$host" "$session_id"
exit 0

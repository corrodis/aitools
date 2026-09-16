#!/usr/bin/env bash
# PreToolUse hook for Claude Code's Bash tool.
#
# Reads the pending shell command from stdin (Claude Code's hook payload
# JSON) and blocks it if the invoked command matches the blocked list --
# a guard rail against lateral/remote access and privilege escalation.
#
# Claude Code's hook payload for PreToolUse:
#   { "tool_name": "Bash", "tool_input": { "command": "..." }, ... }
#
# Exit codes and stdout control the hook outcome:
#   - Exit 0, stdout empty or non-JSON: tool proceeds normally
#   - Exit 0, stdout contains JSON with "decision":"block": tool is blocked
#     and the "reason" string is shown to the user
#   - Exit 2: hook error (tool is blocked as a safety measure)
#
# This is a heuristic, not a hard sandbox. Claude Code runs shell commands
# with your full user-level Unix permissions; this only blocks the obvious
# path. The same approach as the goose harness's guardrails-plugin.

blocked="ssh scp sftp rsync kinit sudo su"

payload="$(cat)"
command_str="$(printf '%s' "$payload" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('tool_input', {}).get('command', ''))
" 2>/dev/null)"

if [[ -z "$command_str" ]]; then
  exit 0
fi

# Split on shell separators and check the first word of each segment.
# Same logic as goose's block-commands.sh.
segments="$(printf '%s' "$command_str" | tr ';|\n' '\n\n\n' | sed -E 's/&&|\|\|/\n/g')"

while IFS= read -r seg; do
  word="$(printf '%s' "$seg" | sed -E 's/^[[:space:]]*//' | awk '{print $1}')"
  [[ -z "$word" ]] && continue
  base="$(basename -- "$word" 2>/dev/null)"
  for b in $blocked; do
    if [[ "$base" == "$b" ]]; then
      printf '{"decision":"block","reason":"%s is blocked inside Claude Code sessions by this harness (guard rail against lateral/remote access and privilege escalation). Run it from your own shell instead."}' "$b"
      exit 0
    fi
  done
done <<< "$segments"

exit 0

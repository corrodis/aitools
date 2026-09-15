#!/usr/bin/env bash
# PreToolUse hook for developer__shell (see ../hooks/hooks.json). Reads
# the pending shell command from stdin (goose's hook payload JSON) and
# blocks it if the invoked command matches this list -- a guard rail
# against lateral/remote access and privilege escalation, not a hard
# sandbox: goose's shell tool runs with your full user-level Unix
# permissions, so nothing here is a real security boundary, just a check
# on the obvious/likely path.
#
# Matching approach: split the command string on shell separators (;, |,
# &&, ||) and check the first word of each segment (stripped of any path
# prefix via basename), rather than grepping the whole string for these
# names -- catches `/usr/bin/ssh host` and `sudo -i` the same as bare
# `ssh`/`sudo`, without flagging something like `grep su file.txt` where
# the name only appears as an argument. Known gap: a leading env var
# assignment (`FOO=bar ssh host`) shifts the first word and would slip
# through -- this is a heuristic, not a shell parser.
blocked="ssh scp sftp rsync kinit sudo su"

payload="$(cat)"
command_str="$(printf '%s' "$payload" | jq -r '.tool_input.command // empty')"

segments="$(printf '%s' "$command_str" | tr ';|\n' '\n\n\n' | sed -E 's/&&|\|\|/\n/g')"

while IFS= read -r seg; do
  word="$(printf '%s' "$seg" | sed -E 's/^[[:space:]]*//' | awk '{print $1}')"
  [[ -z "$word" ]] && continue
  base="$(basename -- "$word" 2>/dev/null)"
  for b in $blocked; do
    if [[ "$base" == "$b" ]]; then
      printf '{"decision":"block","reason":"%s is blocked inside goose sessions by this harness (guard rail against lateral/remote access and privilege escalation). Run it from your own shell instead."}' "$b"
      exit 0
    fi
  done
done <<< "$segments"

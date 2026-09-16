#!/usr/bin/env bash
# Usage:
#   . setup.sh     (sourced -- also leaves claude on PATH in your shell)
#   ./setup.sh     (executed -- installs only, doesn't touch your shell)
#
# Idempotent: safe to re-run. Installs Claude Code via npm if not already
# present, seeds config files, and syncs MCP servers from the registry.
#
# Deliberately does NOT use `set -e` at top-level: this script is meant to
# be sourceable (`. setup.sh`), and set -e leaking into an interactive shell
# can silently break it on any failing command. Errors are checked explicitly
# instead.

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat >&2 <<'USAGE'
Usage:
  . setup.sh
  ./setup.sh
USAGE
  return 2 2>/dev/null || exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Always source env.sh first -- it defines CLAUDE_HOME, CLAUDE_BIN_DIR, etc.
if ! source "$script_dir/env.sh"; then
  echo "Error: failed to source env.sh" >&2
  unset script_dir
  return 1 2>/dev/null || exit 1
fi

# ---------------------------------------------------------------------------
# 1. Find or install Claude Code
# ---------------------------------------------------------------------------

_find_claude_binary() {
  # Find the real claude binary, not any shell function or PATH wrapper.
  # `command -v` returns the function name (not a path) when a shell function
  # named `claude` is active (env.sh defines one), so we use `type -P` which
  # searches PATH only and never returns function names.
  local bin
  bin="$(type -P claude 2>/dev/null || true)"
  # Skip the harness wrapper (written by a previous setup run) -- we want
  # the real upstream binary so the wrapper can be (re)written pointing at it.
  if [[ -z "$bin" || "$bin" == "$CLAUDE_BIN_DIR/claude" ]]; then
    # Not on PATH, or only the wrapper. Try nvm if available.
    if command -v nvm >/dev/null 2>&1; then
      nvm use --lts >/dev/null 2>&1 || true
    fi
    bin="$(type -P claude 2>/dev/null || true)"
    # If still only the wrapper, resolve what the wrapper itself delegates to.
    [[ "$bin" == "$CLAUDE_BIN_DIR/claude" ]] && bin=""
  fi
  echo "$bin"
}

# ---------------------------------------------------------------------------
# Try to activate nvm if it's available but not yet active.
# Claude Code requires Node.js ≥ 22; nvm is the normal way to manage that
# on these shared gpvm nodes. NVM_DIR is set even when nvm itself hasn't
# been sourced into the current shell, so we check for nvm.sh directly.
# ---------------------------------------------------------------------------
_activate_nvm() {
  # Search for nvm.sh in the common locations. NVM_DIR may not be set in the
  # user's shell even when nvm is installed -- these gpvm nodes store nvm in
  # group storage (/exp/mu2e/app/users/$USER/.nvm), not $HOME/.nvm.
  local nvm_sh=""
  local candidates=(
    "${NVM_DIR:-__unset__}/nvm.sh"
    "/exp/mu2e/app/users/$USER/.nvm/nvm.sh"
    "$HOME/.nvm/nvm.sh"
  )
  for candidate in "${candidates[@]}"; do
    [[ "$candidate" == "__unset__/nvm.sh" ]] && continue
    if [[ -s "$candidate" ]]; then
      nvm_sh="$candidate"
      break
    fi
  done

  if [[ -z "$nvm_sh" ]]; then
    return 1  # nvm not found
  fi

  # shellcheck source=/dev/null
  source "$nvm_sh"

  # Find the highest *installed* node ≥ 22.
  # Strip ANSI colour codes first, then filter to lines that have a real
  # installed version (no "(-> N/A)" marker nvm uses for uninstalled aliases).
  local want_major=22
  local best=""
  best="$(nvm ls 2>/dev/null \
          | sed 's/\x1b\[[0-9;]*m//g' \
          | grep -E '^\s*(->)?\s*v[0-9]' \
          | grep -v 'N/A' \
          | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' \
          | awk -F'[v.]' -v m="$want_major" '$2>=m {print}' \
          | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)"
  if [[ -n "$best" ]]; then
    nvm use "$best" >/dev/null 2>&1 || true
  else
    echo "nvm found but no Node.js ≥ $want_major installed. Installing now..." >&2
    nvm install "$want_major" >/dev/null 2>&1 || true
    nvm use "$want_major" >/dev/null 2>&1 || true
  fi
  return 0
}

_check_node_version() {
  local node_ver
  node_ver="$(node --version 2>/dev/null | sed 's/v//' | cut -d. -f1)"
  [[ "${node_ver:-0}" -ge 22 ]]
}

# Activate nvm upfront so both the version check and npm install use it.
if ! _check_node_version 2>/dev/null; then
  _activate_nvm || true
fi
unset -f _activate_nvm

claude_bin="$(_find_claude_binary)"

if [[ -z "$claude_bin" ]]; then
  echo "Claude Code not found. Installing via npm..."

  # Check node / npm are present and at the right version.
  if ! command -v npm >/dev/null 2>&1; then
    cat >&2 <<'MSG'
Error: npm is not on PATH.

Claude Code requires Node.js ≥ 22. On these gpvm nodes the recommended
approach is nvm (already installed for most users at $NVM_DIR):

  source "$NVM_DIR/nvm.sh"     # activate nvm in this shell
  nvm install 22
  nvm use 22

Then re-run:  . setup.sh
MSG
    unset script_dir claude_bin
    return 1 2>/dev/null || exit 1
  fi

  if ! _check_node_version; then
    node_ver="$(node --version 2>/dev/null || echo unknown)"
    cat >&2 <<MSG
Error: Node.js $node_ver is too old. Claude Code requires Node.js ≥ 22.

Activate a newer version via nvm:

  source "\$NVM_DIR/nvm.sh"
  nvm install 22
  nvm use 22

Then re-run:  . setup.sh
MSG
    unset script_dir claude_bin
    return 1 2>/dev/null || exit 1
  fi

  if ! npm install -g @anthropic-ai/claude-code; then
    cat >&2 <<'MSG'
Error: npm install failed.

If you see EACCES / permission denied on /usr/local/lib/node_modules,
you are using the system-managed node instead of nvm. Activate nvm first:

  source "$NVM_DIR/nvm.sh"
  nvm use 22    # or: nvm install 22 && nvm use 22

Then re-run:  . setup.sh
MSG
    unset script_dir claude_bin
    return 1 2>/dev/null || exit 1
  fi
  claude_bin="$(type -P claude 2>/dev/null || true)"
fi

if [[ -z "$claude_bin" ]]; then
  echo "Error: claude not found after install attempt" >&2
  unset script_dir claude_bin
  return 1 2>/dev/null || exit 1
fi

echo "Claude Code found: $claude_bin"
HOME="$CLAUDE_HOME" "$claude_bin" --version 2>&1 || true

# Write a thin wrapper into CLAUDE_BIN_DIR so `claude` (with HOME override,
# KRB5 scrubbing, umask) is always invoked through env.sh's claude() function
# when users have sourced env.sh. The wrapper itself is only used when the
# real binary isn't directly on PATH -- env.sh's claude() shell function
# takes precedence for sourced shells.
mkdir -p "$CLAUDE_BIN_DIR"
cat > "$CLAUDE_BIN_DIR/claude" <<WRAPPER
#!/usr/bin/env bash
# Thin PATH-level wrapper: delegates to the real claude binary with the
# same HOME/KRB5/umask protections env.sh's claude() function applies,
# so scripts that invoke claude without sourcing env.sh get the same
# treatment. NOT used when env.sh's claude() shell function is active.
export HOME="$CLAUDE_HOME"
export KRB5CCNAME="/tmp/.claude-no-krb5-cache-\$\$"
umask 077
exec "$claude_bin" "\$@"
WRAPPER
chmod 0755 "$CLAUDE_BIN_DIR/claude"

# ---------------------------------------------------------------------------
# 2. Set up CLAUDE_HOME directory structure
# ---------------------------------------------------------------------------

mkdir -p "$CLAUDE_HOME/.claude/backups"

# ---------------------------------------------------------------------------
# 3. Seed settings.json from template if not present
# ---------------------------------------------------------------------------

settings_file="$CLAUDE_HOME/.claude/settings.json"
settings_example="$script_dir/settings.json.example"

if [[ ! -f "$settings_file" && -f "$settings_example" ]]; then
  echo "No Claude Code settings yet -- seeding from harness template: $settings_file"
  # Expand ${CLAUDE_HARNESS_DIR} placeholder to the real hooks directory path.
  hooks_dir="$script_dir/hooks"
  if ! ( umask 077
         sed "s|\${CLAUDE_HARNESS_DIR}|$hooks_dir|g" "$settings_example" > "$settings_file" ); then
    echo "Error: failed to seed $settings_file" >&2
    unset script_dir claude_bin settings_file settings_example hooks_dir
    return 1 2>/dev/null || exit 1
  fi
  unset hooks_dir
fi

# ---------------------------------------------------------------------------
# 4. Symlink CLAUDE.md (shared context) into CLAUDE_HOME
#    Claude Code auto-discovers CLAUDE.md files in the working directory tree
#    and at $HOME/CLAUDE.md. We put it at $HOME so it applies everywhere.
# ---------------------------------------------------------------------------

claude_md_target="$CLAUDE_HOME/CLAUDE.md"
claude_md_source="$(cd "$script_dir/../shared" && pwd)/mu2e.md"

if [[ -f "$claude_md_source" ]]; then
  if [[ ! -e "$claude_md_target" ]] || [[ "$(readlink "$claude_md_target" 2>/dev/null)" != "$claude_md_source" ]]; then
    ln -sfn "$claude_md_source" "$claude_md_target"
    echo "Linked CLAUDE.md → $claude_md_source"
  fi
else
  echo "Warning: shared/mu2e.md not found at $claude_md_source -- CLAUDE.md not linked" >&2
fi

# ---------------------------------------------------------------------------
# 5. API key / backend configuration
# ---------------------------------------------------------------------------

env_file="$CLAUDE_HOME/.env"

_has_backend() {
  # Returns 0 if any usable backend is already configured:
  #   - API key / base URL in env or .env file
  #   - apiKeyHelper in settings.json
  #   - claude.ai OAuth credentials (written by `claude /login`)
  [[ -n "${ANTHROPIC_API_KEY:-}" ]] && return 0
  if [[ -f "$env_file" ]]; then
    grep -q "ANTHROPIC_API_KEY\|ANTHROPIC_BASE_URL" "$env_file" 2>/dev/null && return 0
  fi
  if [[ -f "$settings_file" ]]; then
    python3 -c "
import json, sys
d = json.load(open('$settings_file'))
sys.exit(0 if 'apiKeyHelper' in d else 1)
" 2>/dev/null && return 0
  fi
  # OAuth credentials from `claude /login` live in ~/.claude/ as session
  # tokens managed entirely by Claude Code -- we can't easily inspect them,
  # but their presence means login has happened.
  if find "$CLAUDE_HOME/.claude" -name "*.json" -not -path "*/backups/*" \
       -newer "$CLAUDE_HOME/.claude/settings.json" 2>/dev/null | grep -q .; then
    return 0
  fi
  return 1
}

if ! _has_backend; then
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Claude Code needs a backend. Choose one:"
  echo ""
  echo "  [1] litellm.fnal.gov  (shared Fermilab endpoint; personal API"
  echo "                         key from https://litellm.fnal.gov)"
  echo "  [2] Personal Anthropic API key  (console.anthropic.com)"
  echo "  [3] Skip for now  (API key or claude.ai subscription -- configure"
  echo "                     manually: ./setup-litellm.sh or /login in claude)"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  read -rp "Choice [1/2/3]: " _choice

  case "${_choice:-1}" in
    1)
      bash "$script_dir/setup-litellm.sh"
      ;;
    2)
      echo ""
      echo "Get a key at: https://console.anthropic.com/settings/keys"
      read -rsp "Anthropic API key: " _api_key
      echo ""
      if [[ -z "$_api_key" ]]; then
        echo "No key entered -- skipping. Re-run setup.sh when you have one." >&2
      else
        ( umask 077 && printf 'ANTHROPIC_API_KEY=%s\n' "$_api_key" > "$env_file" )
        echo "Key saved to $env_file"
        export ANTHROPIC_API_KEY="$_api_key"
      fi
      unset _api_key
      ;;
    3|*)
      echo "Skipping backend setup. When ready:"
      echo "  ./setup-litellm.sh          configure litellm.fnal.gov"
      echo "  ./setup.sh                  re-run this prompt"
      echo "  claude, then type /login    sign in with a claude.ai subscription"
      ;;
  esac
  unset _choice
fi
unset -f _has_backend

# ---------------------------------------------------------------------------
# 6. Sync MCP servers from the registry (non-fatal)
# ---------------------------------------------------------------------------

echo "Syncing MCP servers from the registry..."
if ! bash "$script_dir/sync-mcp.sh"; then
  echo "Warning: sync-mcp.sh failed -- Claude Code is installed, but MCP servers weren't synced." \
       "Run ./sync-mcp.sh by hand once the registry is reachable." >&2
fi

echo ""
echo "Done. Run: claude   (or mu2eai-claude)"
echo "Every new shell needs: . env.sh   (or . setup.sh)"

unset script_dir claude_bin settings_file settings_example env_file claude_md_target claude_md_source

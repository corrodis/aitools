# Source this file to use claude from this harness:
#   . env.sh
#
# Kept separate from setup.sh (which installs the binary) so that using
# claude is an explicit, per-shell opt-in rather than a permanent PATH
# change in ~/.bashrc.
#
# Also sourced internally by setup.sh.

env_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

claude_root="/exp/mu2e/app/users/$USER/claude-code"

export CLAUDE_BIN_DIR="$claude_root/bin"
export CLAUDE_HOME="$claude_root/home"

# Add a local bin dir to PATH -- used for the thin `claude` wrapper script
# written by setup.sh so the system nvm-managed binary is invoked through it.
case ":$PATH:" in
  *":$CLAUDE_BIN_DIR:"*) ;;
  *) export PATH="$CLAUDE_BIN_DIR:$PATH" ;;
esac

# Claude Code has no XDG relocation of its own: it writes ~/.claude.json
# and ~/.claude/ using $HOME, and uses $XDG_CACHE_HOME for MCP logs and
# other cache data. Home dirs have tight disk quotas, so redirect both to
# group storage exactly as the goose harness does.
mkdir -p "$CLAUDE_HOME" "$claude_root/cache"
export CLAUDE_FAKE_HOME="$CLAUDE_HOME"
export XDG_CACHE_HOME="$claude_root/cache"

# Shared context file (mu2e.md) -- used by the goose harness via TOM and
# by Claude Code via CLAUDE.md symlink (managed by setup.sh). Exported so
# other scripts can reference the canonical path.
export MU2E_CONTEXT_FILE="$env_script_dir/../shared/mu2e.md"

# LiteLLM endpoint (same server the goose harness uses, different API path).
# Claude Code speaks the *Anthropic* API natively, and litellm.fnal.gov
# exposes an Anthropic-compatible proxy at /anthropic -- so we point
# ANTHROPIC_BASE_URL there and Claude Code needs no other changes.
# Users with a personal Anthropic API key should leave ANTHROPIC_BASE_URL
# unset; setup.sh sets it when the litellm backend is chosen.
#
# ANTHROPIC_BASE_URL is read here only if already in the environment so
# that sourcing env.sh repeatedly is idempotent, but setup.sh writes it
# into a per-user env file ($CLAUDE_HOME/.env) that is sourced below.
_claude_env_file="$CLAUDE_HOME/.env"
if [[ -f "$_claude_env_file" ]]; then
  # shellcheck source=/dev/null
  source "$_claude_env_file"
fi
unset _claude_env_file

# Wrap claude so:
#   1. HOME points at quota-free group storage (keeps ~/.claude.json and
#      ~/.claude/ out of /nashome).
#   2. KRB5CCNAME is redirected to a non-existent path so Claude Code (and
#      anything it spawns) cannot use your Kerberos ticket.  This does NOT
#      call kdestroy -- the real ticket stays valid for your own shell.
#   3. umask 077 ensures any config/key files created are private.
claude() {
  (
    export HOME="$CLAUDE_HOME"
    export KRB5CCNAME="/tmp/.claude-no-krb5-cache-$$"
    umask 077
    command claude "$@"
  )
}

# Convenience alias (same pattern as mu2eai for goose).
mu2eai-claude() {
  claude "$@"
}

# Model picker (analogous to goose-model / mu2eai-model for goose).
# Runs claude-model.sh which queries litellm.fnal.gov live and writes the
# chosen model to CLAUDE_HOME/.env so the next `claude` invocation uses it.
claude-model() {
  "$CLAUDE_HARNESS_DIR/claude-model.sh" "$@"
}

mu2eai-claude-model() {
  claude-model "$@"
}

# Export the harness directory so the shell functions above can find their
# scripts even after env_script_dir is unset (functions are evaluated lazily).
export CLAUDE_HARNESS_DIR="$env_script_dir"
unset env_script_dir

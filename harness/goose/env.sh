# Source this file to use goose from this harness:
#   . env.sh
#
# Kept separate from setup.sh/install.sh (which fetch the binary) so that
# using goose is an explicit, per-shell opt-in rather than a permanent
# PATH change in ~/.bashrc.
#
# Also sourced internally by setup.sh/install.sh/alcf.sh so GOOSE_BIN_DIR
# is defined in exactly one place.

env_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
goose_root="/exp/mu2e/app/users/$USER/goose"

export GOOSE_BIN_DIR="$goose_root/bin"

case ":$PATH:" in
  *":$GOOSE_BIN_DIR:"*) ;;
  *) export PATH="$GOOSE_BIN_DIR:$PATH" ;;
esac

# goose (a Rust/`directories`-crate app) has no config/data-dir override of
# its own -- it just follows XDG. Home dirs here have tight disk quotas
# (this is what sent us down this path -- `goose` failed on first run with
# "Disk quota exceeded" writing to ~/.config/goose), so point the whole
# XDG stack at group storage instead of $HOME. Verified against `goose
# info`: Config dir/yaml <- XDG_CONFIG_HOME, sessions DB <- XDG_DATA_HOME,
# logs dir <- XDG_STATE_HOME (each under a trailing .../goose/). CACHE_HOME
# isn't shown by `goose info` but is set for the same reason.
export XDG_CONFIG_HOME="$goose_root/config"
export XDG_DATA_HOME="$goose_root/data"
export XDG_STATE_HOME="$goose_root/state"
export XDG_CACHE_HOME="$goose_root/cache"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_CACHE_HOME"

# Fake $HOME for goose's own invocation (see goose() below) -- goose's
# plugin/hook discovery is hardcoded to ~/.agents/plugins with no env var
# override (unlike XDG_*), and the real $HOME here is out of disk quota
# even for a single symlink (confirmed: `ln` itself failed with "Disk
# quota exceeded"). Overriding HOME redirects that discovery to group
# storage instead. Side effect: anything goose's shell commands run that
# reads real dotfiles (git, mainly) would see an empty home -- mitigated
# just below by symlinking in the ones that matter.
goose_fake_home="$goose_root/home"
mkdir -p "$goose_fake_home/.agents/plugins"
ln -sfn "$env_script_dir/guardrails-plugin" "$goose_fake_home/.agents/plugins/mu2e-guardrails"
[[ -f "$HOME/.gitconfig" ]] && ln -sfn "$HOME/.gitconfig" "$goose_fake_home/.gitconfig"

unset goose_root

# Wrap goose so anything it writes from here on (config, sessions, a
# provider key added later) is private by construction -- umask applied
# only for the invocation, not the whole shell -- rather than relying on
# remembering to chmod it after the fact.
#
# Also hides your Kerberos ticket from goose. goose does have a native
# SessionStart hook (see guardrails-plugin/ below) -- but a hook is just
# a subprocess whose environment changes don't propagate back to goose's
# own process, so it can't be used to inject KRB5CCNAME for the rest of
# the session; this env-var scoping is still the only place to do that.
# Deliberately NOT a real `kdestroy`: this host gives each login session
# its own ticket cache file (confirmed -- hundreds of distinct
# /tmp/krb5cc_<uid>_XXXXXXXX files, one per login, referenced via a
# per-session KRB5CCNAME), so kdestroy-ing it for real would take out
# your actual interactive shell's ticket (and any same-session panes),
# forcing a re-kinit for anything else needing Kerberos afterward.
# Pointing KRB5CCNAME at a path that doesn't exist, scoped to this
# subshell only, gives goose (and anything it spawns) a clean "no
# credentials cache found" with zero effect outside the invocation.
# Safe for the harness itself either way -- /exp/mu2e/app (bin/config/
# data/state/cache, everything goose touches here) is CephFS with a
# mount-level secret, not Kerberos; only $HOME (/nashome, NFSv4
# sec=krb5) depends on the ticket.
#
# Also blocks a short list of commands (ssh/scp/sftp/rsync/kinit/sudo/su)
# via goose's native PreToolUse hook (guardrails-plugin/, discovered
# through the fake HOME above) -- a guard rail against lateral/remote
# access and privilege escalation, not a hard sandbox: goose's shell tool
# runs with your full user-level Unix permissions, so nothing here is a
# real security boundary against a determined agent, just a check on the
# obvious/likely path. Confirmed this actually blocks /usr/bin/ssh (full
# path, not just bare `ssh`) since the hook matches on the command TEXT,
# not PATH lookup -- unlike an earlier PATH-shadowing attempt here, which
# turned out not to work at all: goose runs shell commands through a
# *login* shell that rebuilds PATH from /etc/profile.d, discarding any
# PATH we set for the invocation. kinit is blocked too so the agent can't
# just re-acquire the Kerberos ticket kdestroy took away from it above.
# Deliberately NOT blocking rm/curl/git/gh -- those are core to normal
# agent work and easy to route around anyway; goose's own Smart Approve
# mode (see `goose configure`) is the right tool if you want
# file-modification confirmation instead.
# OpenTelemetry, auto-detected rather than opt-in via a flag: if
# otel.sh's local Jaeger container (OTLP/HTTP receiver on
# 127.0.0.1:4318) is up, point goose at it for this invocation; if not,
# skip silently and goose runs with no tracing. Confirmed via `strings`
# on the goose binary that OTLP support is real (opentelemetry-otlp is
# linked in) but HTTP-only -- this build has no grpc-tonic -- hence
# http/protobuf rather than the more common grpc default.
# OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT is left off:
# without it, spans/metrics describe calls (timing, token counts,
# tool/provider names) but not prompt/response text -- given goose
# sessions can carry real conversation content (and, since the litellm
# provider was set up, its API key lives in secrets.yaml on this same
# shared host), that content shouldn't leave the machine by default even
# though the collector itself is localhost-only.
# Re-checked on every invocation (cheap: a refused localhost connection
# fails near-instantly, it's only the up-and-listening case that costs
# anything) rather than once at source time, so starting/stopping
# otel.sh mid-session takes effect on the very next goose call.
goose() {
  (
    export HOME="$goose_fake_home"
    export KRB5CCNAME="/tmp/.goose-no-krb5-cache-$$"
    umask 077
    if curl -sS -o /dev/null -m 1 http://127.0.0.1:4318 2>/dev/null; then
      export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
      export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
      export OTEL_SERVICE_NAME=mu2eai-goose
      export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false
    fi
    command goose "$@"
  )
}

# Alias so this harness's goose is invoked as `mu2eai` instead of `goose`.
mu2eai() {
  goose "$@"
}

# Interactive model picker (goose-model.sh) -- just a script invocation,
# not an agent session, so it doesn't need goose()'s HOME/KRB5CCNAME/umask
# wrapping.
goose-model() {
  "$env_script_dir/goose-model.sh" "$@"
}

mu2eai-model() {
  goose-model "$@"
}

# ---------------------------------------------------------------------------
# Shared context injection via TOM (Top Of Mind) extension
#
# goose's TOM extension reads GOOSE_MOIM_MESSAGE_FILE and injects the
# file's content at the top of every turn -- the same text Claude Code
# picks up automatically via CLAUDE.md at $HOME.  One shared file,
# two consumers.
#
# Only set if the file exists and TOM hasn't already been overridden.
# ---------------------------------------------------------------------------
_shared_context="$env_script_dir/../shared/mu2e.md"
if [[ -f "$_shared_context" && -z "${GOOSE_MOIM_MESSAGE_FILE:-}" ]]; then
  export GOOSE_MOIM_MESSAGE_FILE="$(cd "$(dirname "$_shared_context")" && pwd)/$(basename "$_shared_context")"
fi
unset _shared_context

#!/usr/bin/env bash
# Checked before `set -euo pipefail` below -- see install.sh for why.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  echo "Error: run this script, don't source it. Use: ./otel.sh" >&2
  return 1 2>/dev/null || exit 1
fi

set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  ./otel.sh start    Start a local Jaeger all-in-one container: OTLP/HTTP
                      receiver on 127.0.0.1:4318, UI on 127.0.0.1:16686.
                      Idempotent -- safe to call if already running.
  ./otel.sh stop      Stop and remove the container.
  ./otel.sh status    Show whether it's running and whether the OTLP
                      receiver is actually accepting connections.
  ./otel.sh logs      Follow the container's logs.

Local trace viewer for goose sessions. env.sh's goose() wrapper checks
127.0.0.1:4318 on every invocation and, only if something answers,
points OTEL_EXPORTER_OTLP_ENDPOINT at it for that one run -- so this is
opt-in by simply being up or not; goose runs fine with no tracing if
you never start it.

This build of goose only includes the OTLP HTTP transport (see
env.sh/setup-litellm.sh for the same finding on the goose binary
itself), which is why this runs Jaeger's OTLP/HTTP receiver rather than
gRPC.

Storage: badger (embedded) in a container-internal tmpfs (/badger).
Traces are lost when the container is stopped -- that's intentional:
CephFS (where the rest of the harness state lives) does not support
flock() correctly when accessed from inside a rootless podman container,
which is what badger uses for its LOCK file. A tmpfs mount sidesteps
this entirely.

Both ports are bound to 127.0.0.1 only -- Jaeger has no auth on either
the OTLP receiver or the UI, so this must never be exposed beyond
localhost on this host. To view the UI from your laptop, forward it
over SSH rather than opening the port:
  ssh -L 16686:localhost:16686 <this-host>
then browse to http://localhost:16686 there.
USAGE
  exit 2
}

container_name="mu2eai-jaeger"
goose_root="/exp/mu2e/app/users/$USER/goose"
image="docker.io/jaegertracing/all-in-one:latest"

cmd="${1:-}"

case "$cmd" in
  start)
    if podman ps --format '{{.Names}}' | grep -qx "$container_name"; then
      echo "$container_name is already running."
      exit 0
    fi
    # Leftover stopped container from a prior run (e.g. after a reboot) --
    # remove before recreating so `podman run` doesn't fail on the name.
    if podman ps -a --format '{{.Names}}' | grep -qx "$container_name"; then
      podman rm -f "$container_name" >/dev/null
    fi
    # --memory: this is a shared, memory-constrained gpvm node -- keep the
    # local trace viewer from being a noisy neighbor. (Not also capping
    # --cpus: this host's rootless cgroup only has memory/pids
    # controllers available, confirmed via `podman info` -- cpu isn't,
    # and asking for it makes the container fail to start.)
    #
    # --user 0: this rootless podman setup maps only a single UID (see
    # `podman info` idMappings -- size: 1, no subuid range configured),
    # so Jaeger's default non-root image user (10001) can't be
    # represented for the bind-mounted volume below and fails with
    # "insufficient UIDs ... in user namespace". Root inside the
    # container *is* representable (it's the one UID that's mapped, to
    # your own host UID) -- same trick as any single-UID rootless
    # podman setup.
    #
    # --tmpfs /badger: badger (Jaeger's embedded key-value store) uses
    # flock() for its LOCK file. CephFS (where the rest of the harness
    # state lives) does not support flock() correctly when accessed from
    # inside a rootless podman container -- the lock always appears held,
    # so Jaeger fails on startup even with no competing process. A
    # container-internal tmpfs sidesteps this entirely. Trace data is
    # lost when the container stops, which is acceptable for a local
    # debugging aid.
    #
    # --cgroup-manager=cgroupfs: the default systemd cgroup manager
    # requires D-Bus / lingering to be enabled for the invoking user, which
    # is not the case on these shared gpvm nodes. cgroupfs works without it.
    podman run -d \
      --name "$container_name" \
      --user 0 \
      --memory=768m \
      --cgroup-manager=cgroupfs \
      --tmpfs /badger:rw,exec,size=512m \
      -p 127.0.0.1:4318:4318 \
      -p 127.0.0.1:16686:16686 \
      -e SPAN_STORAGE_TYPE=badger \
      -e BADGER_EPHEMERAL=false \
      -e BADGER_DIRECTORY_VALUE=/badger/data \
      -e BADGER_DIRECTORY_KEY=/badger/key \
      "$image" >/dev/null
    echo "Started $container_name."
    echo "UI (on this host): http://127.0.0.1:16686"
    echo "From elsewhere: ssh -L 16686:localhost:16686 $(hostname -f 2>/dev/null || hostname), then http://localhost:16686"
    ;;
  stop)
    if podman rm -f "$container_name" >/dev/null 2>&1; then
      echo "Stopped $container_name."
    else
      echo "$container_name was not running."
    fi
    ;;
  status)
    if podman ps --format '{{.Names}}' | grep -qx "$container_name"; then
      echo "$container_name: running"
    else
      echo "$container_name: not running"
      exit 1
    fi
    if curl -sS -o /dev/null -m 2 -w "OTLP/HTTP receiver (127.0.0.1:4318): reachable (http_code=%{http_code})\n" http://127.0.0.1:4318 2>/dev/null; then
      :
    else
      echo "OTLP/HTTP receiver (127.0.0.1:4318): NOT reachable"
      exit 1
    fi
    ;;
  logs)
    exec podman logs -f "$container_name"
    ;;
  -h|--help|"")
    usage
    ;;
  *)
    echo "Error: unknown argument '$cmd'" >&2
    usage
    ;;
esac

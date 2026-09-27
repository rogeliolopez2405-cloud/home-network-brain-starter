# Shared helpers for bin/* and install.sh. Sourced, not executed.
HNB_HOME="${HNB_HOME:-/opt/hnb}"
HNB_ENV_FILE="${HNB_ENV_FILE:-/etc/hnb/hnb.env}"
HNB_ROOT="${HNB_ROOT:-/var/lib/hnb}"

compose() { docker compose --env-file "$HNB_ENV_FILE" -f "$HNB_HOME/deploy/compose.yaml" "$@"; }

need_root() { [ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0 $*" >&2; exit 1; }; }

hub_running() { [ -n "$(compose ps --status running -q hub 2>/dev/null)" ]; }

# hnb.cli inside the running hub, or a one-off container when it is stopped.
hnb_cli() {
  if hub_running; then compose exec -T hub python -m hnb.cli "$@"
  else compose run --rm --no-deps -T hub "$@"; fi
}

hub_health() {
  local id; id="$(compose ps -q hub 2>/dev/null)"
  [ -n "$id" ] && docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null
}

wait_healthy() {
  local limit="${1:-120}" waited=0 state
  while [ "$waited" -lt "$limit" ]; do
    state="$(hub_health || true)"
    [ "$state" = "healthy" ] && return 0
    sleep 3; waited=$((waited + 3))
  done
  echo "hub state after ${limit}s: ${state:-not running}" >&2
  return 1
}

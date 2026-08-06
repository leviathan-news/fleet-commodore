#!/bin/bash
# One-minute dead-man for the Mini-local helm supervisor. The service-owned
# config contains paths and bounds, not bot credentials. The Fleet runtime
# config remains the sole owner of Telegram/provider secrets.
set -euo pipefail

: "${HELM_CONTROLLER_RUNTIME_CONFIG:=$HOME/.config/fleet-commodore/helm-controller.env}"
if [[ ! -r "$HELM_CONTROLLER_RUNTIME_CONFIG" ]]; then
  echo "$(date -u +%FT%TZ) helm controller config unavailable" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$HELM_CONTROLLER_RUNTIME_CONFIG"
set +a

required=(
  FLEET_COMMODORE_CONFIG
  FLEET_COMMODORE_PYTHON
  HELM_CONTROLLER_DB_FILE
  HELM_CONTROLLER_TOKEN_FILE
  HELM_CONTROLLER_LOCK_FILE
  HELM_CONTROLLER_CRON_STATE_FILE
  HELM_FLEET_RELEASE
  HELM_SUCCESSOR_RELEASE
  COMMODORE_DB_FILE
)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "$(date -u +%FT%TZ) missing helm controller setting: $name" >&2
    exit 1
  fi
done

exec "$FLEET_COMMODORE_PYTHON" "$HELM_SUCCESSOR_RELEASE/helm_supervisor.py" \
  --db "$HELM_CONTROLLER_DB_FILE" \
  --token-file "$HELM_CONTROLLER_TOKEN_FILE" \
  --lock-file "$HELM_CONTROLLER_LOCK_FILE" \
  --config "$FLEET_COMMODORE_CONFIG" \
  --fleet-release "$HELM_FLEET_RELEASE" \
  --successor-release "$HELM_SUCCESSOR_RELEASE" \
  --commodore-db "$COMMODORE_DB_FILE" \
  --sol-ttl "${HELM_SOL_TTL_SECONDS:-2400}" \
  --watcher-ttl "${HELM_WATCHER_TTL_SECONDS:-120}" \
  --bridge-ttl "${HELM_BRIDGE_TTL_SECONDS:-30}" \
  --namespace "${HELM_CONTROLLER_NAMESPACE:-default}" \
  --controller-config "$HELM_CONTROLLER_RUNTIME_CONFIG" \
  --cron-state-file "$HELM_CONTROLLER_CRON_STATE_FILE" \
  reconcile

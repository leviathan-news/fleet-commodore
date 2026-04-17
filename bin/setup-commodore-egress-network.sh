#!/bin/bash
# Create the commodore-egress Docker bridge + start the two long-running
# sidecars (tinyproxy and socat db-tunnel). Idempotent: safe to re-run.
#
# Prereq: bin/build-reviewer-image.sh has built both sidecar images.
#
# After this script, the review container (launched per-review) joins
# --network commodore-egress and reaches:
#   - commodore-egress-proxy:8888 (HTTPS via tinyproxy allowlist)
#   - commodore-db-tunnel:5432    (Postgres via socat forwarder)
# Nothing else on the internet is reachable from the review container.
set -euo pipefail

DOCKER=${DOCKER:-/usr/local/bin/docker}
NETWORK=commodore-egress
PROXY=commodore-egress-proxy
TUNNEL=commodore-db-tunnel

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
if "$DOCKER" network inspect "$NETWORK" >/dev/null 2>&1; then
    echo "network $NETWORK already exists"
else
    echo "creating network $NETWORK"
    "$DOCKER" network create --driver bridge "$NETWORK"
fi

# ---------------------------------------------------------------------------
# Proxy sidecar
# ---------------------------------------------------------------------------
if "$DOCKER" container inspect "$PROXY" >/dev/null 2>&1; then
    if [[ "$("$DOCKER" inspect -f '{{.State.Running}}' "$PROXY")" == "true" ]]; then
        echo "$PROXY already running"
    else
        echo "$PROXY exists but stopped; restarting"
        "$DOCKER" start "$PROXY"
    fi
else
    echo "launching $PROXY"
    "$DOCKER" run -d \
        --name "$PROXY" \
        --network "$NETWORK" \
        --restart unless-stopped \
        --read-only \
        --tmpfs /run:rw,size=2m \
        --tmpfs /var/log/tinyproxy:rw,size=10m \
        commodore-egress-proxy:latest
fi

# ---------------------------------------------------------------------------
# DB tunnel sidecar
# ---------------------------------------------------------------------------
if "$DOCKER" container inspect "$TUNNEL" >/dev/null 2>&1; then
    if [[ "$("$DOCKER" inspect -f '{{.State.Running}}' "$TUNNEL")" == "true" ]]; then
        echo "$TUNNEL already running"
    else
        echo "$TUNNEL exists but stopped; restarting"
        "$DOCKER" start "$TUNNEL"
    fi
else
    if ! "$DOCKER" image inspect commodore-db-tunnel:latest >/dev/null 2>&1; then
        echo "WARN: commodore-db-tunnel:latest not built. Run:"
        echo "  DB_HOST=<host> DB_PORT=25060 ./bin/build-reviewer-image.sh"
        echo "Skipping tunnel launch."
    else
        echo "launching $TUNNEL"
        "$DOCKER" run -d \
            --name "$TUNNEL" \
            --network "$NETWORK" \
            --restart unless-stopped \
            commodore-db-tunnel:latest
    fi
fi

# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
echo
echo "=== sidecar status ==="
"$DOCKER" ps --filter "name=commodore-" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

echo
echo "=== proxy health check (should return 403 for a non-allowlisted host) ==="
"$DOCKER" run --rm --network "$NETWORK" alpine sh -c "
    wget --timeout=5 -q -O /dev/null \
        --proxy=on --https-proxy=http://${PROXY}:8888 \
        https://example.com && echo 'FAIL: example.com should be denied' \
        || echo 'PASS: example.com correctly denied'
" || true

echo
echo "=== proxy allowlist check (should succeed for api.github.com) ==="
"$DOCKER" run --rm --network "$NETWORK" alpine sh -c "
    wget --timeout=5 -q -O /dev/null \
        --proxy=on --https-proxy=http://${PROXY}:8888 \
        https://api.github.com/rate_limit && echo 'PASS: api.github.com allowed' \
        || echo 'FAIL: api.github.com was denied'
" || true

echo
echo "=== tunnel connectivity check ==="
"$DOCKER" run --rm --network "$NETWORK" alpine sh -c "
    nc -vz ${TUNNEL} 5432 2>&1 | head -3
" || true

echo
echo "Setup complete. Reviews can now be launched against --network $NETWORK."

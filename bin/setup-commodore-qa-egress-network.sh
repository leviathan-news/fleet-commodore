#!/bin/bash
# Create the commodore-qa-egress Docker bridge + start the Q&A-specific
# proxy + db-tunnel sidecars. Idempotent: safe to re-run.
#
# Differences from setup-commodore-egress-network.sh:
#   - Distinct network name (commodore-qa-egress)
#   - Distinct proxy image (commodore-qa-egress-proxy:latest, built with qa-filter)
#   - Same db-tunnel image (commodore-db-tunnel:latest), separate container
#   - NO github.com allowlist — Q&A reads docs/dev-journal corpus locally
set -euo pipefail

DOCKER=${DOCKER:-/usr/local/bin/docker}
NETWORK=commodore-qa-egress
PROXY=commodore-qa-egress-proxy
TUNNEL=commodore-qa-db-tunnel

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
# Proxy sidecar (Q&A-specific image)
# ---------------------------------------------------------------------------
if "$DOCKER" container inspect "$PROXY" >/dev/null 2>&1; then
    if [[ "$("$DOCKER" inspect -f '{{.State.Running}}' "$PROXY")" == "true" ]]; then
        echo "$PROXY already running"
    else
        echo "$PROXY exists but stopped; restarting"
        "$DOCKER" start "$PROXY"
    fi
else
    if ! "$DOCKER" image inspect commodore-qa-egress-proxy:latest >/dev/null 2>&1; then
        echo "ERROR: commodore-qa-egress-proxy:latest not built."
        echo "Build it from egress/qa-filter via build-reviewer-image.sh's qa-proxy target."
        exit 1
    fi
    echo "launching $PROXY"
    "$DOCKER" run -d \
        --name "$PROXY" \
        --network "$NETWORK" \
        --restart unless-stopped \
        --read-only \
        --tmpfs /run:rw,size=2m \
        --tmpfs /var/log/tinyproxy:rw,size=10m \
        commodore-qa-egress-proxy:latest
fi

# ---------------------------------------------------------------------------
# DB tunnel sidecar (same image as the build/review network, separate name)
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
        echo "WARN: commodore-db-tunnel:latest not built."
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
# Smoke test — confirm GitHub is denied (the whole point)
# ---------------------------------------------------------------------------
echo
echo "=== Q&A egress sidecars ==="
"$DOCKER" ps --filter "name=commodore-qa-" --format 'table {{.Names}}\t{{.Status}}'

echo
echo "=== GitHub deny check (should FAIL with 403 — that's the intent) ==="
"$DOCKER" run --rm --network "$NETWORK" alpine sh -c "
    wget --timeout=5 -q -O /dev/null \
        --proxy=on --https-proxy=http://${PROXY}:8888 \
        https://api.github.com/rate_limit && echo 'FAIL: api.github.com was allowed (Q&A leak!)' \
        || echo 'PASS: api.github.com denied'
" || true

echo
echo "=== Anthropic allow check ==="
"$DOCKER" run --rm --network "$NETWORK" alpine sh -c "
    wget --timeout=5 -q -O /dev/null \
        --proxy=on --https-proxy=http://${PROXY}:8888 \
        https://api.anthropic.com/ && echo 'PASS: api.anthropic.com allowed' \
        || echo 'FAIL: api.anthropic.com denied'
" || true

echo
echo "Setup complete. Q&A containers should run on --network $NETWORK."

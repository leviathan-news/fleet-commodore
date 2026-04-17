#!/bin/bash
# Build the three Commodore images: reviewer, egress-proxy, db-tunnel.
#
# One-shot operator script. Invoke on the Mini from the fleet-commodore
# repo root:
#
#   cd ~/dev/leviathan/fleet-commodore
#   DB_HOST=<your-do-host> DB_PORT=25060 ./bin/build-reviewer-image.sh
#
# DB_HOST is required for db-tunnel. If omitted, db-tunnel skip is logged
# and the script continues building reviewer + egress-proxy. You can rerun
# this later with DB_HOST set to build just the tunnel.
set -euo pipefail

cd "$(dirname "$0")/.."  # fleet-commodore repo root
REPO_ROOT=$(pwd)

DOCKER=${DOCKER:-/usr/local/bin/docker}
GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")

echo "=== Build context ==="
echo "  repo:     $REPO_ROOT"
echo "  git sha:  $GIT_SHA"
echo "  docker:   $($DOCKER --version)"
echo

# ---------------------------------------------------------------------------
# Vendor squid-bot requirements for Django ORM inside the reviewer image.
# commodore-orm needs Django + models; pin them to what prod runs by copying
# from the local squid-bot checkout.
# ---------------------------------------------------------------------------
SQUID_BOT_REQS="$HOME/dev/leviathan/squid-bot/requirements.txt"
if [[ -f "$SQUID_BOT_REQS" ]]; then
    echo "Vendoring squid-bot requirements from $SQUID_BOT_REQS"
    cp "$SQUID_BOT_REQS" "$REPO_ROOT/requirements-squid-bot.txt"
else
    echo "WARN: $SQUID_BOT_REQS not found; commodore-orm will fail at runtime"
    : > "$REPO_ROOT/requirements-squid-bot.txt"
fi

# ---------------------------------------------------------------------------
# Image 1: commodore-reviewer (the main review-worker image, ~500 MB)
# ---------------------------------------------------------------------------
echo
echo "=== Build 1/3: commodore-reviewer ==="
"$DOCKER" build \
    -f "$REPO_ROOT/reviewer.Dockerfile" \
    -t "commodore-reviewer:${GIT_SHA}" \
    -t "commodore-reviewer:latest" \
    "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Image 2: commodore-egress-proxy (tinyproxy HTTPS allowlist, ~30 MB)
# ---------------------------------------------------------------------------
echo
echo "=== Build 2/3: commodore-egress-proxy ==="
"$DOCKER" build \
    -f "$REPO_ROOT/egress-proxy.Dockerfile" \
    -t "commodore-egress-proxy:${GIT_SHA}" \
    -t "commodore-egress-proxy:latest" \
    "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Image 3: commodore-db-tunnel (socat, ~15 MB) — needs DB_HOST at build time.
# ---------------------------------------------------------------------------
echo
echo "=== Build 3/3: commodore-db-tunnel ==="
if [[ -z "${DB_HOST:-}" ]]; then
    echo "WARN: DB_HOST not set; skipping db-tunnel build."
    echo "      Re-run with DB_HOST=<host> DB_PORT=<port> ./bin/build-reviewer-image.sh"
else
    "$DOCKER" build \
        --build-arg "DB_HOST=$DB_HOST" \
        --build-arg "DB_PORT=${DB_PORT:-25060}" \
        -f "$REPO_ROOT/db-tunnel.Dockerfile" \
        -t "commodore-db-tunnel:${GIT_SHA}" \
        -t "commodore-db-tunnel:latest" \
        "$REPO_ROOT"
fi

# ---------------------------------------------------------------------------
# Smoke test: reviewer --version should exit 0 with all components present.
# ---------------------------------------------------------------------------
echo
echo "=== Smoke test: commodore-reviewer --version ==="
if "$DOCKER" run --rm commodore-reviewer:latest --version; then
    echo "PASS"
else
    echo "FAIL: reviewer --version reported missing components. See output above."
    exit 1
fi

echo
echo "=== Build complete ==="
"$DOCKER" images --filter=reference="commodore-*" --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}'

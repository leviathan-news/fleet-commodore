# Egress HTTPS allowlist sidecar for the review container.
#
# tinyproxy with FilterDefaultDeny enforced. Only *.github.com and
# *.anthropic.com domains can be reached through the proxy; everything else
# returns 403. This sidecar, combined with the review container being on
# --network commodore-egress (no direct internet), prevents a hijacked
# review-Claude from exfiltrating the GH PAT or DB URL to an arbitrary host.
#
# Postgres DOES NOT go through this proxy — libpq has no HTTP_PROXY support.
# The db-tunnel sidecar handles Postgres separately.
#
# Lifecycle: one long-running container started by
# bin/setup-commodore-egress-network.sh, --restart unless-stopped.

FROM alpine:3.20

RUN apk add --no-cache tinyproxy ca-certificates

COPY egress/tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY egress/filter         /etc/tinyproxy/filter

# tinyproxy writes to /var/log/tinyproxy/; mount a tmpfs there at runtime.
# For image smoke tests we allow it to write to the image's /var/log.
RUN mkdir -p /var/log/tinyproxy \
 && chown -R tinyproxy:tinyproxy /var/log/tinyproxy

EXPOSE 8888

# Run in foreground so Docker sees the process + can restart it.
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]

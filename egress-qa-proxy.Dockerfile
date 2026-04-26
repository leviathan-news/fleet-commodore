# Egress HTTPS allowlist sidecar for the Q&A container.
#
# Same shape as egress-proxy.Dockerfile, but uses egress/qa-filter — which
# DOES NOT include *.github.com. Q&A has no business reaching GitHub at
# runtime: the docs/dev-journal corpus is mounted read-only, and the
# read-only Postgres reader role is reachable via the db-tunnel sidecar.
#
# This sidecar runs on commodore-qa-egress (a separate Docker bridge from
# commodore-egress), set up by bin/setup-commodore-qa-egress-network.sh.

FROM alpine:3.20

RUN apk add --no-cache tinyproxy ca-certificates

COPY egress/tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY egress/qa-filter      /etc/tinyproxy/filter

RUN mkdir -p /var/log/tinyproxy \
 && chown -R tinyproxy:tinyproxy /var/log/tinyproxy

EXPOSE 8888

CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]

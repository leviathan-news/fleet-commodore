# Postgres TCP-tunnel sidecar for the review container.
#
# Plain TCP forwarder: listens on :5432 and forwards every connection to a
# hardcoded DB_HOST:DB_PORT. The destination is baked in at BUILD time
# (--build-arg DB_HOST=..., --build-arg DB_PORT=...); the runtime container
# cannot be told to forward elsewhere.
#
# Why this exists: libpq / psycopg2 do NOT honour HTTP_PROXY. Sending
# Postgres traffic through the tinyproxy sidecar doesn't work at the client
# layer — psycopg just opens a TCP socket to whatever host you give it.
# So we give it "commodore-db-tunnel:5432" and socat does the last hop to
# the real DO host. Postgres TLS (sslmode=require) still verifies end-to-end,
# so a tampered sidecar can't MITM.
#
# Rebuild whenever DigitalOcean rotates the DB host.

FROM alpine:3.20

RUN apk add --no-cache socat ca-certificates

ARG DB_HOST
ARG DB_PORT=25060

# Ensure the build-arg was actually provided (empty → fail fast).
RUN test -n "${DB_HOST}" || (echo "ERROR: DB_HOST build-arg is required" >&2 && exit 1)

ENV DB_HOST=${DB_HOST} DB_PORT=${DB_PORT}

EXPOSE 5432

# exec form so socat receives SIGTERM cleanly on docker stop.
CMD ["sh", "-c", "exec socat -dd TCP-LISTEN:5432,fork,reuseaddr TCP:${DB_HOST}:${DB_PORT}"]

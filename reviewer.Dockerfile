# Fleet Commodore review-worker image.
#
# Runs a single PR review as a disposable container (--rm), reading a job dict
# from stdin and emitting findings JSON on stdout. The launcher helper
# (bin/launch-review-container) invokes this image via `docker run`.
#
# Isolation posture:
#   - Read-only rootfs at runtime.
#   - /home/reviewer mounted as tmpfs for gh/claude XDG config + cache.
#   - /tmp mounted as tmpfs for scratch.
#   - Non-root user reviewer (uid 1000).
#   - --network commodore-egress → only reachable hosts are the sidecars
#     (commodore-egress-proxy for HTTPS, commodore-db-tunnel for Postgres).
#
# Build: bin/build-reviewer-image.sh

FROM python:3.11-slim

# --- System packages --------------------------------------------------------
# bash: Claude CLI's Bash(...) tool invokes /bin/sh; python-slim's default
#       dash lacks features gh occasionally wants. /bin/bash keeps both happy.
# git:  gh pr diff / gh pr view shell out to git for repo metadata + auth
#       plumbing. Without git, gh fails with opaque errors on certain paths.
# ca-certificates, curl, openssl: standard HTTPS hygiene for gh + claude + pip.
# procps: ps/kill/etc — cheap insurance when Claude's Bash runs ps.
# gnupg, lsb-release: required to add the GitHub CLI apt repo.
# nodejs, npm: Claude CLI + Codex CLI are Node global npm packages.
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
        bash git ca-certificates curl openssl procps gnupg lsb-release \
 && install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends gh nodejs \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# --- LLM CLIs (pin to specific versions for reproducibility) ---------------
# Claude Code CLI and Codex CLI are both npm global packages. Pin to the
# versions that match what the host Mini currently runs so behaviour in
# container matches interactive debugging. Bump deliberately when host rotates.
RUN npm install -g \
        @anthropic-ai/claude-code \
        @openai/codex \
 && npm cache clean --force

# --- Python deps -----------------------------------------------------------
# sqlparse: read-only SQL statement validator for commodore-db wrapper.
# psycopg2-binary: Postgres driver for both commodore-db and Django ORM.
# requests: HTTP client for review_worker.py's calls to GitHub / LN API.
# The Django + squid-bot deps are installed in a separate layer below so
# rebuilds on commodore-only changes stay fast.
COPY requirements-reviewer.txt /tmp/requirements-reviewer.txt
RUN pip install --no-cache-dir -r /tmp/requirements-reviewer.txt \
 && rm /tmp/requirements-reviewer.txt

# --- Django + squid-bot env (for commodore-orm wrapper) --------------------
# Expect requirements-squid-bot.txt to be vendored from squid-bot at build
# time — bin/build-reviewer-image.sh copies it in before `docker build`.
# If the file is absent, the image still builds but commodore-orm will fail
# at runtime with "Django not installed" rather than silently.
COPY requirements-squid-bot.txt /tmp/requirements-squid-bot.txt
RUN if [ -s /tmp/requirements-squid-bot.txt ]; then \
        pip install --no-cache-dir -r /tmp/requirements-squid-bot.txt ; \
    fi \
 && rm /tmp/requirements-squid-bot.txt

# --- Non-root user + writable XDG paths (via tmpfs at runtime) -------------
# HOME=/home/reviewer is where gh + claude + codex read auth and write cache.
# At runtime, the launcher bind-mounts host-side pre-populated auth state
# READ-ONLY into /home/reviewer/.claude (the Claude OAuth credentials) and
# /home/reviewer/.claude.json (Claude CLI settings). This matches the Jenbot
# pattern (rezscore) which solved the same "can't use an API key, have to
# smuggle in the OAuth state" problem empirically on this Mini.
#
# The rest of HOME (and /tmp) is tmpfs so gh/claude/codex can write their
# caches somewhere; XDG_* paths keep those writes inside HOME so the
# --read-only rootfs stays read-only.
RUN useradd --create-home --uid 1000 --shell /bin/bash reviewer \
 && mkdir -p /app \
 && chown -R reviewer:reviewer /app

ENV HOME=/home/reviewer \
    XDG_CONFIG_HOME=/home/reviewer/.config \
    XDG_CACHE_HOME=/home/reviewer/.cache \
    XDG_DATA_HOME=/home/reviewer/.local/share \
    GIT_CONFIG_GLOBAL=/home/reviewer/.gitconfig \
    PYTHONUNBUFFERED=1

WORKDIR /app

# --- App payload -----------------------------------------------------------
COPY --chown=reviewer:reviewer review_worker.py /app/review_worker.py
COPY --chown=reviewer:reviewer qa_worker.py /app/qa_worker.py
COPY --chown=reviewer:reviewer bin/commodore-db /app/bin/commodore-db
COPY --chown=reviewer:reviewer bin/commodore-orm /app/bin/commodore-orm
RUN chmod +x /app/bin/commodore-db /app/bin/commodore-orm

# Make the wrappers invokable via simple names from Claude CLI's Bash tool.
ENV PATH="/app/bin:${PATH}"

USER reviewer

# ENTRYPOINT is the review worker. Version-check mode short-circuits so
# `docker run --rm commodore-reviewer:latest --version` is a cheap smoke test
# during deploy.
ENTRYPOINT ["python3", "-u", "/app/review_worker.py"]

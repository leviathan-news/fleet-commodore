FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg \
    && install -d -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Claude CLI + Codex CLI installs are provider-specific and vary by upstream.
# Install them into the image per the vendor docs at build time, OR mount host
# binaries read-only via docker-compose. Left un-scripted here to avoid pinning
# to an upstream URL that may rotate.

RUN useradd -m -u 1000 commodore
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY commodore.py .

USER commodore
RUN mkdir -p /workspace

ENV WORKSPACE_DIR=/workspace
CMD ["python", "commodore.py"]

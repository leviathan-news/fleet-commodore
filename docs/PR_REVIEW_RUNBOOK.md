# Fleet Commodore — PR Review Runbook

Operator-facing runbook for the PR-review feature. Covers first-time setup,
rotations, image rebuilds, troubleshooting, and shutdown.

## Architecture recap

The PR-review feature is a **three-container** stack on the Mac Mini's Docker
Desktop, orchestrated by the Commodore chat daemon (which itself runs in
`tmux`, NOT in a container):

```
┌────────────────────────────────────────────────────────────────────┐
│ Mac Mini host                                                      │
│  tmux session leviathan / window 5 / commodore.py (chat daemon)    │
│    ├── (polls Telegram for "review PR N" intents)                  │
│    └── coordinator thread → bin/launch-review-container <uuid>     │
│            │                                                        │
│            └── docker run --rm                                      │
│                 commodore-reviewer:latest ─────────┐                │
│                           ↓                        │                │
│                    commodore-egress (bridge)       │                │
│                   ┌────────┴────────┐              │                │
│                   ↓                 ↓              │                │
│        commodore-egress-proxy  commodore-db-tunnel ┘                │
│          (tinyproxy, HTTPS)     (socat, Postgres)                   │
│              ↓                       ↓                              │
│        api.github.com,         leviathan-prod-...                   │
│        api.anthropic.com       .db.ondigitalocean.com:25060         │
└────────────────────────────────────────────────────────────────────┘
```

**Isolation boundaries:**

| Process / container | Sees GH PAT | Sees DB URL | Sees BOT_TOKEN | Has internet |
|---|---|---|---|---|
| `commodore.py` (chat daemon) | No | No | Yes (Telegram) | Direct (host network) |
| coordinator thread | No | No | Yes (inherited) | Direct (it's in-proc) |
| `bin/launch-review-container` | **Yes** (reads files) | **Yes** (reads files) | No | Direct |
| `commodore-reviewer` container | Yes (env) | Yes (env) | No | **Proxy + tunnel only** |
| `commodore-egress-proxy` | No | No | No | Allowlist (github/anthropic) |
| `commodore-db-tunnel` | No | No (just a forwarder) | No | Direct to DO DB host |

---

## First-time setup

Run in order. Each step is idempotent — safe to re-run if you need to
restart the setup.

### 1. GitHub identity

Either:

**(a) Use the `leviathan-commodore` dedicated account.** See the shipping
dev journal (`squid-bot:dev-journal/entries/ai-agents/2026-04-17-fleet-commodore-shipping.md`)
for account creation notes. Fine-grained PAT scopes:

- Resource owner: `leviathan-news` org
- Repository access: `squid-bot`, `auction-ui`, `be-benthic`, `agent-chat`, `fleet-commodore`
- Permissions: Contents **Read-only**, Metadata **Read-only**, Pull requests **Read-only**. Everything else No access.

**(b) Fallback**: a fine-grained PAT on CurveCap's own account, same scope.
Less clean but functionally identical. Rotate when `leviathan-commodore` is
unflagged.

Save the PAT to the Mini:

```bash
ssh mini
mkdir -p ~/.config/commodore && chmod 700 ~/.config/commodore
cat > ~/.config/commodore/gh_pat
# paste the github_pat_... token, then Ctrl+D
chmod 600 ~/.config/commodore/gh_pat
# Smoke-test:
GH_TOKEN=$(cat ~/.config/commodore/gh_pat) gh api user --jq .login
# Should print: leviathan-commodore  (or your fallback account)
GH_TOKEN=$(cat ~/.config/commodore/gh_pat) gh api repos/leviathan-news/squid-bot --jq .full_name
# Should print: leviathan-news/squid-bot  (403 = org approval still pending)
```

### 2. PostgreSQL read-only role

Connect to the DigitalOcean managed Postgres cluster as `doadmin` (or
equivalent):

```sql
-- Create the reader with a strong password (save in password manager).
CREATE ROLE commodore_reader WITH LOGIN PASSWORD '<generated>';

-- Session-level read-only + statement timeout enforcement.
ALTER ROLE commodore_reader SET default_transaction_read_only = on;
ALTER ROLE commodore_reader SET statement_timeout = '3000ms';
ALTER ROLE commodore_reader SET idle_in_transaction_session_timeout = '10s';

-- Grants — SELECT on everything public.
GRANT CONNECT ON DATABASE leviathan_prod TO commodore_reader;
GRANT USAGE ON SCHEMA public TO commodore_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO commodore_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO commodore_reader;

-- Denylist (operator choice per plan).
REVOKE SELECT (password) ON auth_user FROM commodore_reader;
REVOKE SELECT ON django_session FROM commodore_reader;

-- Verify: as commodore_reader, INSERT must fail.
SET ROLE commodore_reader;
INSERT INTO lnn_news (id) VALUES (999999999);  -- expect: permission denied
RESET ROLE;
```

Save the connection URL on the Mini. **The host in this URL is
`commodore-db-tunnel`, NOT the real DO host** — the sidecar handles the
last hop.

```bash
ssh mini
cat > ~/.config/commodore/db_url
# Paste (fill in <password>):
# postgres://commodore_reader:<password>@commodore-db-tunnel:5432/leviathan_prod?sslmode=require
chmod 600 ~/.config/commodore/db_url
```

Also record the **real** DO host + port separately — you'll need them as
build-args for the tunnel image:

```bash
DO_DB_HOST='leviathan-prod-do-user-XXXXX-0.k.db.ondigitalocean.com'
DO_DB_PORT=25060
echo "DB_HOST=$DO_DB_HOST" > ~/.config/commodore/db-build-args
echo "DB_PORT=$DO_DB_PORT" >> ~/.config/commodore/db-build-args
chmod 600 ~/.config/commodore/db-build-args
```

### 3. DO firewall trust

DigitalOcean control panel → Databases → your cluster → Settings → **Trusted
sources** → add the Mini's public IP. To find it:

```bash
ssh mini 'curl -s https://api.ipify.org; echo'
```

Name the entry "Mac Mini (Commodore reviewer)". Update this entry whenever
the Mini's IP rotates — the tunnel sidecar will start returning connection
refused when that happens.

### 4. Stage the Claude OAuth state

Claude Code CLI uses OAuth, not an API key. The review container inherits
the Mini's login state via a staged copy. This is the **Jenbot pattern** —
verified to work on this Mini.

```bash
ssh mini
mkdir -p ~/.config/commodore/claude-auth
# Copy the Mini's Claude state into the Commodore's staging dir.
cp -R ~/.claude/. ~/.config/commodore/claude-auth/
cp ~/.claude.json ~/.config/commodore/dot-claude.json
chmod -R go-rwx ~/.config/commodore/claude-auth ~/.config/commodore/dot-claude.json
# Verify the credentials file came across:
ls -la ~/.config/commodore/claude-auth/.credentials.json
```

Re-run steps 4 whenever you re-authenticate Claude Code on the Mini itself
(every ~30 days per OAuth rotation). The launcher copies this into a
per-review tempdir, so there's no risk of a container mutating the staged
state.

### 5. Build the three images

```bash
ssh mini
cd ~/dev/leviathan/fleet-commodore
source ~/.config/commodore/db-build-args
DB_HOST=$DB_HOST DB_PORT=$DB_PORT ./bin/build-reviewer-image.sh
```

Expect ~3 minutes on first build (pulls python:3.11-slim + installs gh +
claude-code + codex via npm + vendored Django deps + alpine for sidecars).
Subsequent builds are ~30s because layer caching holds.

Smoke test is automatic at the end of the script:
`docker run --rm commodore-reviewer:latest --version` — must exit 0 with
`all_components_present: true`.

### 6. Start the egress network + sidecars

```bash
ssh mini
cd ~/dev/leviathan/fleet-commodore
./bin/setup-commodore-egress-network.sh
```

This creates the `commodore-egress` bridge + launches the two long-running
sidecars (`commodore-egress-proxy`, `commodore-db-tunnel`) with
`--restart unless-stopped`. Idempotent: re-run safely any time.

Expected output: both containers show `Up` in `docker ps`, the proxy denies
`https://example.com` with a 403, allows `https://api.github.com/rate_limit`,
and the tunnel accepts a `nc -vz commodore-db-tunnel 5432` probe.

### 7. Pull latest Commodore code + restart its tmux window

```bash
ssh mini
cd ~/dev/leviathan/fleet-commodore
git pull --ff-only
/opt/homebrew/bin/tmux kill-window -t leviathan:commodore
# Watchdog respawns within 5 minutes. If you don't want to wait:
/opt/homebrew/bin/tmux new-window -t leviathan -n commodore \
    "cd ~/dev/leviathan/fleet-commodore && ./run.sh"
```

### 8. First review smoke test

From CurveCap's Telegram in Bot HQ or Lev Dev:

```
@leviathan_commodore_bot /review leviathan-news/fleet-commodore 1
```

Expected: acknowledgement message within ~5 seconds ("Very well ... stand
by for the formal assessment"). During the review, `docker ps` on the Mini
shows THREE commodore-* containers. Review posts back as a threaded reply
within ~2 minutes.

If it doesn't post a review within 10 minutes, see Troubleshooting below.

---

## Rotations and periodic maintenance

### Monthly: rotate the GH PAT

PATs expire. When the current one gets within 2 weeks of expiry:

1. Generate new fine-grained PAT on `leviathan-commodore` with the same
   scope as step 1 above.
2. Save to `~/.config/commodore/gh_pat` (overwriting the old one).
3. No restart required — the launcher re-reads the file each review.
4. Delete the old PAT from GitHub's settings page.

### ~Monthly: refresh Claude OAuth state

OAuth tokens refresh themselves for a while, but eventually expire. When
reviews start returning "claude auth failed" errors:

1. `ssh mini` and run `claude --help` once interactively — Claude Code
   refreshes its OAuth if needed, or prompts for login.
2. Re-run step 4 above to re-stage the refreshed auth into the
   Commodore's claude-auth directory.

### After squid-bot schema changes (Django migrations land)

The `commodore-orm` wrapper has Django pinned to a snapshot of
`squid-bot/requirements.txt` at image build time. When prod runs a
migration that adds new columns or changes models:

```bash
ssh mini
cd ~/dev/leviathan/fleet-commodore
git pull  # in case anything upstream changed
DB_HOST=... DB_PORT=... ./bin/build-reviewer-image.sh
# No need to restart sidecars — they're unchanged.
```

### When DigitalOcean rotates the DB host

The tunnel sidecar has the host baked in. When DO rotates (rare, but it
happens):

1. Update `~/.config/commodore/db-build-args` with the new host.
2. Rebuild the tunnel image only:
   ```bash
   cd ~/dev/leviathan/fleet-commodore
   source ~/.config/commodore/db-build-args
   docker build \
       --build-arg DB_HOST=$DB_HOST \
       --build-arg DB_PORT=$DB_PORT \
       -f db-tunnel.Dockerfile \
       -t commodore-db-tunnel:latest .
   ```
3. Recreate the running container:
   ```bash
   docker rm -f commodore-db-tunnel
   ./bin/setup-commodore-egress-network.sh
   ```

No reviewer-image rebuild is needed for this — the tunnel is independent.

---

## Troubleshooting

### Review never posts

Check in order:

1. **`docker ps --filter name=commodore-review-`** during the review window.
   If you see no container, the launcher never fired: check
   `~/dev/leviathan/fleet-commodore/logs/commodore.log` for errors like
   "missing_credentials" or "docker: command not found".
2. **`docker ps --filter name=commodore-egress`**. Both
   `commodore-egress-proxy` and `commodore-db-tunnel` must show Up. If
   either is missing, re-run `./bin/setup-commodore-egress-network.sh`.
3. **Per-review stderr log** at
   `~/dev/leviathan/fleet-commodore/logs/reviews/<uuid>.stderr`.
   Contains Claude CLI + gh output from inside the container — usually
   has the real error message (auth failure, PR not found, etc.).
4. **SQLite pr_review row**:
   ```bash
   sqlite3 ~/dev/leviathan/fleet-commodore/commodore.db \
       'SELECT id, status, repo, pr_number, verdict, error, created_at FROM pr_review ORDER BY id DESC LIMIT 5'
   ```
   If `status=orphaned` with an error about "container not found on boot",
   the Commodore process restarted mid-review; the row was abandoned and
   an apology was posted. The user should re-issue the command.

### "handshake failed" when relay receipts aren't landing

The Commodore's JWT (`LN_API_TOKEN` in its `.env`) expires periodically
(~24 hours). When the poll-loop logs show `401 Unauthorized` on
`/agent-chat/post/`, re-run the handshake:

```bash
ssh mini
cd ~/dev/leviathan/fleet-commodore
# The handshake script lives in scripts/ or inline. If not yet committed,
# regenerate via the pattern in 2026-04-17-fleet-commodore-shipping.md.
# Afterwards, update .env with the new LN_API_TOKEN and restart.
```

### Review containers accumulate

`--rm` should auto-clean. If you see `commodore-review-<uuid>` containers
lingering (Exited), something crashed the launcher before its finally
block:

```bash
docker ps -a --filter name=commodore-review- --filter status=exited
docker rm -f $(docker ps -aq --filter name=commodore-review-)
```

Then check `logs/commodore.log` + `logs/reviews/*.stderr` for what
crashed the launcher.

### Proxy returns 403 on a host that should be allowed

The allowlist in `egress/filter` is the source of truth. Edit, rebuild,
restart:

```bash
ssh mini
cd ~/dev/leviathan/fleet-commodore
vim egress/filter     # add the new host pattern
docker build -f egress-proxy.Dockerfile -t commodore-egress-proxy:latest .
docker rm -f commodore-egress-proxy
./bin/setup-commodore-egress-network.sh
```

### PostgreSQL connection refused

Almost always one of two things:

1. **Mini's IP rotated**. Check `curl -s https://api.ipify.org` and
   compare with DO's Trusted Sources list. Update if needed.
2. **DO rotated the DB host**. See "When DigitalOcean rotates the DB host"
   above.

Confirm with a direct probe:

```bash
ssh mini
docker run --rm --network commodore-egress alpine nc -vz commodore-db-tunnel 5432
# Should say: open
```

If `nc` says closed but the tunnel container is `Up`, the tunnel itself is
running but its destination isn't reachable (DO firewall or host rotation).

---

## Shutdown

### Stop reviews without stopping the Commodore

```bash
ssh mini
docker stop commodore-egress-proxy commodore-db-tunnel
# Review preflight will now decline with "signal-relay inoperable" in-character.
# Chat + mentions still work; only the review path is blocked.
```

### Full shutdown

```bash
ssh mini
# Stop any in-flight review first.
docker ps --filter name=commodore-review- -q | xargs -r docker stop
# Stop sidecars.
docker stop commodore-egress-proxy commodore-db-tunnel
# Stop the chat daemon.
/opt/homebrew/bin/tmux kill-window -t leviathan:commodore
# Disable the watchdog so it doesn't respawn.
crontab -l | grep -v 'fleet-commodore/cron/watchdog.sh' | crontab -
```

To restart: re-add the watchdog entry, start sidecars via
`setup-commodore-egress-network.sh`, and let the watchdog respawn the
tmux window within 5 minutes.

---

## Secret inventory

All secrets live at `~/.config/commodore/` on the Mini (mode 0700 dir).
Each file is owner-only (0600). None are in git.

| File | What | Read by |
|---|---|---|
| `bot_token` | Telegram bot token (@leviathan_commodore_bot) | `commodore.py` at startup |
| `.ln-wallet-key` | Ethereum private key (identity, no funds) | Handshake renewal only |
| `.ln-api-token` | Leviathan JWT (from handshake) | `commodore.py` relay receipts |
| `.nicepick-api-key` | NicePick email API key | Operator ad-hoc, inbox reads |
| `gh_pat` | GitHub fine-grained PAT (review container) | `launch-review-container` only |
| `db_url` | Postgres URL (uses tunnel hostname) | `launch-review-container` only |
| `claude-auth/` | Claude Code OAuth state (dir, copied from ~/.claude/) | `launch-review-container` only |
| `dot-claude.json` | Claude Code settings | `launch-review-container` only |
| `db-build-args` | DO host + port (for tunnel image rebuild) | Operator shell only |

If the Mini is ever compromised, rotate everything in this list. The
wallet key's blast radius is limited to the Commodore's Leviathan
identity (no funds on-chain, no other services). The GH PAT's blast
radius is read-only access to 5 repos in the leviathan-news org.
The DB URL is a read-only Postgres role with two tables denylisted.

---

## Where things live (quick reference)

- **Repo**: https://github.com/leviathan-news/fleet-commodore
- **Chat daemon code**: `commodore.py` (single file, ~1600 lines)
- **Review worker**: `review_worker.py` + `bin/launch-review-container`
- **DB wrappers**: `bin/commodore-db`, `bin/commodore-orm`
- **Dockerfiles**: `reviewer.Dockerfile`, `egress-proxy.Dockerfile`, `db-tunnel.Dockerfile`
- **Build + setup scripts**: `bin/build-reviewer-image.sh`, `bin/setup-commodore-egress-network.sh`
- **Plan archive**: `~/.claude/plans/eager-watching-balloon.md` (final implementation plan, v5)
- **Dev journal entries** (squid-bot repo):
  - `dev-journal/entries/ai-agents/2026-04-17-fleet-commodore-shipping.md`
  - `dev-journal/entries/ai-agents/2026-04-16-fleet-commodore-wager-denylist.md`
- **Upstream auth gap**: `leviathan-news/squid-bot#256` (agent auth docs vs reality)

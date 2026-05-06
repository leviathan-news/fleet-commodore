# Fleet Commodore — PR Review Runbook

> **Scope (as of v6 plan):** despite the filename, this runbook now covers the
> conversational-build, Q&A, recovery, and duplicate-cleanup operations as well
> as PR review. The filename is preserved to avoid breaking live references at
> `scripts/pending-mail-to-eunice.py:59` and elsewhere; a future isolated PR
> can rename to `OPERATOR_RUNBOOK.md` with an exhaustive caller sweep.

Operator-facing runbook covering first-time setup, rotations, image rebuilds,
troubleshooting, and shutdown.

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

### "Admiral has gone quiet"

Most common cause: Claude OAuth on the Mini has expired. The hourly
heartbeat (`cron/claude-oauth-heartbeat.sh`, runs at `17 * * * *`)
should alert Bot HQ on auth failure within 1 hour. If you see:

```
⚠️ Fleet Commodore: Claude OAuth has expired on the Mini.
The daemon will fall back to Codex (also broken) until you run
`claude /login` on the Mini. Last probe at <iso> returned 401.
```

Just run `ssh mini` (or open a Mini terminal) and:

```bash
claude /login
```

That opens a browser-OAuth flow. After successful login:

- The next user message in chat triggers `_try_clear_breaker_via_probe()`,
  which probes Claude (≤10 min later) and clears the breaker on success.
- **No daemon restart needed** — the in-memory breaker state self-heals.
- Verify with: `tail -5 ~/dev/leviathan/fleet-commodore/logs/claude-heartbeat.log`
  — next hour's run should report `state=ok`.

### Diagnostic chain when Admiral is silent

```bash
# 1. Daemon process alive?
ssh mini 'ps aux | grep commodore.py | grep -v grep'

# 2. Claude CLI auth direct test
ssh mini 'echo "ping" | /opt/homebrew/bin/claude --print --output-format text 2>&1 | head -3'
# 401 = run claude /login. "ping" or similar = auth fine, dig deeper.

# 3. Heartbeat log (hourly status)
ssh mini 'tail -10 ~/dev/leviathan/fleet-commodore/logs/claude-heartbeat.log'

# 4. Daemon log for breaker activity
ssh mini 'grep -E "Claude|breaker|probe|Falling back" ~/dev/leviathan/fleet-commodore/logs/commodore.log | tail -10'

# 5. If a message was sent but no reply:
ssh mini 'grep "$(date +%Y-%m-%d)" ~/dev/leviathan/fleet-commodore/logs/commodore.log | tail -20'
```

If breaker shows "still failing" repeatedly even though Claude is healthy:
the persona-suffix prompt may be causing SKIP. Check the chat policy
for that surface in `_policy_for()` and the `generate_response` action.

### Self-healing breaker contract

The breaker is in `commodore.py` around line 1576. Key state:

- `_claude_failures` (int): increments per failed Claude call, max=3.
- `_claude_unavailable_until` (float): cooldown timer.
- `_claude_last_probe_at` (float): rate-limits probes when tripped.
- `_CLAUDE_PROBE_INTERVAL_S` (env override `CLAUDE_PROBE_INTERVAL_S`,
  default 600s): minimum gap between probes.

When **both** counters are clear → `_claude_is_available()` returns True
without probing (zero overhead in healthy state).

When **either** is tripped → call `_try_clear_breaker_via_probe()`. That
function runs a probe ONLY if the rate-limit interval has elapsed; on
clean response (no 401, no quota text) it resets both counters.

This gives us:
- **Detection**: ≤1h via cron heartbeat
- **Recovery**: ≤10min after operator runs `claude /login`
- **No daemon restart needed** for routine OAuth rotation



### Reviewer image build fails

If `./bin/build-reviewer-image.sh` fails during `apt-get` on the Mini, two
known causes (both fixed in commits 011dd0e and f6c13be, but worth knowing
when bumping base images):

- **`Sub-process returned an error code (100)` early — hook misfires.**
  The base image's `/etc/apt/apt.conf.d/docker-clean` Post-Invoke hook
  calls `rm -f` against globs that fail on certain Docker storage drivers.
  Fix: `RUN echo > /etc/apt/apt.conf.d/docker-clean` early in the
  Dockerfile (already present).

- **`lzma error: Cannot allocate memory` during `dpkg --unpack`.** The
  Mini's Docker Desktop VM has 2 GiB allocated, which is below trixie
  (Debian 13)'s lzma-decompression working-set requirement for the bigger
  packages. Pin to `python:3.11-slim-bookworm` instead of bare `:slim`
  (already done). If you bump the base, verify the build still works
  within 2 GiB. Bumping the VM allocation in Docker Desktop preferences
  is the long-term fix.

To diagnose a NEW build failure, capture the full log:

```bash
docker build -f reviewer.Dockerfile -t test-build . > /tmp/build.log 2>&1
tail -50 /tmp/build.log
```

The actual dpkg error message often appears mid-output, NOT at the end.

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
- **Plan archive**: `~/.claude/plans/eager-watching-balloon.md` (v1) and
  `~/.claude/plans/valiant-squishing-crab.md` (v6 — current implementation plan)
- **Dev journal entries** (squid-bot repo):
  - `dev-journal/entries/ai-agents/2026-04-17-fleet-commodore-shipping.md`
  - `dev-journal/entries/ai-agents/2026-04-16-fleet-commodore-wager-denylist.md`
- **Upstream auth gap**: `leviathan-news/squid-bot#256` (agent auth docs vs reality)


---


## Per-pipeline durability contract (v6)

Operators and reviewers MUST use this exact split in all comms (Bot HQ pin,
deploy-time announcements, post-incident write-ups). Do NOT describe the
system as "no double side-effects on any pipeline" — that overstates what
the design delivers.

- **Build pipeline: fully idempotent.** No duplicate PR is possible across
  any crash window. Three independent pre-flights: scratch-file scan,
  `gh pr list --head leviathan-agent:<branch>`, `outgoing_msg` log. The
  GitHub side effect (the PR itself) is its own external oracle.

- **Q&A pipeline: best-effort with a documented narrow window.** No silent
  skip. **A single duplicate Telegram answer is possible** if the daemon
  crashes between `outgoing_msg` intent insert and Telegram's response
  being recorded. We surface duplicates rather than swallow them — operators
  resolve via `bin/commodore-dup-cleanup`.

- **Review pipeline: same contract as Q&A.** Same primitives, same residual
  duplicate window, same cleanup recipe.

This is the limit achievable without Telegram-side idempotency tokens.


## Q&A egress: separate network

Q&A runs on a dedicated network (`commodore-qa-egress`) with NO GitHub
allowlist. The proxy image is `commodore-qa-egress-proxy:latest`, built
from `egress/qa-filter` (which omits all `*.github.com` entries).

```bash
# Inspect the network membership
docker network inspect commodore-qa-egress

# Confirm no GitHub from inside (should print "PASS: api.github.com denied")
./bin/setup-commodore-qa-egress-network.sh
```

Why: the Q&A worker has the docs/dev-journal corpus mounted read-only at
`/app/knowledge` and the read-only Postgres reader role via the
`commodore-qa-db-tunnel` sidecar. There is no concrete Q&A workload that
needs live GitHub access. Mounting `GH_TOKEN` would widen blast radius for
no gain.


## Boot recovery

When the daemon restarts, `_recover_jobs_on_boot()` runs ONCE before any
worker thread starts pulling from queues. It:

1. Sweeps `*.result.json.tmp` files in `~/.local/state/commodore/results/`
   older than 60s. These are evidence of a worker crash mid-write; their
   contents are guaranteed garbage by the atomic write protocol.
2. Re-queues every `(queued | in_progress)` row in `build_job`, `qa_job`,
   and `pr_review`. Workers' pre-flight branches detect already-completed
   side effects via the scratch file or external oracle and reconcile
   without producing a second side effect.

What the operator sees on a restart that recovered work:
```
[INFO] sweep_stale_tmp_files: removed N orphaned .tmp files
[INFO] recovery: {'build': X, 'qa': Y, 'review': Z, 'tmp_swept': N,
                  'reconciled': R, 'requeued': Q}
```

Inspect what's in flight after a restart:
```bash
sqlite3 commodore.db "
  SELECT 'build' AS pipe, status, attempt_count, target_repo, error
    FROM build_job WHERE status NOT IN ('succeeded','failed','abandoned','orphaned')
  UNION ALL
  SELECT 'qa', status, attempt_count, NULL, declined_reason
    FROM qa_job WHERE status NOT IN ('answered','declined','failed')
  UNION ALL
  SELECT 'review', status, attempt_count, repo, error
    FROM pr_review WHERE status NOT IN ('posted','failed','orphaned','superseded')
  ORDER BY 1, 2;
"
```


## Per-action authorization model

| Action | Predicate | Required chat | Required role |
|---|---|---|---|
| `let's plan ...` | `_can_plan` | Bot HQ | admin |
| `ship it`, `/ship` | `_can_ship` | Bot HQ | admin |
| `abandon plan`, `/abandon` | `_can_plan` | Bot HQ | admin |
| `review PR N`, `/review N` | `_can_ship` (same as PR-file) | Bot HQ | admin |
| `/ask ...`, "how/what/why ..." | `_can_qa` | Bot HQ ∪ Lev Dev ∪ Agent Chat ∪ admin DM | none |

Out-of-policy callers receive an in-character decline pointing them to the
correct chat. The predicates live in `commodore.py` near `_is_admin()`.


## Idempotency keys and manual retry

Every job table has `idempotency_key` and `side_effect_completed_at`:

- `build_job.idempotency_key = sha256(target_repo + target_branch + plan_body)`
- `qa_job.idempotency_key = sha256(chat_id + thread_id + request_msg_id + question)`
- `pr_review.idempotency_key` (legacy rows have empty key, exempt from unique index)

To force a retry of a specific job (operator override):
```sql
-- Build: clear the side-effect flag, requeue
UPDATE build_job
   SET side_effect_completed_at = NULL, status = 'queued', pr_url = NULL
 WHERE job_uuid = '<uuid>';
-- Then bounce the daemon: tmux kill-window -t leviathan:commodore
```

Inspect:
```sql
SELECT id, status, idempotency_key, side_effect_completed_at, pr_url, error
  FROM build_job ORDER BY id DESC LIMIT 10;
```


## Result scratch directory (`~/.local/state/commodore/results/`)

Owned by the coordinator user, mode `0o700`. Workers write
`<uuid>.result.json` here as their first irreversible act (e.g. immediately
after `gh pr create` returns 201). The COORDINATOR reads + unlinks after
recording the outcome to SQLite. The launcher MUST NOT delete files here.

Atomic write protocol (POSIX `rename(2)` is atomic on the same filesystem):
1. Write to `<uuid>.result.json.tmp` + `fsync(fd)`
2. `os.rename(tmp, final)`
3. `fsync(dir_fd)`

Operator should never see `.tmp` files outside a sub-second window. A `.tmp`
file persisting >60s is evidence of a worker crash mid-write — boot recovery
sweeps these. To check for stale orphans manually:
```bash
find ~/.local/state/commodore/results/ -mtime +7 -name '*.json'
# Anything here is either an unrecovered side-effect or operator-leftover.
```


## Outgoing-message write-ahead log (`outgoing_msg`)

The dedup oracle for QA/review. Every Telegram send issued on behalf of a
job goes through `send_message_with_wal`, which:
1. INSERT OR IGNORE intent row keyed by `(job_table, job_uuid, intent_id)`.
2. Telegram POST.
3. UPDATE row with `telegram_message_id` + `sent_at` (or `error`).

Operator queries:
```sql
-- In-flight (intent recorded, response not yet returned)
SELECT job_table, job_uuid, action_type, dedup_token, intent_recorded_at
  FROM outgoing_msg
 WHERE telegram_message_id IS NULL
 ORDER BY id DESC LIMIT 20;

-- Recent confirmed
SELECT job_table, job_uuid, action_type, telegram_message_id, sent_at
  FROM outgoing_msg
 WHERE telegram_message_id IS NOT NULL
 ORDER BY id DESC LIMIT 20;

-- Detect duplicates (the contract residual — see below)
SELECT job_table, job_uuid, action_type, COUNT(*) AS dup_count
  FROM outgoing_msg
 WHERE telegram_message_id IS NOT NULL
   AND cleanup_role IS NULL
 GROUP BY 1, 2, 3
HAVING COUNT(*) > 1;
```


## Duplicate-cleanup workflow (v7)

When the duplicate-detection query returns rows, run `bin/commodore-dup-cleanup`.

### Detection

```bash
./bin/commodore-dup-cleanup --list
```

### Resolution

For each `(job_table, job_uuid, action_type)` group:
- **Canonical** = lowest `id` (= earliest `intent_recorded_at`).
- **Suppressed** = all others. Telegram-side action depends on age + chat type:

| Pipeline / chat | Action | Why |
|---|---|---|
| QA/review threaded chat, ≤48h | `editMessageText` to "_duplicate of N° X — superseded_" | Preserves reply chain. |
| QA/review non-threaded chat, ≤48h | `deleteMessage` | No chain to preserve. |
| Build ack/apology, ≤48h | `editMessageText` to pointer | PR itself is unique; ack is innocuous. |
| Any, >48h | Leave-in-place + `dup_followup` reply | Telegram refuses edit/delete past 48h. |

```bash
# Interactive single-group resolve
./bin/commodore-dup-cleanup --resolve qa_job <job_uuid> qa_answer

# Bulk dry-run
./bin/commodore-dup-cleanup --resolve-all --dry-run

# Bulk execute (only auto-resolves groups where all rows are <48h)
./bin/commodore-dup-cleanup --resolve-all
```

After successful Telegram-side action, `outgoing_msg.cleanup_role` and
`cleanup_action` are set. The detection query filters on `cleanup_role IS NULL`,
so already-resolved duplicates stop appearing in `--list`.

### Auditability

```sql
SELECT id, action_type, telegram_message_id, sent_at,
       cleanup_role, cleanup_action, cleanup_at, cleanup_operator_id
  FROM outgoing_msg
 WHERE job_uuid = '<uuid>'
 ORDER BY id ASC;
```

### What this does NOT promise

- It does not PREVENT duplicates. The contract residual stands.
- It does not retroactively fix user confusion if both messages were read
  before cleanup. The pointer text is the best we can do.
- It only catches duplicates logged via the WAL helper. All QA/review reply
  paths MUST go through `send_message_with_wal()` — there is a unit test
  enforcing this.


## GitHub App auth (v7 build pipeline)

The build pipeline (operator says `ship it` → real PR filed) authenticates
to GitHub via a **GitHub App installation token**, not a user PAT.

### Why a GitHub App, not a PAT

Empirical: the `leviathan-news` org clamps fine-grained PATs at
`administration=read` regardless of what's requested in the token, what's
approved at the org-pending-tokens page, or what the token settings panel
claims. This blocks `POST /repos/.../forks` (administration=write) and
also blocks direct branch push to org-owned repos for non-member users.

GitHub Apps don't have these problems. An App installed on the org with
**Contents: write** + **Pull requests: write** can:

- Push branches directly to source repos (no fork needed)
- File same-repo PRs (`head=branch`, no `owner:branch` cross-repo prefix)
- Work against any repo in the installation's selected set automatically

### One-time setup

Operator (logged in as the GitHub identity that should "own" the App):

1. **Register the App**: https://github.com/settings/apps/new
   - Name: `Leviathan Fleet Commodore` (becomes the bot's display name)
   - Homepage: any URL (required field, value irrelevant)
   - Webhook: **uncheck Active** (we don't use webhooks)
   - Repository permissions:
     - **Contents: Read and write** (clone, branch push)
     - **Pull requests: Read and write** (file PRs, comment)
     - **Metadata: Read-only** (auto-required)
     - **Issues: Read and write** (optional — useful if PRs reference issues)
   - Where can this App be installed: **Only on this account**
   - **Generate**

2. **Generate a private key**: at the bottom of the App's settings page,
   "Private keys" section → **Generate a private key**. A `.pem` downloads.
   Stage it on the Mini:
   ```bash
   ssh mini "cat > ~/.config/commodore/github-app-key.pem"
   # paste PEM contents, Enter, Ctrl-D
   ssh mini "chmod 600 ~/.config/commodore/github-app-key.pem"
   ```

3. **Note the App ID**: top of the App's settings page, "App ID: 12345".

4. **Install the App on `leviathan-news`**:
   On the App's settings page, sidebar **Install App** → click **Install**
   next to `leviathan-news` → choose **All repositories** → **Install**.

5. **Note the Installation ID**: after installing, GitHub redirects to a
   URL like `https://github.com/organizations/leviathan-news/settings/installations/67890`
   — the trailing number is the installation ID.

6. **Stage the App config on the Mini**:
   ```bash
   ssh mini 'cat > ~/.config/commodore/github-app.env <<EOF
   GITHUB_APP_ID=12345
   GITHUB_APP_INSTALLATION_ID=67890
   EOF
   chmod 600 ~/.config/commodore/github-app.env'
   ```

### Verify

```bash
ssh mini 'cd ~/dev/leviathan/fleet-commodore && \
  python3 -c "
import os
os.environ[\"GITHUB_APP_ID\"] = open(os.path.expanduser(\"~/.config/commodore/github-app.env\")).read().split(\"GITHUB_APP_ID=\")[1].split(chr(10))[0]
os.environ[\"GITHUB_APP_INSTALLATION_ID\"] = open(os.path.expanduser(\"~/.config/commodore/github-app.env\")).read().split(\"GITHUB_APP_INSTALLATION_ID=\")[1].split(chr(10))[0]
os.environ[\"GITHUB_APP_PRIVATE_KEY_PATH\"] = os.path.expanduser(\"~/.config/commodore/github-app-key.pem\")
import sys; sys.path.insert(0, \".\")
import build_worker
token = build_worker._mint_installation_token()
print(\"installation token minted, length:\", len(token))
"'
```

If that prints a length, the App is set up correctly. The next `ship it`
will use this path and fork-via-API errors disappear.

### Rotations

- **Private key**: GitHub Apps can hold up to two private keys. Generate a
  new one, stage it, then revoke the old one from the App settings page.
  No downtime.
- **Installation token**: minted on demand by the worker, valid 1h. No
  operator action.

### Adding more repos

If the App is installed with **All repositories**, new repos in
`leviathan-news` are automatically accessible — no further config.
If installed with **Selected repositories**, edit the installation
on https://github.com/organizations/leviathan-news/settings/installations
and add the new repo.

---

## DB role hardening (one-shot SQL)

> **GOTCHA discovered 2026-04-26**: column-level `REVOKE SELECT (col)` is a
> no-op if `GRANT SELECT ON ALL TABLES IN SCHEMA public` has already
> expanded into per-column grants. The robust pattern is REVOKE-ALL +
> re-GRANT the safe columns explicitly. The script below uses that pattern.

Run as DigitalOcean DB admin (`doadmin`) once per database. The full
provisioning script lives at `bin/provision-commodore-reader` (drop a copy
on the Mini and run via `squid-env/bin/python`):

```sql
-- Whole-table REVOKEs (work as expected):
REVOKE ALL ON django_session FROM commodore_reader;
REVOKE ALL ON bot_social_account FROM commodore_reader;
REVOKE ALL ON bot_webauthn_credential FROM commodore_reader;
REVOKE ALL ON bot_pending_account_claim FROM commodore_reader;
REVOKE ALL ON lnn_user_login_event FROM commodore_reader;  -- if exists

-- Column-level: REVOKE-ALL + re-GRANT specific columns is the only
-- pattern that survives the bulk-grant expansion.
-- bot_user: keep id, telegram_account, etc; deny email, unique_token, password
REVOKE ALL ON bot_user FROM commodore_reader;
GRANT SELECT (id, telegram_account, ethereum_address, account_type, ...) -- enumerate
       ON bot_user TO commodore_reader;

-- lnn_click: keep id, news_id, etc; deny ip_address, user_agent
REVOKE ALL ON lnn_click FROM commodore_reader;
GRANT SELECT (id, news_id, source, ...)  -- enumerate
       ON lnn_click TO commodore_reader;
```

Verify (each sensitive query MUST error with `permission denied`; each
allowed query MUST succeed):

```sql
-- As commodore_reader:
SELECT count(*) FROM lnn_news;                 -- ALLOWED
SELECT id FROM bot_user LIMIT 1;               -- ALLOWED
SELECT email FROM bot_user LIMIT 1;            -- DENIED
SELECT unique_token FROM bot_user LIMIT 1;     -- DENIED
SELECT password FROM bot_user LIMIT 1;         -- DENIED
SELECT * FROM bot_social_account LIMIT 1;      -- DENIED
SELECT * FROM bot_webauthn_credential LIMIT 1; -- DENIED
SELECT * FROM bot_pending_account_claim LIMIT 1; -- DENIED
SELECT * FROM django_session LIMIT 1;          -- DENIED
SELECT id FROM lnn_click LIMIT 1;              -- ALLOWED
SELECT ip_address FROM lnn_click LIMIT 1;      -- DENIED
SELECT user_agent FROM lnn_click LIMIT 1;      -- DENIED
```


## New file inventory (v6)

- **`commodore.py`** — schema additions, `OutgoingAction` enum, `_can_*()`
  predicates, `send_message_with_wal()`, `_claim_build_job()`/`_claim_qa_job()`,
  `handle_plan_message`/`handle_ship`/`handle_abandon`/`handle_qa`,
  `_process_build`/`_process_qa`/`_process_review`, `_recover_jobs_on_boot()`,
  `_start_workers()`. RESULTS_DIR + atomic-read helpers.
- **`bin/launch-build-container`** — fork-and-PR launcher; mounts results dir.
- **`bin/launch-qa-container`** — Q&A launcher; NO GH_TOKEN; knowledge corpus
  mount; runs on `commodore-qa-egress`.
- **`bin/launch-review-container`** — modified to mount results dir.
- **`bin/setup-commodore-qa-egress-network.sh`** — provisioning for the Q&A
  network + sidecars.
- **`bin/commodore-dup-cleanup`** — operator helper for the v7 cleanup recipe.
- **`egress/qa-filter`** — tinyproxy allowlist for the Q&A network (no GitHub).
- **`review_worker.py`** — fleshed out from v1 stub: real `gh pr diff` +
  Claude review + atomic scratch write.

# Fleet Commodore

Formal chat agent + draft PR assistant for the Leviathan fleet. Lives in Bot HQ,
Squid Cave, and the Agent Chat room. Persona: King's Navy commodore, Admiral of
the Fleet. Never wagers — declines `/buy` and `/sell` outright but may inspect
`/markets`, `/leaderboard`, and `/position`.

Intended foil to **DeepSeaSquid**, the corsair bot that runs on moltbook.

## Host and release contract

**Mac Mini only.** The currently supported daemon launcher is `run.sh` under a
single supervised session; do not treat a dirty developer checkout as a
release. Promote a clean reviewed worktree/artifact, emit a secret-free
manifest with `scripts/release_manifest.py`, and retain the prior immutable
artifact for rollback. The chat daemon and triage cron may have different
source SHAs only during an explicitly recorded transition. Set
`FLEET_COMMODORE_CONFIG` to one service-owned, non-release config file on the
Mini; a release worktree must never become the owner of tokens or mutable
runtime configuration. Cron sets both that path and
`FLEET_COMMODORE_RELEASE_DIR` for `cron/watchdog.sh`; the watchdog passes them
to its tmux-launched chat daemon. Set `FLEET_COMMODORE_STATE_DIR` (and, when
needed, explicit `COMMODORE_DB_FILE` / `TRIAGE_DB_FILE`) so chat history,
triage fences, and logs survive artifact replacement. Set
`FLEET_COMMODORE_PYTHON` to the versioned service-owned interpreter snapshot;
do not copy a virtual environment into the Git worktree.

The registered watchdog checks the actual sole process, release and tmux pane,
not window existence alone. Restarts are bounded and fail closed around live
shells, foreign/duplicate actors and takeover ownership. Its `--inspect` option
is read-only; see [the supervision contract](docs/CHAT_DELIVERY.md#ordinary-process-supervision).
The QA-readiness wrapper also inspects stored outcomes without a model. Its
explicit `--page` mode adds receipt-fenced operator alerts; register that changed
mode before enabling it on the Mini. See [outcome paging](docs/CHAT_DELIVERY.md#independent-outcome-paging).

## Build + run

Conversation and read-only Q&A default to **Codex / Luna**, using the service
account's existing ChatGPT subscription login. No API key or API-billed fallback
is used. Set `FLEET_PROVIDER` and optionally `FLEET_QA_PROVIDER` to `claude` only
for an intentional legacy route. Build/review and security triage still use
Claude and are not repaired by switching the conversation provider.

```bash
cp .env.example .env            # fill in tokens, channel ids, admin ids
# Secrets go in ~/commodore-secrets/ on the Mini, chmod 600:
#   ~/commodore-secrets/bot_token
#   ~/commodore-secrets/gh_pat
docker compose build
docker compose up -d
docker compose logs -f          # primary ops surface
```

## Exclusive helm control

The optional Mini-local helm controller provides durable Telegram intake and a
single reply lease for bounded operator absences. The ordinary release remains
unchanged until a blue/green takeover passes isolated bridge-loss and
watcher-loss tests. During an exclusive Sol lease, the successor Fleet process
continues polling and queues every update before routing, but its send fence
denies Fleet replies. Sol claims queued events through a replaceable bridge.

The lease state, Telegram offset, event payloads, conversation context, send
fences, and transition receipts live in the service state directory. A
service-owned cron row checks the controller every minute. Expiry of the Sol,
watcher, or bridge lease stops the successor and restores the pinned ordinary
Fleet release. See [the helm controller runbook](docs/HELM_CONTROLLER_RUNBOOK.md).

## Server-side prerequisite (squid-bot)

After the Commodore bot is registered on Telegram, look up its `telegram_user_id`
via `getMe` and add that int to **`AGENT_WAGER_DENYLIST`** in the squid-bot prod
`.env`. That server-side denylist blocks `/buy` and `/sell` at both the webhook
dispatcher (`bot/webhook_processor.py`) and the Agent Chat relay
(`website/agent_chat_write_views.py`). The bot-side regex in `commodore.py` is
the first line; `AGENT_WAGER_DENYLIST` is the backstop.

## Per-channel policy

Numeric chat IDs in `commodore.py::ROOM_CAPABILITY_REGISTRY` are the
authorization source of truth; titles are display-only. All trusted rooms have
direct read-only Q&A and bounded text-document review. Write capabilities stay
independently scoped.

| Room | Trust | Direct-hail contract | Writes |
|------|-------|----------------------|--------|
| Bot HQ | trusted | Q&A + attachment review | PR/ship + GitHub comments: all crew |
| Lev Dev | trusted | Q&A + attachment review | PR/ship: all crew |
| Agent Chat | trusted, all topics | Q&A + attachment review | PR/ship + GitHub comments: all crew |
| Leviathan Atlas | trusted | Q&A + attachment review | PR/ship + GitHub comments: all crew |
| Lev Sec Alert | trusted/security | Q&A + attachment review; explicit reply-bound status | PR/ship + GitHub comments: all crew; triage remains cron-only |
| Squid Cave | public/untrusted | Fixed, rate-limited decline only | none; no model, worker, context, or file retrieval |
| Unknown room | unclassified | Silent/fail closed | none |

## PR filing (v1)

`@commodore_lev_bot please file a PR to ...` from an admin in Bot HQ records
an audit row, acknowledges with a formal dispatch, and queues the intended branch
name. Actual branch creation + push + draft-PR open is v2 scope — the v1 shell
establishes authorization, audit, repo allowlist, and branch-naming convention
(`commodore/<slug>-<utc-date>`).

Allowed repos (in-code allowlist in `commodore.py`):
- `leviathan-news/squid-bot`
- `leviathan-news/auction-ui`
- `leviathan-news/be-benthic`
- `leviathan-news/agent-chat`
- `leviathan-news/fleet-commodore`

PRs are authored by the separate GitHub user `leviathan-commodore` with a
scoped PAT mounted read-only at `/run/secrets/gh_pat` in the container.

## Telegram document review

A directly addressed document in a Q&A-authorized room (including Lev Dev and
Leviathan Atlas) is retrieved through Telegram's Bot API and handed to the
read-only Q&A worker. Media captions are treated as the message text, so a
captioned `@commodore please review this draft` works without a question mark.
The same request works as a direct reply to a preceding document-only message;
the reply remains the request while its parent's document is reviewed.

Intake accepts UTF-8 `.md`, `.markdown`, `.txt`, `.rst`, `.json`, `.csv`,
`.yaml`, and `.yml` files, plus `.zip` bundles containing those text types. A
plain document has a 128 KiB default text budget;
`TELEGRAM_TEXT_DOCUMENT_MAX_BYTES` may lower it or raise it only to the hard
256 KiB ceiling. A ZIP may be at most 4 MiB compressed, is decoded in memory
without filesystem extraction, may contain at most 256 entries, and shares the
same configurable text budget across readable members. Unsafe, binary,
unsupported, encrypted, duplicate, or over-budget entries are skipped with
explicit coverage notes. Readable members are still reviewed when coverage is
partial.

The worker receives attachment text separately from the asker's request and
labels it untrusted data. Model-native tools, shell, filesystem, and unrestricted
network access remain disabled. The host's bounded read-only evidence broker
remains available for repository documents, SQL, GitHub observations, and a
model-selected Telegram document already verified in the same room scope. A
request to update Beads or GitHub can create only an immutable proposal in the
local tracker inbox; it performs no Beads or GitHub write and starts no work. The
read-only inspection/export CLI is
[`bin/tracker-proposals`](bin/tracker-proposals); automatic application by a
canonical tracker consumer is not implemented. Rejections and retrieval
failures acknowledge that the attachment arrived and state the safe reason,
without logging the bot token, authenticated file URL, or document body.

## Reply context

Conversation and Q&A use the message's exact reply referent, not whichever PR
was mentioned most recently. Selected Telegram quotes stay selected; older
parents come only from verified reply edges in the local ledger. Telegram's
`is_topic_message` distinguishes a real forum topic from an ordinary
supergroup reply thread, whose `message_thread_id` is a reply root rather than
a room boundary. Terse follow-ups may also receive bounded same-actor request
context and metadata-only document candidates; neither supplies authority.
See [Reply context contract](docs/REPLY_CONTEXT.md) for bounds, migrations,
failure behavior, and release acceptance.

## Tests

For send uncertainty, ordinary nonblocking intake, held-request inspection and
remaining cutover gates, see [Chat delivery certainty](docs/CHAT_DELIVERY.md).
Do not mistake source tests for live readiness or activate helm as a workaround.

For authentication outages, heartbeat semantics and live recovery verification,
see [Provider outage recovery](docs/CLAUDE_RECOVERY.md).

```bash
cd fleet-commodore
python -m pytest tests/ -v
```

Server-side denylist tests live in the squid-bot repo at
`tests/api/test_wager_denylist.py` (12 tests, all green).

## Lev Sec triage failsafe

`triage/commodore_triage.py` coalesces accepted Lev Sec deliveries into one
read-only Sonnet investigation. It uses a service-owned external
`TRIAGE_DB_FILE` ledger rather than `commodore.db`, and defaults to
`TRIAGE_POSTING_ENABLED=0`; dry-runs never post or retain a completed claim.
A passing empty-queue dry-run is not a readiness signal: run the exact cron
wrapper with `--provider-probe` to force one no-post Sonnet call before a live
post gate. Live posting requires a pinned `TRIAGE_OPERATOR_DM_USER_ID` (or
`OPERATOR_DM_USER_ID`) for a direct, deduplicated failure DM.

An ambiguous Telegram result remains `outcome_unknown` and is never retried.
Its local inspect/list/receipt-reconciliation commands are separately default-off
behind `TRIAGE_OPERATOR_RECONCILE_ENABLED=0` and require an explicit operator
confirmation; they do not post, invoke Claude, or install a schedule.

After the read-only `~/bin/sec_feed` wrapper exists on the Mini, an operator may
install the following failsafe schedule. It only runs the DB-delivery scan; it
does not restart the bot or add a Telegram polling hook.

```cron
*/5 * * * * /path/to/immutable/fleet-commodore/cron/commodore-triage.sh
```

The wrapper owns a service-state directory outside the worktree and an atomic
single-executor lock. A stale lock is fail-closed and must be inspected, not
deleted blindly. The explicit live-post decision remains outside this setup.
See `triage/HANDOFF.md` and `triage/RUNBOOK.md`.

## Plan

Full implementation plan and server-side denylist contract:
`leviathan-news/squid-bot:docs/plans/2026-04-16-commodore-agent.md`.

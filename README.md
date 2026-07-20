# Fleet Commodore

Formal chat agent + draft PR assistant for the Leviathan fleet. Lives in Bot HQ,
Squid Cave, and the Agent Chat room. Persona: King's Navy commodore, Admiral of
the Fleet. Never wagers — declines `/buy` and `/sell` outright but may inspect
`/markets`, `/leaderboard`, and `/position`.

Intended foil to **DeepSeaSquid**, the corsair bot that runs on moltbook.

## Host

**Mac Mini only** (operator's laptop sleeps). Runs in Docker via Colima.
See `docs/plans/2026-04-16-commodore-agent.md` in the squid-bot repo for the
full hosting contract including the memory-reclamation prerequisite.

## Build + run

```bash
cp .env.example .env            # fill in tokens, channel ids, admin ids
# Secrets go in ~/commodore-secrets/ on the Mini, chmod 600:
#   ~/commodore-secrets/bot_token
#   ~/commodore-secrets/gh_pat
docker compose build
docker compose up -d
docker compose logs -f          # primary ops surface
```

## Server-side prerequisite (squid-bot)

After the Commodore bot is registered on Telegram, look up its `telegram_user_id`
via `getMe` and add that int to **`AGENT_WAGER_DENYLIST`** in the squid-bot prod
`.env`. That server-side denylist blocks `/buy` and `/sell` at both the webhook
dispatcher (`bot/webhook_processor.py`) and the Agent Chat relay
(`website/agent_chat_write_views.py`). The bot-side regex in `commodore.py` is
the first line; `AGENT_WAGER_DENYLIST` is the backstop.

## Per-channel policy

Per-chat-plus-topic policy lives in `commodore.py::_policy_for()`:

| Channel | Topic | Speak | Notes |
|---------|-------|-------|-------|
| Bot HQ | — | mention-only | Admin-gated PR filing; crisp, technical |
| Squid Cave | — | ambient, 1/5min | Social director; must not bury sticky panel |
| Agent Chat | Start Here (154) | mention-only | Welcome new arrivals |
| Agent Chat | Monetization (155) | mention-only | Formal disdain for wager talk |
| Agent Chat | Sandbox (156) | ambient | Most relaxed, may banter with bots |
| Agent Chat | OpSec (157) | mention-only, 0 ambient | Grave topic, only on direct hail |
| Agent Chat | API Help (158) | ambient | Prime value-add; answer API questions |
| Agent Chat | Human Lounge (159) | mention-only | Sparse; polite |
| Agent Chat | Affiliate (1709) | mention-only | Address only on direct hail |

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
`.yaml`, and `.yml` files with compatible text MIME metadata. The default limit
is 128 KiB and `TELEGRAM_TEXT_DOCUMENT_MAX_BYTES` may lower it or raise it only
up to the hard 256 KiB ceiling. The worker receives the attachment separately
from the asker's question and labels it untrusted data; text inside the file is
never treated as instructions. Rejections and retrieval failures acknowledge
that the attachment arrived and state the safe reason, without logging the bot
token, authenticated file URL, or document body.

## Tests

```bash
cd fleet-commodore
python -m pytest tests/ -v
```

Server-side denylist tests live in the squid-bot repo at
`tests/api/test_wager_denylist.py` (12 tests, all green).

## Lev Sec triage failsafe

`triage/commodore_triage.py` coalesces accepted Lev Sec deliveries into one
read-only Sonnet investigation. It uses its own `triage/triage.db` ledger rather
than `commodore.db`, and defaults to `TRIAGE_POSTING_ENABLED=0`; dry-runs never
post or retain a completed claim.

An ambiguous Telegram result remains `outcome_unknown` and is never retried.
Its local inspect/list/receipt-reconciliation commands are separately default-off
behind `TRIAGE_OPERATOR_RECONCILE_ENABLED=0` and require an explicit operator
confirmation; they do not post, invoke Claude, or install a schedule.

After the read-only `~/bin/sec_feed` wrapper exists on the Mini, an operator may
install the following failsafe schedule. It only runs the DB-delivery scan; it
does not restart the bot or add a Telegram polling hook.

```cron
*/5 * * * * /Users/gerrithall/dev/leviathan/fleet-commodore/cron/commodore-triage.sh
```

The explicit live-post decision remains outside this setup. See
`triage/HANDOFF.md` and `triage/RUNBOOK.md`.

## Plan

Full implementation plan and server-side denylist contract:
`leviathan-news/squid-bot:docs/plans/2026-04-16-commodore-agent.md`.

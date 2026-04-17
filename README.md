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

## Tests

```bash
cd fleet-commodore
python -m pytest tests/ -v
```

Server-side denylist tests live in the squid-bot repo at
`tests/api/test_wager_denylist.py` (12 tests, all green).

## Plan

Full implementation plan and server-side denylist contract:
`leviathan-news/squid-bot:docs/plans/2026-04-16-commodore-agent.md`.

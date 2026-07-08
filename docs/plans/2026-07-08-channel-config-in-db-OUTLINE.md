# Plan Outline: Move Commodore channel authorization to DB config

**Status:** OUTLINE ONLY — deferred, attack another day. Not scoped for implementation.
**Date:** 2026-07-08
**Sparked by:** operator, after PR #15 (opening Atlas Q&A) required a full code+deploy
cycle to onboard a single channel.

## Problem

Channel authorization is scattered across hardcoded structures in `commodore.py`:
- `_PRIVILEGED_CHAT_IDS` (Q&A allowlist) — a tuple of env-driven constants
- `_SHIP_CHAT_IDS` (PR-filing allowlist)
- `_policy_for()` per-room overrides (`speak: mention_only|ambient`, cooldowns)
- `_can_ship` / `_can_plan` / `_can_comment` / `_can_qa` gates

Onboarding a new channel = edit code + update test matrix + PR + merge + merge-into-live-
branch on the Mini + restart the daemon. That friction recurs for every new room, and the
Atlas self-serve onboarding push points toward MORE rooms, not fewer.

## Proposed direction (split by risk — the key idea)

Not everything should move. Split the surface by blast radius:

- **MOVE to DB/config (low stakes, churns most):** the `speak` policy and the **Q&A**
  allowlist. A `CommodoreChannel` table: `chat_id`, `label`, `can_qa` (bool),
  `speak_policy` (`mention_only|ambient`), `ambient_cooldown_s`. Onboard a room = insert a
  row, no deploy, no restart (daemon re-reads on an interval or on a reload signal).

- **KEEP in code (high stakes, deploy-gated):** `can_ship` / `can_plan` (PR-filing +
  GitHub-comment authority). This bot can file PRs and comment on GitHub as the fleet —
  "who is allowed to do that" should stay behind the `test_authorization.py` matrix and a
  PR review, not be a live-mutable DB row. A DB-config auth boundary for write actions is a
  silent loss-of-control surface.

## Sketch of the work (NOT a spec)

1. Model: `CommodoreChannel` (or a JSON config file the daemon watches, if we want to avoid
   a DB dependency on the Mini — note the Mini has NO Redis and Django falls back to
   LocMemCache; a plain versioned JSON file re-read on mtime change may be simpler than a
   table here).
2. Loader: read config at startup + refresh on interval (or SIGHUP). Keep an in-memory
   snapshot; never hit the store per-message.
3. Replace `_can_qa()` allowlist check + `_policy_for()` overrides to consult the snapshot.
   Leave `_can_ship`/`_can_plan` on the code constants.
4. Migration path: seed the config store from today's hardcoded values so behavior is
   identical on day one. Keep `test_authorization.py` green by having the test fixtures load
   a known config.
5. Admin surface: a `/channels` Q&A-room command (admin-only) or a small management path to
   list/add/remove Q&A rooms without SSH.

## Open questions to resolve when we pick this up

- DB table vs. watched JSON file? (Mini has no Redis; a table means Django ORM on the Mini,
  which is fine but heavier than a file.)
- How does the daemon get told to reload — interval poll, file mtime, or a Telegram admin
  command that hot-reloads?
- Do we keep the env-var override as an escape hatch (belt-and-suspenders) or fully replace?
- Audit logging: any change to a Q&A room should leave a trail (who added it, when).

## Non-goals

- Do NOT move ship/plan/comment authorization to mutable config.
- Do NOT change current behavior in the same change that introduces the mechanism (seed to
  identical state first, verify parity, then use it).

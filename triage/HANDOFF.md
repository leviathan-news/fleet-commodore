# Handoff — Commodore Sonnet Security Triage

**Implementation status:** Steps 3 and 5 are implemented and tested on the isolated
`fleet-commodore/triage-steps-3-5` branch. They are not deployed: no Mini cron was
installed, no Commodore process was restarted, `commodore.py::poll()` is untouched, and
`TRIAGE_POSTING_ENABLED` remains `0` by default.

The security-triage control plane is deliberately separate from `commodore.db` and
from the Telegram polling loop.

- `commodore_triage.py --alert-id <uuid>` adds the event to a coalesced batch.
- `commodore_triage.py --scan-db` is the failsafe for accepted Lev Sec deliveries that
  Telegram did not deliver to the bot.
- The only agent shell permission is `Bash(sec_feed:*)`; there is no generic `Read`
  capability. The prompt receives only a sanitized, explicitly untrusted alert index;
  raw evidence stays behind the read-only Mini wrapper for `security_triage_feed`.
- `TRIAGE_POSTING_ENABLED` defaults to `0`. A disabled gate does not claim, post, or DM
  alerts; use `--dry-run` to inspect an agent result safely.
- Dequeue and claim commit atomically with a token-bound, batch-sized lease. A stale
  pre-send claim is requeued; a crashed or receipt-less post is durably
  `outcome_unknown` and is never blindly resent.
- `cron/commodore-triage.sh` supplies the Mini's documented five-minute cron command;
  deployment review must install it explicitly. It does not restart or otherwise control
  the bot.

The Telegram `poll()` hook is intentionally not part of this implementation. It is a
separate, live-bot decision.

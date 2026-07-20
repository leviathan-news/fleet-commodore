# Handoff — Commodore Sonnet Security Triage

**Implementation status:** the triage control plane is unified with the current
chat release candidate. It is not a production authorization: promotion still
requires the manifest, external-ledger, pinned-operator-DM, provider-probe,
single-executor, and watched-receipt gates in `RUNBOOK.md`.

The security-triage control plane is deliberately separate from `commodore.db` and
from the Telegram polling loop.

- `commodore_triage.py --alert-id <uuid>` adds the event to a coalesced batch.
- `commodore_triage.py --scan-db` is the failsafe for accepted Lev Sec deliveries that
  Telegram did not deliver to the bot.
- The only agent shell permission is `Bash(sec_feed:*)`; there is no generic `Read`
  capability. The prompt receives only a sanitized, explicitly untrusted alert index;
  raw evidence stays behind the read-only Mini wrapper for `security_triage_feed`.
- `TRIAGE_POSTING_ENABLED` defaults to `0`. A disabled gate does not claim, post, or DM
  alerts; use `--dry-run` to inspect an agent result safely. `--provider-probe`
  is the non-vacuous readiness gate: it forces a no-post Sonnet invocation even
  when the queue is empty.
- Dequeue and claim commit atomically with a token-bound, batch-sized lease. A stale
  pre-send claim is requeued only with an exact owner-token/expiry compare-and-delete;
  a crashed or receipt-less post is durably `outcome_unknown` and is never blindly
  resent. `TRIAGE_OPERATOR_RECONCILE_ENABLED` also defaults to `0`: it exposes a
  local, `--operator-confirm` inspect/list/terminal-resolution path with the
  rendered-note artifact and receipt evidence, but no automatic resend.
- `cron/commodore-triage.sh` supplies the Mini's documented five-minute cron command;
  deployment review must install it explicitly. It does not restart or otherwise control
  the bot.

The Telegram `poll()` hook does not enqueue or synchronously invoke triage. In trusted
Lev Sec, a direct reply to an original alert can read only its exact bound ledger
status; textual alert IDs never select work or trigger Sonnet. Re-triage remains a
separate control-plane decision.

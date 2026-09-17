# Chat delivery certainty and nonblocking intake

This source changes send recovery; it is not proof of a deployed repair.
Ordinary `poll()` now commits minimized updates and its cursor atomically, then
routes them through one FIFO worker. Provider work, document retrieval, history
cleanup and Benthic backup generation cannot block that polling path. The
optional helm path remains unchanged and separately gated; do not activate it
as a substitute for this release's ordinary intake.

## Send contract

`send_message` requires `ok=true` and a positive integer `message_id`.
Missing, malformed or uncertain responses cannot count as acceptance. HTML may
fall back once to plain text only after an explicit HTTP/Bot API 400 refusal.
Timeouts, decode errors and server failures never trigger a second POST. A
confirmed receipt remains valid even if local reply-history persistence fails.

Job sends commit one content-independent intent before contacting Telegram.
Ordinary routed replies (including queued-job acknowledgements and fixed
declines) use that same WAL keyed by the Telegram update ID. Their wording
cannot create another send intent. Direct helper calls without an intake ID
remain outside this ordinary routing contract and require the separate
operator/helm boundary; they are not an authorized replay interface.
The claim is serialized with `BEGIN IMMEDIATE` and persisted with SQLite WAL
and `synchronous=FULL`; concurrent calls cannot both send the same intent.
`delivery_status` distinguishes `prepared`, `accepted`, `failed` and
`outcome_unknown`. Positive legacy receipts still dedupe; legacy rows without
positive receipts remain unknown. A crash after preparation or accepted POST
but before receipt persistence leaves a held intent, not a reason to resend.

An existing intent without a positive receipt returns `ok=false, held=true`.
Queued/in-progress jobs become `delivery_held` before boot requeue or direct
coordinator relaunch. Their result files are retained. Definitive refusals are
also held rather than automatically retried: a later bounded retry mechanism
must prove rejection and acquire a fresh authorized claim.

This prevents automatic replay; it does not guarantee eventual delivery. A
prepared intent may represent a current sender or a dead sender. Do not clear
it while an actor could still send. Inspect ownership and an independent
Telegram receipt before any reconciliation. No automated reconciliation or
outcome watchdog is introduced by this foundation release. Until those gates
are completed, held requests require operator review. Do not delete the intent
or result file to force a replay or replace an unknown result with success.

Read-only inspection against the service-owned database:

```sql
SELECT job_table, job_uuid, chat_id, action_type, dedup_token,
       intent_recorded_at, delivery_status, telegram_message_id, error
FROM outgoing_msg
WHERE telegram_message_id IS NULL OR telegram_message_id <= 0
ORDER BY id LIMIT 100;
```

The result is capped. Check the full held count separately before claiming an
exhaustive review. Error fields contain fixed exception class names, never raw
authenticated URLs, Telegram responses or message bodies.

## Intake component

`ChatIntake` atomically stores an entire batch and its monotonically increasing
cursor, dedupes by update ID, caps unresolved events, and claims queued work
with tokens. Acknowledged/handed-off work remains unresolved. Terminal outcomes
clear stored payloads. Running, handed-off and unknown events are not
automatically recycled. New database files are created privately before SQLite
opens them; existing file modes are preserved and symlink database paths are
rejected. Connections close after each operation.

Room authorization now precedes persistence. Unknown messages retain only an
update ID, public hails retain a static yes/no signal, and trusted messages keep
only routing/document metadata and a bounded reply chain. A single routing
worker preserves FIFO ordering without unlimited concurrency. The queue caps
unresolved events at 4096; overflow leaves the durable offset unchanged.

An OS-held service lock excludes other cooperating ordinary pollers. Only after
acquiring it does boot mark interrupted routing claims `held_unknown`, never
requeue them. An ambiguous send or routing exception remains held. Shutdown
does not release polling ownership while the routing thread can still send;
a permanently stuck thread needs process-level supervision. This lock cannot
exclude older releases or unrelated token holders, so cutover still requires
independent sole-actor verification. A 409 backs off instead of interfering
with another poller. The old release has no ordinary durable cursor: first
cutover must explicitly reconcile pending Telegram updates and existing
positive reply receipts, not assume starting a new ledger at zero is replay-safe.

Q&A, review and build acknowledgements link to their durable job identity and
remain `handed_off`. Final job status alone is insufficient: reconciliation
requires its positive outgoing receipt before resolving or escalating intake.
New build rows also retain the exact ship message ID. No receipt means the
request remains unresolved. Intake outcome logs expose counts, oldest unresolved
age and router liveness without payloads or model calls. Terminal payloads clear
immediately; terminal metadata prunes after 30 days in bounded batches. Cursor
fencing prevents pruned old IDs from becoming new work.

The local `bin/qa-healthcheck.py` also reads intake counts, oldest unresolved
age and the latest receipt-backed terminal time with SQLite `mode=ro`. It warns
about held requests or an oldest age over 120 seconds without claiming that
dependency readiness validates provider transport. Missing intake state is
reported as not installed, not silently created. This diagnostic does not yet
page the operator or supervise a stuck router.

Remaining gates include receipt reconciliation for held/legacy requests, bounded
end-to-end deadlines and prompt acknowledgement under backlog, an active outcome
watchdog/escalation path, dead-process supervision and rehearsal of controller
ownership. The generic storage API is not a license to admit public-room models
or persist arbitrary private updates. This source is not live readiness proof.

## Verification boundary

Local fixtures cover batch rollback, reopen/dedupe, capacity, claim races,
token fences, private creation, connection lifetime, positive receipt checks,
safe HTML rejection fallback, uncertain outcomes, concurrent job sends and boot
recovery. Provider/Telegram calls are mocked. None proves live service behavior,
fresh useful replies, single-owner takeover or the required unattended soak.
Helm remains a separately reviewed and rehearsed deployment gate.

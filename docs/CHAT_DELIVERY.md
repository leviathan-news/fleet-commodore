# Chat delivery certainty and intake foundation

This source changes send recovery; it is not proof of a deployed repair.
`chat_intake.py` is a tested storage component, not yet connected to `poll()`.
Polling remains synchronous until the asynchronous routing release is completed.

## Send contract

`send_message` requires `ok=true` and a positive integer `message_id`.
Missing, malformed or uncertain responses cannot count as acceptance. HTML may
fall back once to plain text only after an explicit HTTP/Bot API 400 refusal.
Timeouts, decode errors and server failures never trigger a second POST. A
confirmed receipt remains valid even if local reply-history persistence fails.

Job sends commit one content-independent intent before contacting Telegram.
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

Integration must still enforce room authorization and payload minimization
before persistence, per-thread ordering, explicit durable job handoff, receipt-
backed completion, bounded worker deadlines, retention, ownership reconciliation
and model-free oldest-unresolved monitoring. The generic component is not a
license to store arbitrary private updates or admit public-room model work.

## Verification boundary

Local fixtures cover batch rollback, reopen/dedupe, capacity, claim races,
token fences, private creation, connection lifetime, positive receipt checks,
safe HTML rejection fallback, uncertain outcomes, concurrent job sends and boot
recovery. Provider/Telegram calls are mocked. None proves live service behavior,
fresh useful replies, single-owner takeover or the required unattended soak.
Helm remains a separately reviewed and rehearsed deployment gate.

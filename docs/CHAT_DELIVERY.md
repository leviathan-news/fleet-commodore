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
Telegram receipt before any reconciliation. No automated request/receipt
reconciliation is introduced. The independent pager below reports held work
but cannot resolve it. Held requests still require operator review. Do not delete the intent
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

### First legacy cutover

Ordinary startup now refuses a ledger without `legacy_capture_complete=1`,
before contacting Telegram or starting workers. During the authorized offline
transition, pause the legacy watchdog, independently verify that the sole old
actor and any sending worker have stopped, and retain atomic state backups.
Do not activate helm or discard pending updates to bypass this boundary.

With the service configuration sourced on the designated Mini, set the reviewed
`FLEET_COMMODORE_RELEASE_DIR`, config and state paths, then run its service-owned
Python with `intake_cutover.py --capture-legacy`. The tool holds ordinary poll
and new-watchdog locks, verifies actor absence and ordinary controller ownership
before and after each bounded, zero-timeout `getUpdates`, and atomically stores
minimized pending updates as `held_unknown` before advancing the cursor. It
never invokes a provider, starts a worker, sends a reply, deletes a webhook,
or labels a legacy question answered. An empty confirmed batch completes the
capture marker; failure or the forty-batch cap leaves the marker unset and all
committed bodies/cursor retained. Resume from that ledger, never reset it.

The old watchdog does not obey these new locks: its explicit offline pause and
independent sole-actor/worker verification are still required. Unrelated token
holders can likewise race the snapshots; no stronger exclusivity is claimed.
Held legacy requests need separate read-only inspection and independent exact
reply-target/positive-receipt reconciliation. Newly arriving work after capture
uses ordinary queued intake. Capture completion is not request completion or
permission to replay an old question. Restore only the registered new watchdog
after the immutable source/config switch and startup verification.

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
page the operator or supervise a stuck router by itself.

## Independent outcome paging

`outcome_watch.py` reads intake counts, unresolved age, latest receipt-backed
resolved time and poll/router metadata with SQLite `mode=ro`. It never reads
stored message payloads, invokes a model, changes request state or retries an
answer. Missing/unreadable intake, stale polling, a dead router, held outcomes
and work unresolved over 120 seconds are separate fixed problem classes. Counts
include all admitted updates, not only direct hails; an alert does not claim
every stored update is an unanswered question. Latest resolved time is an
operational receipt, not a semantic audit of answer quality.

Poll freshness is recorded only after a confirmed `getUpdates` batch is durably
admitted. A timer cannot renew it. Router liveness is a separate observation:
a blocked but live routing thread still leaves work overdue and alertable.
The existing `cron/qa-healthcheck.sh` inspects these outcomes independently of
the daemon, even when dependency readiness fails. Default mode is local-only.
Its explicit `--page` mode must be registered truthfully before enabling that
mode in the existing Mini cron row; construction does not install it.

Paging uses the pinned positive `OPERATOR_DM_USER_ID`, never an arbitrary admin
or fallback room. One plain-prose HTML operator alert is claimed in private
SQLite WAL/FULL state before its single POST. Acceptance requires `ok=true`,
a positive non-boolean integer message ID and the exact destination. Explicit
4xx refusal is failed; malformed, timed-out, 5xx or otherwise uncertain results
remain unknown. A prepared, failed or unknown attempt suppresses further sends
for that incident, even beyond six hours. Accepted alerts may repeat after six
hours. A genuinely healthy inspection closes the incident without sending;
later distinct incidents can page again. No automatic receipt reconciliation
or request replay is provided.

An OS-held lock serializes paging runs; a fifteen-second main-thread wall timer
bounds its transport and no token enters subprocess argv or logs. Non-Fleet or
unreadable controller ownership suppresses paging before credentials or writable
alert state are opened. This is an interim ordinary-release boundary, not proof
of the still-disabled full helm send protocol.

The hourly heartbeat classifier likewise requires a positive message receipt;
`ok=true` alone is not accepted. Only an explicit non-boolean 4xx code marks
rejection. Its existing six-hour notification dedup policy remains unchanged.

With the existing five-minute cron cadence, an overdue classification pages
on the next watchdog invocation; it does not meet a 120-second end-to-end answer
or alert SLA. Prompt backlog acknowledgements, bounded terminal outcomes, held
receipt reconciliation and independent useful-answer verification remain gates.

## Ordinary process supervision

The existing five-minute `cron/watchdog.sh` delegates to `fleet_watchdog.py`.
It checks process PID/ancestry, exact script source plus working directory, and
tmux pane state. A sole actor from the expected release in its live pane is
process-healthy, not necessarily answering questions. A dead pane with no actor
may be respawned without `-k`; a missing window/session may be created. A live
shell without an actor is held for inspection, never killed or overwritten.
Foreign, detached or duplicate actors and failed/ambiguous probes fail closed.
The launcher now uses absolute script argv; the dormant helm observer recognizes
both that form and the legacy relative form without activating the controller.

An OS-held watchdog lock serializes cooperating invocations. A durable private
SQLite restart budget allows at most three start attempts per fifteen minutes,
at least five minutes apart. Failed/uncertain starts consume budget; a provider
auth/quota failure never causes a live process restart. The watchdog rechecks
its observation and ownership before starting. It cannot prevent an unrelated
token holder from starting after that snapshot; cutover still needs independent
sole-actor verification and the ordinary poll-owner lock.

The controller ledger is read with SQLite `mode=ro`, never initialized by this
watchdog. Any non-Fleet lease (including an expired one) suppresses ordinary
restart: only controller reconciliation may fail it back. Unreadable ownership
also holds. `--inspect` observes without acquiring a lock, writing restart state
or starting a process. A reported start action is not a readiness receipt.

Remaining gates include receipt reconciliation for held/legacy requests, bounded
end-to-end deadlines and prompt acknowledgement under backlog, registered live
outcome-watch activation, live process-supervision rehearsal and controller
ownership. The generic storage API is not a license to admit public-room models
or persist arbitrary private updates. This source is not live readiness proof.

## Verification boundary

Local fixtures cover batch rollback, reopen/dedupe, capacity, claim races,
token fences, private creation, connection lifetime, positive receipt checks,
safe HTML rejection fallback, uncertain outcomes, concurrent job sends and boot
recovery. Provider/Telegram calls are mocked. None proves live service behavior,
fresh useful replies, single-owner takeover or the required unattended soak.
Helm remains a separately reviewed and rehearsed deployment gate.

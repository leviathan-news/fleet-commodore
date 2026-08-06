# Mini-local helm controller runbook

## Purpose

The controller prevents Fleet and a temporary Sol worker from replying through
the same Telegram identity at the same time. It also keeps intake and task
state on the Mini when the reasoning worker or its client session disappears.

The ordinary Fleet release remains the fallback. The successor release owns
Telegram polling only while the durable reply lease names Sol. Fleet code in
that successor queues updates but cannot route or send them. Sol is a worker,
not the owner of the cursor, queue, or conversation state.

## Durable state

Keep these files in a service-owned directory outside every Git release:

- `controller.db`: Telegram offset, stable events, context, reply lease, send
  fences, transitions, and verification receipts.
- `sol.token`: current exclusive-lease capability, mode `0600`.
- `runtime.lock`: process and cron handoff lock.
- `cron-handoff.json`: exact ordinary watchdog row and its controller
  replacement.

The controller database uses SQLite WAL mode with full synchronous commits.
Each Telegram update and the next offset are committed in one transaction
before routing. Stable identity is `telegram:update:<update_id>`, with a
chat/message or payload-hash fallback for synthetic tests.

## Actors and leases

There is one `reply_lease` row:

- `fleet`: only the pinned ordinary Fleet process may answer.
- `sol`: the successor process polls and queues; Fleet sends are denied. Sol
  sends require the current capability token.
- `transition`: all controlled sends are denied during process cutover.
- `coverage_lost`: no controlled actor may answer; raise the coverage alert.

Exclusive health requires three independent deadlines:

- Sol model lease, renewed only after the reasoning worker is observed.
- Watcher lease, renewed only after a successful Telegram long poll.
- Bridge lease, renewed by the queue-to-worker bridge process.

The recurring Mini watchdog reconciles the durable lease with process outcome.
It does not infer model health from a timer.

## Blue/green sequence

1. Verify one process from the pinned ordinary release and one ordinary
   watchdog cron row.
2. Build the successor from a clean, reviewed commit. Run the full test suite
   and `scripts/helm_controller_fault_test.py` in an isolated namespace.
3. Store passing `isolated_sol_bridge_loss` and `isolated_watcher_loss`
   receipts in the live controller database.
4. Import the current watch state, including the cursor, open tasks, promises,
   and incident evidence.
5. Run `helm_supervisor.py takeover`. It reconciles legacy Fleet history,
   replaces only the exact ordinary watchdog row, stops Fleet, acquires the Sol
   lease, and starts the controlled successor. A failure in this sequence
   restores the pinned release.
6. Start `helm_sol_bridge.py` with the current token. The bridge emits claimed
   queue events locally and renews only its own lease.
7. Renew the Sol lease from the live reasoning session. Do not let a watchdog
   or bridge renew it.

## Sending and deduplication

Fleet's `send_message` creates a prepared send fence before Telegram I/O. A Sol
reply uses `helm_controller.py send` with the event ID and current token. An
accepted or ambiguous attempt prevents another send for the same event.

An ambiguous network outcome is held as `outcome_unknown`; do not retry it.
Telegram's explicit HTML rejection may fall back once to plain text because
the rejected request had no external side effect.

After a Fleet failback, run legacy-history reconciliation before a second Sol
takeover. This marks queued events already routed by Fleet, so the restored Sol
worker cannot answer them again.

## Fault response

When any exclusive lease expires, the supervisor:

1. moves the reply lease to `transition`;
2. stops the successor poller;
3. starts the exact pinned ordinary Fleet release;
4. verifies the running process by its working directory;
5. marks Fleet as reply holder;
6. restores the exact ordinary watchdog row.

The controller database is not replaced or rewound. Its Telegram cursor,
queued events, claims, imported task state, and failed-handoff receipts remain
available for investigation and a second takeover.

If Fleet cannot be verified, the controller enters `coverage_lost`. Send one
truthful coverage alert through the approved emergency path and do not claim
that the helm remains active.

## Stand-down

Use supervisor failback, verify one pinned Fleet process and one ordinary
watchdog row, stop the Sol bridge and any persistent supervisor window, then
confirm reply capability. Preserve the database, token history, cron handoff
record, and unknown send outcomes. Do not delete incident evidence during
stand-down.

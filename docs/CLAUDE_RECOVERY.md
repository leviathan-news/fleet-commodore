# Claude outage recovery

Fleet conversation, Q&A and build/review currently require Claude. Installing
Codex does not add a fallback provider. The dormant helm controller is outside
this recovery slice and must not be enabled as an authentication workaround.

## Re-authenticate and verify

On the service host, use the service account's interactive Claude subscription
login (`claude auth login --claudeai`, or `/login` inside Claude Code). Complete
browser consent there. Do not copy credentials into a release, repository or
chat, and do not switch to API billing to work around an outage.

Credential-file existence and `claude auth status` do not prove that a token
works. Run a bounded no-post provider probe from the actual runtime environment,
then verify a fresh trusted-room hail and a substantive Q&A request using their
Telegram reply receipts. Check the worker container separately: its staged auth
and egress environment can differ from the host. Do not replay historical user
requests to obtain a success receipt.

The chat breaker now opens after the first CLI timeout and suppresses immediate
retries. Its next recovery probe is eligible after 600 seconds by default, when
a call checks availability. This is demand-driven recovery, not a background
promise to recover exactly ten minutes after login. Successful probing clears
the breaker without a restart. The first hung chat call still has its normal
120-second deadline; asynchronous chat ingestion is future work.

## What the checks mean

The hourly heartbeat alerts immediately for an explicit authentication failure,
or after three consecutive non-OK probes of any class. Thus an unclassified
hang can take three scheduled probes to page, while an explicit revoked-token
fixture should page on its first probe. Success resets the failure count.
Six-hour alert suppression and failure state live in the service state
directory outside the immutable release. Accepted, rejected and uncertain
Telegram deliveries must remain distinguishable.

The no-network QA readiness report includes `provider_transport: not_checked`
and a warning when the credential file is older than seven days. Age is only a
heuristic: old credentials may work and fresh ones may already be revoked.

An explicit Q&A worker failure is reported as `worker_failed`, with the worker's
known fixed failure reason and a bounded diagnostic category inferred from its
output. Arbitrary reasons and raw excerpts are not copied into operator alerts
or the durable failure record. Unknown output remains unparseable rather than
being guessed to be an authentication error.

## Release and acceptance

Follow the README immutable-release contract: reviewed clean source, manifest,
external configuration/state/interpreter, one supervised actor, and a retained
rollback release. Do not run a second Telegram poller for a smoke test. Keep
the ordinary watchdog's respawn behavior in mind before any manual takeover.

Local regressions cover timeout and recovery, actual bounded fake-CLI hangs,
QA failure recording/paging, heartbeat streaks/delivery suppression and
credential-age warnings. Passing these does not establish live recovery. Keep
the incident open until authenticated runtime probes and fresh useful replies
have been verified. Codex resilience and supervised helm recovery follow as
separate slices after ordinary operation is restored.

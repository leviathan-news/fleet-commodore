# Provider outage recovery

Conversation and read-only Q&A default to Codex (`gpt-5.6-luna`, medium
reasoning), using the service account's cached ChatGPT subscription login.
Claude authentication is not a prerequisite for these routes. Build/review and
security triage remain Claude-dependent; this release does not migrate them.
The dormant helm controller must not be enabled as an authentication workaround.

## Codex authentication and evidence

Codex is fixed at `/opt/homebrew/bin/codex`. It runs with forced ChatGPT login,
an ephemeral empty workspace, tools/apps/MCP/hooks disabled, no inherited API
keys or application credentials, and a bounded process group. For a new login,
use `codex login --device-auth` on the service host and complete consent in a
browser. Never switch to API billing as an outage workaround.

Run `bin/provider-probe.py` with the service-owned Python interpreter: an exact
neutral marker is required for success. It bypasses cached cooldown state and
does not send Telegram. `codex login status` alone is not a transport proof.

Codex chat calls are bounded to 60 seconds. Failures open an external SQLite
health cooldown: ten minutes for authentication/quota, one minute otherwise.
Subsequent calls fail promptly with an honest outage reply and deduplicated
operator alert. Recovery after cooldown is demand-driven. Ordinary durable
intake polls independently of the FIFO routing worker; provider calls do not
block Telegram polling. Supervised helm remains a separate, dormant mode.

Codex itself has no tools. Q&A uses a host broker with at most four model rounds
for an ordinary turn, or eight when recent-room context or an attachment needs
additional resolution, within the same 225-second deadline; each call is capped
at 55 seconds. Typed requests can search/read the existing allowlisted
documentation, read bounded GitHub or verified same-room Telegram evidence, or
retrieve a named X experiment's current per-arm outcome report. The experiment
report aggregates only posted analysis-eligible receipts with collected nominal
+24h outcomes, and exposes missing and late counts. The model interprets and
cites those measurements. General SQL requests still run through the existing
read-only `commodore-db` wrapper in a
disposable reviewer container. Database credentials go only to that container,
never to the model process. Queries retain the reader role, sensitive-table
denylist, three-second statement timeout and 500-row cap; container
output/lifetime are capped at 128 KiB/15s.

In a room with read-only Q&A access, direct messages that fall past the
specialized ship/review/plan routes enter this same grounded LLM lane even
without a question mark. A follow-up that supplies an outcome card therefore
keeps its verified request context and evidence tools; the model decides
whether the message needs a factual answer or ordinary conversation.

Answers must cite identifiers actually retrieved by the broker. This rejects
invented source identifiers, not every possible misinterpretation of evidence.
SQL evidence carries an observation time; a document mtime is not deployment
proof. Attachment review can use the same bounded host broker as text Q&A; it
does not gain model-native tools or direct access to credentials. General shell,
ORM execution, web fetching, and direct external actions or writes remain
unavailable on the Codex Q&A route.

## Explicit legacy Claude route

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

The legacy chat breaker opens after the first CLI timeout and suppresses immediate
retries. Its next recovery probe is eligible after 600 seconds by default, when
a call checks availability. This is demand-driven recovery, not a background
promise to recover exactly ten minutes after login. Successful probing clears
the breaker without a restart. The first hung chat call still has its normal
120-second deadline; asynchronous chat ingestion is future work.

## What the checks mean

The hourly heartbeat probes the selected conversation provider, not both. A
broken Claude login does not mark a healthy Codex route down. It alerts
immediately for an explicit authentication failure,
or after three consecutive non-OK probes of any class. Thus an unclassified
hang can take three scheduled probes to page, while an explicit revoked-token
fixture should page on its first probe. Success resets the failure count.
Six-hour alert suppression and failure state live in the service state
directory outside the immutable release. Accepted, rejected and uncertain
Telegram deliveries must remain distinguishable.

Each probe writes only its redacted timestamp, state, exit status, and output
length both to the service-state heartbeat log and to stdout. The latter is
intentional: cron's registered wrapper log must show the real probe freshness,
without exposing provider output or credentials.

The no-network QA readiness report includes `provider_transport: not_checked`
and, on the legacy Claude route, a warning when credentials are older than
seven days. Age is only a
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
have been verified. Record exact artifact SHA, read-only SQL/document/attachment
runtime checks, and new Telegram receipts separately. Legacy Claude-dependent
jobs and supervised helm recovery remain outstanding, not implicitly repaired.

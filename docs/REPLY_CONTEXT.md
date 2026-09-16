# Reply context contract

The current request is authoritative. A correction in that request overrides
an earlier parent's mistaken interpretation.

Telegram supplies the direct `reply_to_message`, without nested reply parents.
If the update contains `quote`, only its selected `quote.text` becomes the
direct referent. A malformed or empty selected quote fails closed: it must not
expand into the complete parent or a locally stored copy. Without a selected
quote, the direct parent's supplied text takes precedence over a stale local
copy.

Older parents are recovered only by following exact `reply_to_msg_id` edges in
the local `chat_history` ledger. Every lookup stays within the same chat and
forum topic. Cycles, missing edges, invalid IDs, and unavailable ledgers stop
the walk; chat recency is never a substitute. Context contains at most four
parents, each with at most 500 sanitized text characters and a bounded sender
label. Replies do not use the ambient recent-message buffer as model context.

Accepted bot replies and incoming replies persist their exact edge. Startup
adds nullable `chat_history.reply_to_msg_id` and `qa_job.reply_context_json`
columns idempotently; historical rows remain valid and are not guessed or
backfilled. Q&A stores the selected context with the claimed job and forwards
it as untrusted data to the configured provider. Both Q&A providers put that
untrusted context before a separately labelled, final current question, so a
terse correction cannot be overridden by a parent's stale concrete referent.

When a reply exists but no safe referent can be recovered, Fleet asks the
requester to name the change or question rather than making a provider call
with a guessed subject. Document-review intake remains separately governed by
its attachment contract.

## Verification

`tests/test_reply_chain_context.py` covers protocol-realistic direct-parent
updates, durable incoming/outgoing edges, nullable migration compatibility,
current corrections, chat/topic isolation, selected-quote bounds, malformed
selected quotes, and forwarding through conversation and persisted Codex Q&A.
All provider and Telegram calls in these tests are mocked. They do not prove
live provider or production behavior.

Release acceptance additionally requires a clean reviewed immutable Mini
artifact, the existing singleton launcher and service-owned state/config,
and a live conversation plus grounded Q&A reply/correction sequence with
positive Telegram receipts. Keep the predecessor for rollback. This change
does not activate helm control, change model authentication, repair legacy
Claude build/review, or authorize publication or production data mutations.

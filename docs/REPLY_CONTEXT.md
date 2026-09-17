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
forum topic. An exact thread root is also valid when its message ID equals
the current thread ID and the root has no topic ID: Telegram uses this shape
for ordinary supergroup reply threads. This exception never admits another
NULL-topic message, a different explicit topic, or another chat.
Cycles, missing edges, invalid IDs, and unavailable ledgers stop
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

When a reply exists but no safe referent can be recovered, Fleet supplies that
absence as a host observation to the LLM. Q&A persists the bounded
`{"context_status":"unavailable"}` marker in its existing context field;
providers separate that metadata from quoted ancestors. Ambient history stays
excluded. The model answers a self-contained current request normally, or asks
for the subject in its own words if it depends on the missing parent. It must
not guess from unrelated evidence or memory. Document-review intake remains
separately governed by its attachment contract.

Photo and image-document parents retain a bounded image-presence marker and
their caption in context/history, never file IDs or image bytes. The marker
explicitly says pixels are unavailable. A text-only worker uses the caption
and request, and asks for a page URL or description when required. Selected
quotes remain limited to the selected text, including on image messages.

Authorized conversational hails enter the normal model-job path. There is no
presence phrase matcher, deterministic conversational reply, or intent-to-stock-
text mapping. The LLM interprets the current request and writes its own response.
For ordinary conversation, Codex Q&A returns `status=conversational` and an
`answer`; the host delivers that model-authored text under the existing send
contract. Conversation needs no external citation and does not attest provider
or fleet-wide health. The conversational result cannot review attachments,
carry citations or extra fields, or replace a completed evidence lookup.
Substantive questions still use the ordinary grounded answer contract,
including when paired with a conversational hail. This distinction uses model
judgment, not a host keyword classifier or proof of semantic correctness.
Mixed hails remain grounded Q&A: pronouns such as "that" use the quoted parent
as their subject; only an actual correction overrides it. The Q&A prompt
supplies host identity separately from untrusted context. Identity is not a
citable source for analytics or deployment claims. Missing evidence yields a
plain, subject-specific limitation or clarifying question, without a ceremonial
refusal prefix. No citation requirement for substantive answers is relaxed.

## Current evidence and source links

The model chooses between current observations and reference documents.
`github_pulls` reads the five newest-created or recently-updated PRs for an
allowlisted Fleet repository; `github_pull` reads one numbered PR. Both are
host-side GETs to the fixed public GitHub API, without credentials, redirects,
shell execution or new model tools. Responses are size/time bounded and include
canonical source URLs, observation time, title/body excerpts and merge state.
Closed does not imply merged, and neither state proves deployment. A listing
is a bounded sample, not exhaustive repository history or a merge-time ranking.
Unqualified Leviathan PR questions default to `leviathan-news/squid-bot`; the
model must state the repository scope. An explicitly named repository wins.

Current/latest PR questions require this current source rather than local
document search. Documents are labelled as reference material. An answer using
`basis=current` without a cited current GitHub/SQL observation is returned to
the model for correction within the existing four-step budget. The semantic
choice still belongs to the LLM; this provenance check is not a keyword filter
or proof that every factual assertion is correct. If live retrieval fails,
the model must explain that limitation, not substitute old documented activity.
Attachment review cannot request GitHub evidence.

The host formats retrieved PR sources as labelled GitHub links and repository
document paths as `blob/main` links through the existing Markdown-to-Telegram-
HTML renderer. Document links provide navigation to the current repository
file, not proof that a local excerpt matches main. Source URLs stay complete
when the body is shortened for the sender's raw and visible length bounds.

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

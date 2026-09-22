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
the local `chat_history` ledger. Every lookup stays within the same chat and,
when Telegram sets `is_topic_message=true`, the same forum topic. Telegram's
[Message contract](https://core.telegram.org/bots/api#message) also uses
`message_thread_id` for ordinary reply threads; without `is_topic_message`,
that ID is a reply root and does not partition a normal supergroup into forum
topics. An exact thread root remains valid when its message ID equals the
current thread ID and the root has no topic ID. This exception never admits a
different true forum topic or another chat.
Cycles, missing edges, invalid IDs, and unavailable ledgers stop
the walk; recent room messages never substitute for a quoted edge. Quoted
context contains at most four
parents, each with at most 500 sanitized text characters and a bounded sender
label.

An unquoted Q&A follow-up may use a separate snapshot of up to six prior
messages from the same trusted chat and in-memory thread key, including an
exact forum topic when `is_topic_message=true`, spanning no more than 24 hours
and 2,000 sanitized text characters. The current message is
excluded even though routing has already appended it to the in-memory buffer.
Messages from bots remain eligible because Fleet receipts often name the
subject. Cross-chat, cross-topic, future-dated, undated, and older messages
fail closed. Attachments and any message with an explicit reply parent receive
no recent-room context.

This recent context may identify an ambiguous subject and supply retrieval
keywords. It is never factual evidence, an instruction source, authority, or
permission. The LLM still chooses the appropriate read-only retrieval and
writes the answer. The current question remains authoritative; an exact reply
chain takes precedence over recent-room context, including when the exact
parent is unavailable. There is no deterministic subject or answer matcher.

A separate request-continuity snapshot may contain up to eight prior direct
requests from the same Telegram actor and trusted chat, spanning no more than
24 hours and 8,000 sanitized characters. True forum messages remain confined
to their topic. Ordinary non-forum reply roots do not divide the room, which
allows a terse reply to a newly supplied document to retain an earlier task.
Each entry carries its verified actor ID and `authorization=none`: inferred
history helps interpret the current turn but never grants write authority.
The current request still wins over any older task.

Q&A may also receive at most six metadata-only document candidates from an
exact reply chain or recent verified room scope. Each candidate includes its
message ID, actor provenance, bounded filename/type/size metadata, relationship,
and `read_only=true`; Telegram file IDs and contents do not enter the model
prompt. The model chooses a supplied message ID through the
`telegram_document` broker request. The host then rechecks the claimed job,
chat, true forum topic when present, candidate allowlist, and actor provenance
before performing the existing bounded text-document download. Fleet never
fetches an arbitrary ambient file.

Accepted bot replies and incoming replies persist their exact edge. Startup
idempotently adds reply, actor, direct-address, true-forum, and private
document-reference fields to `chat_history`, and reply, recent-room, request,
known-document, and true-forum context fields to `qa_job`. These fields apply
to observations made after migration. Historical rows remain valid; missing
actor IDs, direct-address evidence, forum classification, and document
references are not invented or backfilled from usernames. Q&A snapshots
context with the claimed job and forwards each supported context class
separately as untrusted data. The Codex broker puts that untrusted context
before a separately labelled, final current question, so a
terse correction cannot be overridden by a parent's stale concrete referent.

When a reply exists but no safe referent can be recovered, Fleet supplies that
absence as a host observation to the LLM. Q&A persists the bounded
`{"context_status":"unavailable"}` marker in its existing context field;
providers separate that metadata from quoted ancestors. Recent room context
also stays excluded whenever an explicit reply exists. The model answers a
self-contained current request normally, or asks
for the subject in its own words if it depends on the missing parent. It must
not guess from unrelated evidence or memory. Document-review intake remains
separately governed by its attachment contract.

Photo and image-document parents retain a bounded image-presence marker and
their caption in context/history, never file IDs or image bytes. The marker
explicitly says pixels are unavailable. A text-only worker uses the caption
and request, and asks for a page URL or description when required. Selected
quotes remain limited to the selected text, including on image messages.

Text-document intake accepts the configured UTF-8 text types and ZIP bundles
of those types. Plain text and combined readable ZIP output use the 128 KiB
default `TELEGRAM_TEXT_DOCUMENT_MAX_BYTES` budget, configurable only up to the
hard 256 KiB ceiling. ZIP downloads are capped at 4 MiB compressed and 256
entries, decoded in memory without extraction. Unsafe, binary, unsupported,
encrypted, duplicate, corrupt, or over-budget members receive explicit skip
notes; readable members remain available with partial-coverage disclosure.

Attachment text remains untrusted evidence. Model-native tools, shell,
filesystem, and unrestricted network access stay disabled, while the bounded
host broker may still perform repository search/read, read-only SQL, GitHub
reads, and a model-selected verified Telegram document lookup. If the user
asks to update Beads or GitHub, `tracker_propose` may save an immutable
proposal-only record for `leviathan-news/squid-bot`. It never invokes `bd`,
writes GitHub, confirms acceptance, or starts implementation. The read-only
local inspection/export surface is
[`bin/tracker-proposals`](../bin/tracker-proposals). No automatic canonical
consumer is implemented, so Fleet reports application as pending and never
claims the tracker changed.

Authorized conversational hails enter the normal model-job path. There is no
presence phrase matcher, deterministic conversational reply, or intent-to-stock-
text mapping. The LLM interprets the current request and writes its own response.
For ordinary conversation, Codex Q&A returns `status=conversational` and an
`answer`; the host delivers that model-authored text under the existing send
contract. Conversation needs no external citation and does not attest provider
or fleet-wide health. An attachment does not invalidate ordinary conversation
or a needed clarification. A substantive attachment request gets a corrective
model turn if it initially yields only conversation. Conversational results
cannot carry citations or extra fields, or replace a completed evidence lookup.
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
host-side GETs to the fixed GitHub API. Private repositories use the existing
Fleet `GH_PAT_FILE` credential (default `~/.config/commodore/gh_pat`) only in the
host HTTP header. No credential enters a model prompt/environment or result;
redirects, shell execution and native model tools remain unavailable.
Responses are size/time bounded and include
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
Attachment review may request the same bounded host evidence when the user's
task needs current project facts; attachment text itself remains reference
material rather than proof of current state.

Q&A uses the installed Codex CLI's `--output-schema` contract to enforce the
message structure, including integer PR numbers. The schema is written only
to the provider's temporary root and removed with it. The model still chooses
the operation and writes the answer; the broker independently validates
authorization and evidence. Other text-generation callers remain unchanged.
Malformed model JSON is returned with parser feedback for correction within
the same four-step budget. Empty literal searches suggest fewer subject words.
No requested operation executes until its JSON and parameters are valid;
transport failures retain the existing failure handling.

The host formats retrieved PR sources as labelled GitHub links and repository
document paths as `blob/main` links through the existing Markdown-to-Telegram-
HTML renderer. Document links provide navigation to the current repository
file, not proof that a local excerpt matches main. Source URLs stay complete
when the body is shortened for the sender's raw and visible length bounds.

## Verification

`tests/test_reply_chain_context.py` covers protocol-realistic direct-parent
updates, durable incoming/outgoing edges, nullable migration compatibility,
current corrections, chat/topic isolation, selected-quote bounds, malformed
selected quotes, bounded unquoted same-room subject resolution, and forwarding
through conversation and persisted Codex Q&A.
`tests/test_document_reply_continuity.py` covers same-actor task recovery across
ordinary reply roots, true forum isolation, metadata-only document selection,
actor provenance, and the separate worker payload fields.
`tests/test_document_intake.py` covers bounded plain-text and ZIP decoding,
member safety, partial coverage, and archive limits.
`tests/test_tracker_proposals.py` covers validation, idempotency, scope-bound
inspection, and proposal-only state.
All provider and Telegram calls in these tests are mocked. They do not prove
live provider or production behavior.

Release acceptance additionally requires a clean reviewed immutable Mini
artifact, the existing singleton launcher and service-owned state/config,
and a live conversation plus grounded Q&A reply/correction sequence with
positive Telegram receipts. Keep the predecessor for rollback. This change
does not activate helm control, change model authentication, repair legacy
Claude build/review, or authorize publication or production data mutations.

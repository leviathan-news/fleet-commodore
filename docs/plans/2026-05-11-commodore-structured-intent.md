# Commodore Structured-Intent Extraction (Replacing the Regex)

## Context

The Commodore's `_PR_REQUEST_RE` is fundamentally the wrong primitive. Today (2026-05-11) it missed:

1. **"Can you make a pr to fix it?"** — original verb form not in the alternation, fell to LLM persona which hallucinated a refusal
2. **"try filing a PR to simply kill it from the header"** — gerund form not handled, fell to LLM persona which hallucinated `Aye, drawing up the dispatch` with no actual build pipeline behind it

Each miss = one regex expansion + one Commodore restart. The audience as we widen this bot is editors, not devs:

- *"robot fix this"*
- *"@commodore the auctions link is broken, can you sort it"*
- *"this typo in the masthead is killing me"*
- *"kill auctions from the nav"*
- *"remove the auctions thing"*

None contain "pr" or "pull request". A keyword pre-filter is dead on arrival. **Every addressed message in Lev Dev needs an intent decision**, and only Sonnet can read editor-speak reliably.

Critically, the LLM is ALREADY called for every reply via `generate_response()`. The bug is that the LLM and the dispatch pipeline are decoupled — the LLM says "aye, dispatch under quill" and posts it, while the dispatch pipeline never knows it was supposed to fire. We need to **couple the reply text and the dispatch decision into a single LLM call** so the bot literally cannot promise to file a PR without actually queueing the build_job.

## Goal

Replace `_PR_REQUEST_RE` + the post-hoc `_detect_pr_request()` check with **structured-intent extraction** from the existing Sonnet reply call. The same model that writes the in-character reply also emits the intent classification and the build payload — so the reply CAN'T promise something the pipeline didn't claim.

## Design

### Single LLM call, structured JSON envelope

Today `generate_response()` returns a string. New contract: the LLM emits a JSON envelope. The bot parses it, posts `reply`, and dispatches according to `intent`.

```jsonc
{
  "intent": "file_pr" | "review_pr" | "qa" | "chat" | "skip",
  "reply": "Aye, Admiral — the Auctions standard shall be struck...",
  "build": {                          // only when intent == "file_pr"
    "target_repo": "leviathan-news/auction-ui",
    "summary": "Hide Auctions link from masthead nav",
    "details": "Flip APP_LINKS.auctions.isShow to false in constants/app.ts. Mirror the isShow guard in HeaderMobileMenu.tsx so mobile honors it too.",
    "rationale": "From operator @gerrithall in Lev Dev — Auctions may be deprecated; hide nav link pending Admiralty deliberation."
  },
  "review": {                         // only when intent == "review_pr"
    "pr_number": 98,
    "repo": "leviathan-news/auction-ui"
  }
}
```

Why this shape:
- **`intent` is the routing decision.** Bot does NOT independently route — it trusts the LLM.
- **`reply` is the only thing posted to Telegram.** Always present (even for `skip` it's empty string).
- **`build` carries the dispatch payload.** If `intent == "file_pr"` and `build` is missing/invalid → reject; fall back to a `qa` decline ("forgive me, the dispatch lacked sufficient detail to draft — pray restate the order").
- **`details` is freeform plan-text** that the build worker passes into Claude CLI as the implementation prompt. Editor doesn't need to write dev-spec.

### Where to wire it

`generate_response()` at `commodore.py:1821`. Today the system prompt produces prose. Add to the prompt:

```
RESPONSE FORMAT — STRUCTURED ENVELOPE
You MUST emit a single JSON object as your entire response. No prose
outside the JSON. Schema:

  intent: one of "file_pr" | "review_pr" | "qa" | "chat" | "skip"
  reply:  string (your in-character reply to post to Telegram;
          empty string for "skip")
  build:  object (REQUIRED when intent="file_pr", FORBIDDEN otherwise)
    target_repo: "leviathan-news/<repo>"
    summary:     one-line plain-English description of the change
    details:     2-5 sentence implementation plan in plain English
    rationale:   why and who asked
  review: object (REQUIRED when intent="review_pr", FORBIDDEN otherwise)
    pr_number: integer
    repo:      "leviathan-news/<repo>"

INTENT RULES
- "file_pr": the speaker is asking for a code/config/doc change that
  could be expressed as a pull request. EXAMPLES from editors (not devs):
    "robot fix this typo"
    "can you kill the auctions link"
    "the sponsor page is broken — sort it"
    "make a pr to update the FAQ"
    "remove that ad placement on /sponsor"
  Only emit "file_pr" when authorized — see CAPABILITIES.
- "review_pr": the speaker is asking you to read or comment on a specific
  PR (URL or "#NNN").
- "qa": the speaker is asking a question about the Fleet, the codebase,
  metrics, or the news corpus. Answer with the "reply" field.
- "chat": casual banter, greetings, acknowledgements. Short reply or SKIP.
- "skip": you have nothing useful to add; the bot will post nothing.

AUTHORIZATION CARRIES (do NOT promise file_pr from unauthorized rooms):
You will be told whether the speaker is "[authorized to order dispatches
in this room]" by their sender_label. If that marker is absent:
- intent="file_pr" is FORBIDDEN regardless of the request
- intent="review_pr" requires the same authorization
- explain in "reply" why you cannot dispatch
```

### Routing on the bot side

```python
def _route_response(envelope: dict, msg: dict, policy: dict) -> Optional[str]:
    """Dispatch the LLM's intent + return the reply text to post."""
    intent = envelope.get("intent", "skip")
    reply = envelope.get("reply", "") or None  # None == post nothing

    if intent == "file_pr":
        if not _can_ship(msg):
            log.warning("LLM emitted file_pr from unauthorized chat — dropping")
            return reply  # post the reply but don't dispatch
        build = envelope.get("build") or {}
        if not _validate_build_payload(build):
            return "Forgive me — the dispatch wanted detail. Pray restate the order."
        _enqueue_plan_from_envelope(msg, build, reply)
        return None  # reply will be posted by the build pipeline ack

    if intent == "review_pr":
        if not _can_ship(msg):
            return reply
        review = envelope.get("review") or {}
        _enqueue_review_from_envelope(msg, review)
        return None

    # "qa", "chat", "skip" — just post the reply, no dispatch
    return reply
```

### LLM-coupled honesty

Because the reply and the intent come from the SAME LLM call:
- LLM cannot promise "aye, dispatch under quill" without setting `intent="file_pr"`
- If `intent="file_pr"` is rejected (auth fail, malformed build payload), the bot OVERRIDES the LLM's reply with a corrective message — the operator never sees the false promise
- The persona prompt's "NEVER pretend to have performed an action" (existing line 1797) becomes structurally enforceable, not just an instruction

### Fallback strategy

If JSON parse fails:
1. Strip code-fence wrappers (```json ... ```)
2. Try parsing the largest `{...}` substring
3. If still fails: log warning, treat as `intent="chat"` with the raw LLM output as `reply` (Sonnet sometimes returns plain prose on retry; better to ship the prose than nothing)
4. Keep the (gerund-tolerant) `_PR_REQUEST_RE` as a DEFENSIVE last resort — if the LLM call totally fails AND the regex hits, file the PR. Belt + braces for the dev-jargon case.

## Implementation Plan

### Step 1 — Update the BOT_IDENTITY persona prompt
File: `commodore.py:1755-1801`

Add the RESPONSE FORMAT, INTENT RULES, and AUTHORIZATION CARRIES sections from the design. Keep the existing pirate-voice + decline directives.

### Step 2 — Add envelope parser + validator
File: `commodore.py` (new helpers near `generate_response`)

```python
def _parse_intent_envelope(raw: str) -> Optional[dict]:
    """Extract the JSON envelope from the LLM response. Returns None if
    unparseable — caller falls back to chat-intent with raw text."""
    # strip code fences, find first { ... last }, json.loads
    ...

def _validate_build_payload(build: dict) -> bool:
    """All required fields present + non-empty + target_repo whitelisted."""
    ...
```

### Step 3 — Refactor `generate_response()` to parse + route
File: `commodore.py:1821`

```python
def generate_response(msg, is_direct, policy, recent_messages):
    ...
    raw = _claude_ask(prompt)
    envelope = _parse_intent_envelope(raw)
    if envelope is None:
        return raw  # bare prose fallback
    return _route_response(envelope, msg, policy)
```

`_route_response` calls `_enqueue_plan_from_envelope(msg, build, reply)` for `file_pr` — which is a new function that creates a `plan_drafts` row directly from the LLM's structured payload, then calls `_claim_build_job` per the existing pipeline. NO regex involved.

### Step 4 — Retire `_detect_pr_request` from the main path
File: `commodore.py:3864`

Move the `_detect_pr_request(text)` check from the main poll loop into the LLM-fallback path only. If the LLM totally fails (no envelope, no prose, exception), the regex becomes the last-resort dispatcher. In normal operation it never fires.

Keep `_PR_REQUEST_RE` as a constant for tests + the fallback. Don't delete — it's still useful for `test_refusal.py` and the dev-jargon emergency case.

### Step 5 — Tests
New file: `tests/test_intent_extraction.py`

```python
# Parser tests (no LLM call)
def test_parse_envelope_with_code_fence(): ...
def test_parse_envelope_with_prose_preamble(): ...
def test_parse_envelope_malformed_returns_none(): ...

# Routing tests (mocked envelope)
def test_route_file_pr_from_lev_dev_non_admin_dispatches(): ...
def test_route_file_pr_from_squid_cave_refuses_and_posts_reply(): ...
def test_route_file_pr_missing_build_payload_returns_corrective(): ...
def test_route_qa_returns_reply_no_dispatch(): ...

# End-to-end (mocked Claude CLI to return canned JSON)
def test_editor_phrasing_robot_fix_this(): ...
def test_editor_phrasing_kill_auctions_from_nav(): ...
def test_gerund_phrasing_filing_a_pr(): ...
```

### Step 6 — Operator runbook entry
Add to `docs/runbook/admiral-troubleshooting.md`: when the Commodore goes off-script ("promises dispatches that don't arrive"), check `logs/commodore.log` for `intent=file_pr` lines without corresponding `plan_drafts`/`build_job` rows. That's the smoking gun.

### Step 7 — Open as PR on fleet-commodore
Branch: `feat/intent-extraction`. Single PR, ~400-600 lines. Should be reviewable in 15 min.

## Critical Files

- `commodore.py:1755-1801` — BOT_IDENTITY (prompt)
- `commodore.py:1821-1920` — generate_response (return type changes)
- `commodore.py:3864-3875` — poll-loop PR-request branch (gets demoted to fallback)
- `commodore.py:2005-2008` — `_PR_REQUEST_RE` (retained for fallback + tests)
- `tests/test_intent_extraction.py` — new
- `tests/test_authorization.py` — may need a couple of envelope-aware additions

## Verification

- All existing tests still pass (246 + new intent tests)
- Live test in Lev Dev: send each of these as separate messages addressed to the Commodore. Confirm each routes correctly via a fresh `plan_drafts` row + an `Aye` reply that actually has a build behind it:
  1. *"@leviathan_commodore_bot fix the typo in the FAQ — comma after 'token'"*
  2. *"robot, kill the Auctions link from the header"*
  3. *"can you make a pr to bump README"* (dev-jargon, still works)
  4. *"hey is the bot HQ keyboard cached anywhere"* (qa intent, no dispatch)
  5. *"morning"* (chat or skip, no dispatch)

## Out of Scope

- The build worker pipeline itself (Claude CLI in a Docker egress proxy). It already exists; this change just feeds it from the LLM envelope instead of from regex-extracted text.
- Multi-turn plan refinement. Keep the existing `plan_drafts` `message_history_json` model — each turn appends an envelope.
- Replacing `_can_ship` with role-based perms. The Lev-Dev-open model from PR #1 stays.
- A "preview" mode where the LLM proposes the build payload and waits for operator approval before queueing. Could be a follow-up.

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| LLM emits `intent="file_pr"` for casual messages ("nice PR yesterday") | Persona prompt explicitly distinguishes "request" from "comment"; auth gate stops most damage; dry-run logging for first week before going live |
| LLM forgets to emit JSON envelope | Fallback to plain-text prose. Worst case: same as today, but the new regex still catches dev-jargon |
| JSON wrapped in markdown code fence | Parser strips fences |
| Cost: every addressed message now gets a structured prompt | Negligible. Already calling Sonnet; envelope adds ~50 tokens to prompt + similar to response |
| LLM hallucinates a `target_repo` that doesn't exist | Whitelist `target_repo` against a known repo list in `_validate_build_payload`; reject otherwise |
| `intent="file_pr"` with no `build` payload | Validator rejects → bot overrides reply with a corrective message |

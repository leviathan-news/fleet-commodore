# Fleet Commodore — Conversational Plan-and-Build + Read-Only Q&A (Revision 7)

## Context

This revision supersedes `eager-watching-balloon.md` (v1) after five code-review passes. v2–v5 progressively hardened durability, idempotency, scratch-file plumbing, and dedup oracle. A fifth review identified three remaining issues — two in the QA/review dedup design and one in the contract statement itself:

1. **Honest contract restatement** — v5's context claimed "no double side-effects on any pipeline" but the same revision documented a single-duplicate-at-worst crash window for QA/review (intent logged, Telegram response lost). v6 (this plan) restates the durability contract precisely per pipeline so reviewers and operators know exactly what each pipeline guarantees, and explicitly tags QA/review as best-effort-with-narrow-duplicate-window.

2. **Hidden intent-marker replaced with a first-class column** — v5 smuggled a zero-width-space marker into message bodies for cross-Telegram dedup detection. That depends on invisible characters surviving formatting/HTML/copy-paste transformations, and on future operators remembering not to strip them. v6 replaces it with a `dedup_token` column on `outgoing_msg` (and a corresponding `last_dedup_token` snapshot per job row), so dedup state lives in the schema, not in the rendered text.

3. **`intent_id` derivation hardened** — v5's `intent_id = sha256(job_uuid + chat_id + thread_id + body[:64])` baked rendered message content into the key. If we ever change the persona phrasing or the answer template, the same logical intent gets a different key, defeating dedup. v6 derives the key from job identity + a stable `action_type` enum value (`'qa_answer'`, `'qa_decline'`, `'review_post'`, `'build_already_filed_ack'`, etc.) — content-independent.

v1 → v5 changes still hold. v6 adds: (a) explicit per-pipeline durability contract, (b) `dedup_token` column replacing the hidden marker, (c) action-type-based `intent_id` derivation. v7 adds the operational counterpart: a concrete duplicate-cleanup recipe so the "detectable post-hoc" residual is operationally addressable, not just architecturally documented.

**Intended outcome (precise contract, v6):**
- **Build pipeline: fully idempotent.** No duplicate PR is possible across any crash window. Pre-flight uses scratch-file → `gh pr list` → `outgoing_msg` log in that order; all three independently catch a prior side-effect.
- **QA pipeline: best-effort with a narrow duplicate window.** No silent skip. No duplicate when the bot received a Telegram response before the crash. **Single duplicate possible** if the daemon crashes between intent log insert and the Telegram response being persisted. Detected post-hoc via `dedup_token` lookup; user-visible duplicate is unsuppressed (we surface it rather than silently swallow).
- **Review pipeline: same as QA** (same primitives, same failure mode, same mitigation).

This is the limit achievable without Telegram API support for client-supplied idempotency tokens. Documenting it precisely is part of the v6 fix — overstating durability misleads operators.

---

## Key design changes vs. v1–v5

| Concern | v1–v3 | v4 | v5 | v6 |
|---|---|---|---|---|
| Job durability | In-memory → SQLite-backed; `_recover_jobs_on_boot()` | = | = | = |
| Idempotency | Per-job key + pre-flight + `side_effect_completed_at` | + scratch-file mount plumbed | + atomic scratch-write + `outgoing_msg` WAL | + first-class `dedup_token` column + action-type-based `intent_id` |
| Durability contract | Implicit "no double side-effects" | = | Same overstatement | **Explicit per-pipeline: build = fully idempotent, QA/review = best-effort with documented narrow window** |
| Dedup oracle for QA/review | None / Telegram scan | Telegram scan (broken) | `outgoing_msg` log + zero-width marker in body | **`outgoing_msg` log + first-class `dedup_token` column on row, no body smuggling** |
| `intent_id` derivation | N/A | N/A | Hash of `job_uuid + chat_id + thread_id + body[:64]` (content-coupled) | **Hash of `job_uuid + action_type` (stable across phrasing changes)** |
| Result-scratch atomic write | N/A | Mentioned, not specified | `.tmp` + fsync + rename + dir-fsync | = |
| Authorization | Per-action gates | = | = | = |
| Q&A container creds | No `GH_TOKEN`; separate `commodore-qa-egress` | = | = | = |
| Runbook filename | Two options → keep `PR_REVIEW_RUNBOOK.md` | = | = | = |

The v1 architecture (containerized workers, parser-gated DB wrappers, PR-not-merge, fork-and-PR pattern, REVOKE hardening on `commodore_reader`, `INJECTION_OUTPUT_PATTERNS` for output filtering, `plan_drafts` + `qa_audit` tables) and all v2 hardening carries over.

---

## Idempotency design (v3 base, v4 closes the gaps)

The principle: **every external side effect is preceded by a pre-flight check that detects "this job already produced its side effect" and short-circuits to the existing artifact instead of producing a new one.** Detection uses a stable `idempotency_key` derived from the job's stable inputs, not its UUID. v4 adds a **persistent result-scratch handoff** so the coordinator can recover the worker's output even when the launcher exited unexpectedly, and a **late-dedup-and-replay** path for QA/review that eliminates the silent-skip window.

### Result-scratch host mount (v4 plumbing + v5 atomic-write protocol)

A persistent host directory owned by the coordinator user, NOT a launcher-scoped temp dir:

- **Host path:** `~/.local/state/commodore/results/` (override via `COMMODORE_RESULTS_DIR`). Created by `_ensure_state_dirs()` in `commodore.py` startup (mode `0o700`).
- **Container path:** `/var/run/commodore-results` (read/write).
- **Per-job filename:** `<uuid>.result.json` written by the worker as soon as the side effect is irreversible (build: immediately after `gh pr create` returns 201; QA: immediately after Claude returns the answer; review: immediately after Claude returns the verdict).
- **Persistence:** The launcher does NOT delete the result file in its `finally` block — it survives launcher exit. The COORDINATOR is responsible for reading and unlinking it after recording the outcome to SQLite. On boot recovery, the coordinator scans the directory for `<uuid>.result.json` files belonging to in-progress jobs; presence proves the worker reached the irreversible point even if SQLite never got the writeback.
- **Wiring:** Each launcher (`bin/launch-review-container`, `bin/launch-build-container`, `bin/launch-qa-container`) gets one new bind-mount line in its docker-run argv:
  ```
  "-v", f"{RESULTS_DIR}:/var/run/commodore-results:rw",
  ```
  where `RESULTS_DIR = Path(os.environ.get("COMMODORE_RESULTS_DIR", "~/.local/state/commodore/results")).expanduser()`. The directory is created by the launcher with `mode=0o700` if missing (matches existing LOG_DIR pattern at launch-review-container:150).
- **Failure mode:** If the file is malformed or missing on coordinator read, fall through to the existing pre-flight (build → `gh pr list`; QA/review → `outgoing_msg` log below). The scratch file is a hot-path optimization; the pre-flights are the safety net.

### Atomic scratch-file write protocol (v5)

Workers write the scratch file using the standard write-temp + fsync + atomic-rename pattern. Every worker (`build_worker.py`, `qa_worker.py`, `review_worker.py`) implements this helper:

```python
def write_result_atomically(uuid: str, payload: dict) -> None:
    results_dir = Path("/var/run/commodore-results")
    final_path = results_dir / f"{uuid}.result.json"
    temp_path = results_dir / f"{uuid}.result.json.tmp"
    # 1. Write payload to .tmp (the temp filename CONTAINS the uuid so
    #    a crashed-mid-write file is recognizably garbage and gets
    #    swept by recovery, not parsed).
    with open(temp_path, "w") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    # 2. Atomic rename. POSIX guarantees rename(2) is atomic on the same
    #    filesystem, so a reader will see EITHER the old absence OR
    #    the new complete file, never partial content.
    os.rename(temp_path, final_path)
    # 3. fsync the directory so the rename survives a crash.
    dir_fd = os.open(results_dir, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
```

**Coordinator-side read invariants:**
- Coordinator reads ONLY `*.result.json` (not `*.result.json.tmp`). A `.tmp` file's existence is evidence of a crash mid-write; recovery sweeps and unlinks `.tmp` files older than 60s.
- Coordinator parses `<uuid>.result.json` and on `JSONDecodeError` (which should not happen given the atomic rename, but defense in depth) treats the file as missing and falls through to the secondary pre-flight (`gh pr list` for build, `outgoing_msg` log for QA/review).
- Unlink ordering: coordinator only unlinks the final `<uuid>.result.json` AFTER recording the outcome to SQLite (build: `pr_url` written; QA/review: `telegram_reply_msg_id` written). If the coordinator crashes between read and unlink, recovery harmlessly re-reads the same file and idempotently re-applies the same SQLite update.

### Outgoing-message write-ahead log (the QA/review dedup oracle, v6 hardened)

The Bot API does not let us ask Telegram "did message X ever post?" after a crash. So we log it ourselves, ordered with the side-effect commit. New SQLite table:

```sql
CREATE TABLE IF NOT EXISTS outgoing_msg (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_table TEXT NOT NULL,          -- 'qa_job' | 'pr_review' | 'build_job'
    job_uuid TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER,
    action_type TEXT NOT NULL,        -- enum: 'qa_answer' | 'qa_decline' | 'review_post'
                                       --       | 'build_already_filed_ack' | 'build_pr_landed'
                                       --       | 'build_failure_apology' | etc.
    intent_id TEXT NOT NULL,          -- sha256(job_uuid + '|' + action_type) — content-INDEPENDENT
    dedup_token TEXT NOT NULL,        -- short uuid4 hex; used by recovery to scan for orphans
                                       -- without smuggling into message body
    intent_recorded_at TEXT NOT NULL, -- BEFORE the API call (write-ahead)
    telegram_message_id INTEGER,      -- NULL until the API call returns successfully
    sent_at TEXT,                     -- timestamp of successful API return
    error TEXT,                       -- on permanent failure (4xx)
    -- v7 cleanup-bookkeeping fields (NULL during normal operation):
    cleanup_role TEXT,                -- 'canonical' | 'suppressed' (set by operator after duplicate cleanup)
    cleanup_action TEXT,              -- 'edited_to_marker' | 'deleted' | 'left_in_place_with_followup'
    cleanup_at TEXT,                  -- timestamp of cleanup
    cleanup_operator_id INTEGER,      -- Telegram user_id of the operator who ran the cleanup
    UNIQUE(job_table, job_uuid, intent_id)
);
CREATE INDEX IF NOT EXISTS idx_outgoing_msg_job ON outgoing_msg(job_table, job_uuid);
CREATE INDEX IF NOT EXISTS idx_outgoing_msg_dedup ON outgoing_msg(dedup_token);
```

Add a corresponding column to each job table to snapshot the latest dedup token (so the operator can run "show me the in-flight token for this job" without joining):

```sql
ALTER TABLE qa_job ADD COLUMN last_dedup_token TEXT;
ALTER TABLE pr_review ADD COLUMN last_dedup_token TEXT;
ALTER TABLE build_job ADD COLUMN last_dedup_token TEXT;
```

**`intent_id` is content-independent.** It is `sha256(job_uuid + '|' + action_type)`. This means: a phrasing change in the QA answer template, a persona-suffix tweak, or any rendered-body diff does NOT change the intent_id. The same logical intent (`(qa_job, this_uuid, action='qa_answer')`) gets the same intent_id forever. Two side effects with the same `(job_table, job_uuid, action_type)` is exactly what we mean by "duplicate," so this is the correct dedup primitive.

**`action_type` is a small, closed enum** declared in `commodore.py` near the existing constants:

```python
class OutgoingAction:
    QA_ANSWER = "qa_answer"
    QA_DECLINE = "qa_decline"
    QA_FAILURE = "qa_failure"
    REVIEW_POST = "review_post"
    REVIEW_FAILURE = "review_failure"
    BUILD_ALREADY_FILED_ACK = "build_already_filed_ack"
    BUILD_PR_LANDED = "build_pr_landed"
    BUILD_FAILURE_APOLOGY = "build_failure_apology"
    BUILD_PRE_FLIGHT_UNVERIFIED = "build_pre_flight_unverified"
    PLAN_REFINEMENT_TURN = "plan_refinement_turn"  # only logged for 'ship it' boundary turns
```

Each `send_message_with_wal` call takes an explicit `action_type` argument — callers cannot forget to set one (no default).

**Send-with-WAL helper:**

```python
import uuid as _uuid

def send_message_with_wal(job_table, job_uuid, action_type, chat_id, text,
                          thread_id=None, reply_to=None):
    """Idempotent send. The intent key is content-independent so phrasing
       changes don't defeat dedup. Returns the Telegram response dict, plus
       a `deduped: True` flag if the call short-circuited."""
    intent_id = hashlib.sha256(f"{job_uuid}|{action_type}".encode()).hexdigest()
    dedup_token = _uuid.uuid4().hex[:16]  # only used if we actually post

    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    # 1. Pre-flight: confirmed prior success?
    row = conn.execute(
        "SELECT telegram_message_id, dedup_token FROM outgoing_msg "
        "WHERE job_table=? AND job_uuid=? AND intent_id=? "
        "AND telegram_message_id IS NOT NULL",
        (job_table, job_uuid, intent_id),
    ).fetchone()
    if row:
        return {"ok": True, "result": {"message_id": row[0]},
                "deduped": True, "dedup_token": row[1]}
    # 2. Write-ahead: record intent (with a fresh dedup_token) BEFORE the API call.
    try:
        conn.execute(
            "INSERT OR IGNORE INTO outgoing_msg "
            "(job_table, job_uuid, chat_id, thread_id, action_type, intent_id, "
            " dedup_token, intent_recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_table, job_uuid, chat_id, thread_id, action_type, intent_id,
             dedup_token, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    # 3. Telegram POST.
    resp = send_message(chat_id, text, thread_id=thread_id, reply_to=reply_to)
    msg_id = ((resp or {}).get("result", {}) or {}).get("message_id") if (resp or {}).get("ok") else None
    # 4. Write-after.
    conn = sqlite3.connect(DB_FILE)
    try:
        if msg_id is not None:
            conn.execute(
                "UPDATE outgoing_msg SET telegram_message_id=?, sent_at=? "
                "WHERE job_table=? AND job_uuid=? AND intent_id=?",
                (msg_id, _now_iso(), job_table, job_uuid, intent_id),
            )
        else:
            conn.execute(
                "UPDATE outgoing_msg SET error=? "
                "WHERE job_table=? AND job_uuid=? AND intent_id=?",
                (str(resp)[:500], job_table, job_uuid, intent_id),
            )
        conn.commit()
    finally:
        conn.close()
    return resp
```

**Three crash windows, exactly:**

| Window | Outgoing_msg row state | Telegram state | Recovery behavior | Dup risk |
|---|---|---|---|---|
| Before intent insert | no row | no post | re-queue → fresh insert → post | none |
| After intent insert, before/during POST | row exists, `telegram_message_id IS NULL` | unknown — Telegram may or may not have delivered | re-queue → INSERT OR IGNORE (no-op) → call returns no confirmed prior, POST again | **single duplicate possible** |
| After Telegram returns | row has `telegram_message_id SET` | confirmed delivered | reconcile job row from log; do NOT re-post | none |

**Per-pipeline contract (the explicit honest restatement):**

- **Build:** the duplicate window above does NOT apply because build has a stronger external oracle. Before any user-visible side effect (the GitHub PR), the worker has called `gh pr create`. Recovery's `gh pr list` pre-flight will see that PR independently of `outgoing_msg`. Two PRs is therefore impossible. The only Telegram side effect is the in-character ack/apology, which IS subject to the duplicate window — but a duplicate ack is operationally innocuous and the user already has the PR URL either way.
- **QA / Review:** the duplicate window applies. **A single duplicate Telegram answer/review can be posted** if the daemon crashes between intent log insert and the Telegram response being recorded. We do not silently swallow the duplicate (preferred over silent skip — the user can see and ignore a duplicate; they can't see a missing answer). Detection is post-hoc via `outgoing_msg` rows where the same `(job_table, job_uuid, action_type)` has multiple successful `telegram_message_id` entries — this is a future v7 cleanup-sweeper concern, not a v6 prevention concern.

**Why no hidden body marker (v6 explicit reversal of v5):** Smuggling a zero-width-space-padded marker into the rendered message body depends on (a) Markdown/HTML parsers preserving zero-width characters, (b) the bot framework not stripping invisible whitespace before send, (c) any future copy-edit pass leaving the marker in place, (d) operators not inadvertently overwriting the suffix in persona tweaks. All four are fragile. The v6 design keeps dedup state entirely in SQLite columns: `outgoing_msg.dedup_token` (per send) and `<job_table>.last_dedup_token` (snapshot). Operators query these directly. No body smuggling.

**This is the limit achievable without Telegram-side idempotency support.** Documenting it precisely is the v6 fix.

### Per-pipeline pre-flight + writeback (v5)

**Build worker (`_process_build`):**
1. Coordinator reads the `build_job` row, builds the job JSON.
2. **Pre-flight #1 (scratch file, atomic):** if `<job_uuid>.result.json` exists, parse it. (`.tmp` files ignored — sweep after 60s.) If valid with `pr_url`: call `send_message_with_wal(action_type=BUILD_ALREADY_FILED_ACK, ...)` (idempotent), write `side_effect_completed_at`, transition to `succeeded`, unlink scratch.
3. **Pre-flight #2 (GitHub):** else `gh pr list --repo leviathan-news/<repo> --head leviathan-agent:<target_branch> --state open --json url,number` (3x retries, 2s backoff on 5xx; failure → mark row `failed` with `error='pre-flight unable to verify'`).
4. Otherwise launch container.
5. Worker writes scratch atomically (`.tmp` → fsync → rename → dir-fsync) immediately after `gh pr create` returns 201.
6. Coordinator reads scratch (preferred) or last-JSON-from-stdout (fallback); updates row; posts via `send_message_with_wal(action_type=BUILD_PR_LANDED, ...)`; unlinks scratch.

**QA worker (`_process_qa`):**
1. Coordinator reads the row.
2. **Pre-flight #1 (`outgoing_msg` log):** any row for `(job_table='qa_job', job_uuid=..., action_type='qa_answer'|'qa_decline')` with `telegram_message_id IS NOT NULL` proves the answer was already posted. Transition row to `answered`, set `telegram_reply_msg_id` and `last_dedup_token` from the log row, unlink scratch, return.
3. **Pre-flight #2 (scratch file, atomic):** if `<job_uuid>.result.json` exists with a valid `answer` or `declined_reason`, proceed to step 5 — DO NOT relaunch.
4. Otherwise launch container; worker writes scratch atomically.
5. Coordinator: `wal_resp = send_message_with_wal(job_table='qa_job', job_uuid=..., action_type=QA_ANSWER (or QA_DECLINE), chat_id, text=answer, thread_id, reply_to=request_msg_id)`. Idempotent against confirmed prior success.
6. On `wal_resp.ok`: `UPDATE qa_job SET side_effect_completed_at=now(), telegram_reply_msg_id=wal_resp.result.message_id, last_dedup_token=wal_resp.dedup_token, status='answered'`. Unlink scratch.

**Review worker (`_process_review`):**
- Same shape as QA, with `action_type=REVIEW_POST` (or `REVIEW_FAILURE` for the apology branch). `pr_review` recovery checks `outgoing_msg` first, then scratch, then re-launches.

### Recovery uses the new flag + scratch directory + outgoing-msg log

`_recover_jobs_on_boot()` (v5):
- **First:** sweep `<results_dir>/*.tmp` files older than 60s (crashed mid-write garbage; safe to unlink).
- **Per in-progress row:**
  - Check `outgoing_msg` for `(job_table, job_uuid)` with `telegram_message_id SET`. If present, the side effect is committed AND the user already saw it; reconcile the row to terminal state (`succeeded` / `answered` / `posted`) using the recorded `message_id`; unlink scratch; done.
  - Else check `outgoing_msg` for the same key with `intent_recorded_at SET` but `telegram_message_id IS NULL` (the ambiguous middle window). Coordinator proceeds with the normal pipeline; `send_message_with_wal` will be called and is idempotent. **This is where the residual single-duplicate risk sits**, mitigated by the hidden intent-marker.
  - Else check the atomic scratch file for the answer. If present, run the post-side of the pipeline (call `send_message_with_wal` with the recovered payload). If absent and `side_effect_completed_at IS NULL`, re-queue for full container relaunch.
  - Build-only fallback: if neither scratch nor outgoing_msg shows a side effect, run `gh pr list` to catch the case where build pushed but everything else was lost. Build is the only pipeline with an external dedup oracle.

### Schema additions to support idempotency

The `outgoing_msg` table (full DDL specified in the "Outgoing-message write-ahead log" section above) is added in `_ensure_tables()` alongside the existing tables.

Additionally, add to all three job tables (`build_job`, `qa_job`, `pr_review`):
```sql
ALTER TABLE build_job ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT '';
ALTER TABLE build_job ADD COLUMN side_effect_completed_at TEXT;
-- side_effect_completed_at is set BEFORE the row transitions to 'succeeded'.
-- It marks "the externally-visible artifact (PR / answer / review post) exists."

ALTER TABLE qa_job ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT '';
ALTER TABLE qa_job ADD COLUMN side_effect_completed_at TEXT;
ALTER TABLE qa_job ADD COLUMN telegram_reply_msg_id INTEGER;
-- For QA, the side effect is the Telegram reply. We capture the msg_id so a re-entry
-- can detect "the answer was already posted, do not post again."

ALTER TABLE pr_review ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT '';
ALTER TABLE pr_review ADD COLUMN side_effect_completed_at TEXT;
-- pr_review.posted_at already exists; side_effect_completed_at is the
-- "before-Telegram-post" reservation, posted_at is the "after-Telegram-post" stamp.
```

### Idempotency keys

- **`build_job.idempotency_key`** = `sha256(target_repo + target_branch + commit_message + canonical_json(edits))`. Two `ship it`s with the same plan → same key. The branch name encodes the date so re-issuing on a different day produces a different branch + key (intentional: it's a different commission).
- **`qa_job.idempotency_key`** = `sha256(chat_id + topic_id + request_msg_id + question)`. Same Telegram message ID → same key. The `request_msg_id` is the keystone — it ties the job to a single user message and prevents duplicate answers if recovery double-fires.
- **`pr_review.idempotency_key`** = `sha256(repo + pr_number + request_msg_id)`. Same review request → same key.

### Idempotency test coverage

`tests/test_idempotency.py` covers eight crash windows + the contract-stability cases (v6 adds #8):

1. Crash before any side effect → recovery re-runs; produces side effect once.
2. Crash after build's `gh pr create` returned 201 + atomic scratch file written, before SQLite update → recovery reads scratch; row reconciled; no second `pr create`.
3. Crash between worker scratch-write and `send_message_with_wal` (QA/review) → recovery finds scratch but no `outgoing_msg` row; calls `send_message_with_wal` once; user gets the answer.
4. Crash mid-`send_message_with_wal` after intent log but before Telegram returns (QA/review ambiguous window) → recovery sees intent row with `telegram_message_id IS NULL`; calls `send_message_with_wal` again. **Asserts the documented contract: a single duplicate is possible.** Test verifies the duplicate is detectable post-hoc by querying `outgoing_msg` for multiple successful `(job_table, job_uuid, action_type)` rows.
5. Two `ship it`s for the same plan in rapid succession → second hits `idx_build_job_idempotency` IntegrityError.
6. Atomic-write kill: kill worker mid-scratch (`.tmp` exists, no rename) → recovery does NOT parse `.tmp`; sweep cleans after 60s.
7. `outgoing_msg` dedup oracle: pre-populate confirmed row → recovery reconciles WITHOUT calling `send_message` (assert `tg_request` mock count = 0).
8. **NEW v6: content-independence of `intent_id`** — for the same `(job_table, job_uuid, action_type)`, vary the rendered message body across two recovery attempts (simulating a phrasing/persona change between daemon versions). Assert the second call detects the prior post via `intent_id` and returns `deduped: True`. The test FAILS if `intent_id` is content-coupled (catches v5-style regression).
9. **v6: `dedup_token` correctness** — verify `last_dedup_token` on the job row matches `outgoing_msg.dedup_token` for the same intent_id; verify operator-facing query `SELECT * FROM outgoing_msg WHERE dedup_token = ?` returns exactly one row.
10. **NEW v7: duplicate-cleanup workflow** — seed `outgoing_msg` with two confirmed rows for the same `(job_table, job_uuid, action_type)`; run `bin/commodore-dup-cleanup --resolve` (with `tg_request` mocked); assert the older `id` is marked `cleanup_role='canonical'`, the newer is `cleanup_role='suppressed'` with `cleanup_action='edited_to_marker'`, the mocked `editMessageText` was called with the suppressed `telegram_message_id`, and re-running the detection query returns zero rows.
11. **NEW v7: 48-hour cleanup boundary** — same setup but with `sent_at` >48h old; assert `cleanup_action='left_in_place_with_followup'` and a follow-up `outgoing_msg` row was inserted with `action_type='dup_followup'`.
12. **NEW v7: WAL-helper enforcement** — static analysis test that greps the QA/review reply codepaths for direct `send_message(` calls (excluding `send_message_with_wal`); fails the build if any are found. Prevents accidental WAL bypass.

Add a unique index per table:
```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_build_job_idempotency ON build_job(idempotency_key) WHERE idempotency_key != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_qa_job_idempotency ON qa_job(idempotency_key) WHERE idempotency_key != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_pr_review_idempotency ON pr_review(idempotency_key) WHERE idempotency_key != '';
```
Empty-string default lets pre-existing rows coexist; new rows always have a non-empty key.

### What's still NOT idempotent (honest accounting)

- **Recovery double-posting on QA** (window between SQLite commit and Telegram send): documented trade-off; worst case is silent skip, user re-asks.
- **Manual operator intervention against the DB** (e.g. operator deletes `side_effect_completed_at` to force a retry): out of scope; intentional override.
- **Concurrent `ship it`s from two operators within the same draft** (race between two coordinator threads picking up the same draft): blocked by the existing v2 `idx_plan_drafts_active` partial unique index, not by idempotency_key.

---

## Runbook rename — pick one before implementation

The plan offers two options; operator picks one before this ships. Default recommendation: **Option B** (no rename).

**Option A — rename + update all callers in one PR:**
- Rename `docs/PR_REVIEW_RUNBOOK.md` → `docs/OPERATOR_RUNBOOK.md`.
- Update every reference in the same change. Currently confirmed: `scripts/pending-mail-to-eunice.py:59`. Plus a `grep -rn "PR_REVIEW_RUNBOOK"` sweep before commit to catch anything missed (commit history, dev journal entries, etc.).
- Pro: clean state going forward.
- Con: bigger blast radius; every rename touches CI / git-blame / any in-flight work referencing the path.

**Option B — keep filename, broaden scope (recommended):**
- Keep `docs/PR_REVIEW_RUNBOOK.md` as-is.
- Add a top-level note: "Despite the filename, this runbook now also covers the conversational-build, Q&A, and recovery operations. Rename to `OPERATOR_RUNBOOK.md` deferred to avoid breaking live references at `scripts/pending-mail-to-eunice.py` and elsewhere."
- Pro: zero caller breakage.
- Con: filename is technically a misnomer now; future operators searching for "operator runbook" might not find it. Mitigation: add `docs/README.md` index entry pointing to the file.

**Option B is the recommendation** because the migration cost outweighs the cosmetic gain. The plan-mode reviewer confirmed at least one live reference; rename without sweep would break it.

---

## Files to create

### `fleet-commodore/build_worker.py` (~250 lines)
Container entrypoint for fork-and-PR builds. Mirrors `review_worker.py`'s shape. Reads job JSON from stdin (fields per v1 plan), pipeline (per v1 plan: `gh repo fork --clone=false` → clone fork → `git fetch upstream && git rebase upstream/main` → branch → apply edits → commit as DeepSeaSquid → push to fork → `gh pr create --head leviathan-agent:branch`), single JSON object on stdout. **Unchanged from v1.**

### `fleet-commodore/qa_worker.py` (~200 lines)
Container entrypoint for read-only Q&A. Reads job JSON: `{"qa_uuid", "question", "requester", "channel"}`. Spawns Claude CLI with allowlisted tools — **`GH_TOKEN` is NOT in the env, and tools that hit GitHub are not in the allowlist**:
- `Read,Grep,Glob` against `/app/knowledge` (mounted: `dev-journal/`, `docs/`, repo CLAUDE.md/README.md from each Leviathan repo at container-launch time)
- `WebFetch` allowlisted to `*.leviathannews.xyz` only (egress filter enforces; tool config is defense-in-depth)
- `Bash(commodore-db:*)`, `Bash(commodore-orm:*)` — existing wrappers, unchanged

Returns `{"status": "answered", "answer": "...", "citations": [...]}` or `{"status": "declined", "reason": "..."}` on stdout. Stderr → `/tmp/commodore-qa-<uuid>.stderr`.

### `fleet-commodore/bin/launch-build-container` (~250 lines)
Direct adaptation of `bin/launch-review-container` (commodore.py:155-193 pattern: same env-file with `0o600`, same `--read-only` + tmpfs + `no-new-privileges`, same `commodore-egress` network, same DB tunnel sidecar attachment, 10-min timeout, single-JSON-on-stdout). Injects `GH_TOKEN` from `~/.config/commodore/gh_pat`.

**v4 addition — result-scratch mount (also retroactively applies to `bin/launch-review-container`):**
- Add a module-level constant `RESULTS_DIR = Path(os.environ.get("COMMODORE_RESULTS_DIR", "~/.local/state/commodore/results")).expanduser()` near the other path constants (after launch-review-container.py:66).
- Before the docker-run argv, ensure the directory exists: `RESULTS_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)`.
- Add ONE LINE to the docker-run argv list (between `"-v", f"{claude_json_copy}:/home/reviewer/.claude.json:rw"` at line 194 and `"--memory", "512m"` at line 195):
  ```python
  "-v", f"{RESULTS_DIR}:/var/run/commodore-results:rw",
  ```
- The launcher MUST NOT delete `<uuid>.result.json` from `RESULTS_DIR` in its `finally` block. The coordinator owns that file's lifecycle. Document this in the launcher's docstring.

### `fleet-commodore/bin/launch-qa-container` (~200 lines)
Same shape as the build launcher with three differences:
- **Does NOT read `gh_pat`** — `GH_TOKEN` is not set in the env-file
- Mounts the knowledge corpus read-only at `/app/knowledge` via `-v <local-checkout>/dev-journal:/app/knowledge/dev-journal:ro` (and similar for `docs/`, top-level CLAUDE.md, README.md from each `~/dev/leviathan/<repo>`)
- 5-min timeout (Q&A is interactive; long answers are usually wrong answers)
- **Includes the same result-scratch mount as build/review** (`-v {RESULTS_DIR}:/var/run/commodore-results:rw`) so the QA worker can write `<uuid>.result.json` for late-dedup recovery.

### `fleet-commodore/bin/commodore-dup-cleanup` (~250 lines, NEW v7)
Operator helper for resolving QA/review duplicates surfaced by the detection query. Modes: `--list`, `--resolve <job_table> <job_uuid> <action_type>`, `--resolve-all [--dry-run]`. Reads `BOT_TOKEN_FILE`; calls `tg_request` for `editMessageText` / `deleteMessage`; updates `outgoing_msg.cleanup_*` only after the Telegram-side action returns OK. Honors the 48-hour edit/delete window — older messages get the "leave-in-place + followup reply" path. Implementation lives outside `commodore.py` so it runs as a one-shot operator command without competing with the daemon for Telegram polling.

### `fleet-commodore/egress/qa-filter` (new — separate filter for Q&A network)
The reviewer/build containers run on `commodore-egress` with the existing `egress/filter` (allowlists `*.github.com`, `api.anthropic.com`). Q&A runs on a NEW `commodore-qa-egress` network that allows `api.anthropic.com` + `*.anthropic.com` + `*.leviathannews.xyz` and **denies `*.github.com`**. This is a network-layer enforcement of the "no GitHub from Q&A" decision; the tool-allowlist is defense-in-depth.

Add `bin/setup-commodore-qa-egress-network.sh` mirroring the existing `bin/setup-commodore-egress-network.sh`.

### Schema additions to `commodore.py:_ensure_tables()` (~120 lines added — bigger than v1)

Three tables — two are NEW, one is the v1 `plan_drafts` design carried over:

```sql
-- v1 design carried over: plan-refinement state
CREATE TABLE IF NOT EXISTS plan_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_uuid TEXT UNIQUE NOT NULL,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER,
    requester_id INTEGER NOT NULL,
    requester_username TEXT,
    title TEXT,
    target_repo TEXT,
    target_branch TEXT,
    plan_body_md TEXT,
    message_history_json TEXT,
    status TEXT NOT NULL,             -- drafting | shipping | shipped | failed | abandoned
    pr_url TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_drafts_active
  ON plan_drafts(chat_id, COALESCE(thread_id,0), requester_id)
  WHERE status IN ('drafting', 'shipping');

-- NEW: durable job table for the build pipeline
CREATE TABLE IF NOT EXISTS build_job (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uuid TEXT UNIQUE NOT NULL,
    draft_uuid TEXT NOT NULL,         -- FK to plan_drafts.draft_uuid
    chat_id INTEGER NOT NULL,
    topic_id INTEGER,
    requester_id INTEGER NOT NULL,
    requester_username TEXT,
    request_msg_id INTEGER,
    target_repo TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    job_payload_json TEXT NOT NULL,   -- the full JSON sent to build_worker stdin
    status TEXT NOT NULL,             -- queued | in_progress | succeeded | failed | abandoned
    pr_url TEXT,
    commit_sha TEXT,
    error TEXT,
    error_stage TEXT,                 -- clone | edit | push | pr (from worker)
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_build_job_status ON build_job(status);

-- NEW: durable job table for the Q&A pipeline
CREATE TABLE IF NOT EXISTS qa_job (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uuid TEXT UNIQUE NOT NULL,
    chat_id INTEGER NOT NULL,
    topic_id INTEGER,
    requester_id INTEGER NOT NULL,
    requester_username TEXT,
    request_msg_id INTEGER,
    question TEXT NOT NULL,
    status TEXT NOT NULL,             -- queued | in_progress | answered | declined | failed
    answer_summary TEXT,              -- truncated; full goes to Telegram
    declined_reason TEXT,
    tools_used TEXT,
    duration_ms INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_qa_job_status ON qa_job(status);
```

The existing `pr_review` table (commodore.py:492-520) needs **two columns added** to bring it to parity:
```sql
ALTER TABLE pr_review ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pr_review ADD COLUMN started_at TEXT;
-- (posted_at already exists per the existing schema)
```
Use guarded `_safe_column_add(conn, "pr_review", "attempt_count", "INTEGER NOT NULL DEFAULT 0")` helper since `_ensure_tables()` is idempotent and SQLite has no `ADD COLUMN IF NOT EXISTS`.

### Postgres role hardening — extend EXISTING `commodore_reader` role
Per v1 plan; one-shot SQL run as DB admin, REVOKEs on `bot_user(email,unique_token)`, `bot_social_account`, `bot_webauthn_credential`, `bot_pending_account_claim`, `lnn_user_login_event`, `lnn_click(ip_address, user_agent)`. Document in `OPERATOR_RUNBOOK.md`. **Unchanged from v1.**

---

## Files to modify

### `fleet-commodore/commodore.py` (~500 lines added — biggest delta is the per-action auth + boot recovery)

**1. Per-action authorization helpers** (replaces the v1 single `_is_privileged_chat()`):

```python
def _can_ship(msg) -> bool:
    """Authorization for /ship, /abandon, plan-refinement, and PR-review.
       These produce GitHub side effects or commit operator intent. Bot HQ + admin only.
       Matches the existing handle_pr_request gate (commodore.py:1614-1623)."""
    chat_id = msg.get("chat", {}).get("id", 0)
    return chat_id == BOT_HQ_GROUP_ID and _is_admin(msg)

def _can_plan(msg) -> bool:
    """Plans are PR drafts. Same gate as ship — same surface."""
    return _can_ship(msg)

def _can_qa(msg) -> bool:
    """Q&A is read-only. Wider surface: Bot HQ, Lev Dev, Agent Chat (any topic),
       OR an admin in any DM with the bot."""
    chat = msg.get("chat", {})
    chat_id = chat.get("id", 0)
    chat_type = chat.get("type", "")
    if chat_id in (BOT_HQ_GROUP_ID, LEV_DEV_GROUP_ID, AGENT_CHAT_GROUP_ID):
        return True
    if chat_type == "private" and _is_admin(msg):
        return True
    return False
```

Note: `LEV_DEV_GROUP_ID` does not currently exist in `commodore.py` (only `BOT_HQ_GROUP_ID`, `SQUID_CAVE_GROUP_ID`, `AGENT_CHAT_GROUP_ID` at lines 87-89). Add it as an env-driven constant alongside the others.

**2. New regexes** at the existing `_PR_REVIEW_RE` block (commodore.py:1312):
```python
_PLAN_REFINE_RE = re.compile(
    r"^(?:let'?s\s+)?(?:plan|design|propose|draft|build|implement|sketch|outline)\b",
    re.IGNORECASE,
)
_SHIP_RE = re.compile(r"^/ship(?:@\S+)?\b|^ship\s+it\b", re.IGNORECASE)
_ABANDON_RE = re.compile(r"^/abandon(?:@\S+)?\b|^abandon\s+plan\b", re.IGNORECASE)
_QA_RE = re.compile(
    r"^/ask(?:@\S+)?\s+(.+)|^(how|what|why|when|where|how many|how much|which)\s+",
    re.IGNORECASE,
)
```

**3. New handlers** (`handle_plan_message`, `handle_ship`, `handle_abandon`, `handle_qa`):
- Each begins with the appropriate `_can_*()` gate; out-of-policy returns the v1 in-character decline.
- `handle_ship` writes the `build_job` row with `status='queued'` **before** any in-memory enqueue (mirroring `_claim_review`'s persist-then-enqueue pattern at commodore.py:1507-1570). The full `job_payload_json` is captured here, so a restart can rebuild the Docker invocation from the row alone.
- `handle_qa` likewise persists a `qa_job` row before enqueueing.

**4. New durable-job claim helpers** mirroring `_claim_review()` for the new queues:
- `_claim_build_job(draft_row) -> (job_uuid, ack_string)` — INSERT row → enqueue → cooldown.
- `_claim_qa_job(msg, question) -> (job_uuid, ack_string)` — INSERT row → enqueue → per-user cooldown via `_qa_cooldown_by_user` (mirrors `_review_cooldown_by_user` at commodore.py:1457).

**5. Three coordinator threads + boot recovery** (the keystone — finally makes the queues live AND survives restarts):

```python
_build_queue: queue.Queue = queue.Queue(maxsize=10)
_qa_queue: queue.Queue = queue.Queue(maxsize=20)
# _review_queue already exists at commodore.py:1455

def _recover_jobs_on_boot():
    """Re-queue every job with status IN ('queued','in_progress') from all three job tables.
       Posts a one-line in-character note for any 'in_progress' row that the previous
       daemon owned (a crash mid-flight) BEFORE re-enqueuing.
       Runs ONCE in poll() startup, before any worker thread starts pulling from queues."""
    conn = sqlite3.connect(str(DB_FILE), timeout=10)
    try:
        # pr_review: rebuild job dict from the row, enqueue
        for row in conn.execute(
            "SELECT review_uuid, repo, pr_number, chat_id, topic_id, request_msg_id, "
            "       requested_by_id, requested_by_username, status "
            "  FROM pr_review WHERE status IN ('queued','in_progress') ORDER BY id"
        ).fetchall():
            review_uuid, repo, pr_number, chat_id, topic_id, *_, status = row
            job = {... rebuild from row ...}
            if status == "in_progress":
                # crashed mid-flight; reset and notify
                conn.execute("UPDATE pr_review SET status='queued', "
                             "error=COALESCE(error,'')||'; restarted after crash' "
                             "WHERE review_uuid=?", (review_uuid,))
                send_message(chat_id, "The Admiralty resumes a previously-interrupted "
                                       "review — pray stand by.", thread_id=topic_id)
            try:
                _review_queue.put_nowait(job)
            except queue.Full:
                # mark orphaned; user will need to re-issue
                conn.execute("UPDATE pr_review SET status='orphaned', "
                             "error='boot recovery: queue full' "
                             "WHERE review_uuid=?", (review_uuid,))
        conn.commit()
        # build_job: same shape (rebuild from job_payload_json + row)
        # qa_job: same shape
    finally:
        conn.close()

def _build_worker():
    while True:
        job = _build_queue.get()
        try: _process_build(job)
        except Exception: log.exception("build worker crash")
        finally: _build_queue.task_done()

def _qa_worker():
    while True:
        job = _qa_queue.get()
        try: _process_qa(job)
        except Exception: log.exception("qa worker crash")
        finally: _qa_queue.task_done()

def _review_worker():
    """The consumer that should have shipped with the original PR-review task."""
    while True:
        job = _review_queue.get()
        try: _process_review(job)
        except Exception: log.exception("review worker crash")
        finally: _review_queue.task_done()
```

`poll()` startup order:
1. `_ensure_tables()` (existing)
2. `_recover_jobs_on_boot()` (NEW — must run before workers start)
3. Start `_build_worker`, `_qa_worker`, `_review_worker` as `threading.Thread(daemon=True)`
4. Begin polling loop (existing)

`_process_*` shape (uniform, mirroring v1):
- Mark row `status='in_progress'`, increment `attempt_count`, set `started_at`
- `subprocess.run(['bin/launch-{review|build|qa}-container', uuid], input=job_json, capture_output=True, timeout=600)`
- Parse last-JSON from stdout; on success update row + post Telegram reply; on failure update row with error+stage and post in-character apology
- Set `finished_at` (or `posted_at` for `pr_review` to match existing column)

**6. CAPABILITIES block update** — per v1 plan (lines 1152-1167 → "I can refine plans, file fork PRs, review PRs, answer read-only Fleet questions; I cannot merge, push to leviathan-news directly, deploy, or reveal credentials/PII").

**7. `INJECTION_OUTPUT_PATTERNS` additions** — per v1 plan: `"ssh "`, `"private key"`, `"BEGIN OPENSSH"`, `"BEGIN RSA"`, `"BEGIN EC PRIVATE"`, `"wallet seed"`, `"mnemonic"`, `"passphrase"`. The dynamic-secret-prefix loop at commodore.py:373-385 already covers `GH_PAT_FILE` and `BOT_TOKEN_FILE`; verify both are in scope and add new credential-file paths to that loop if any new ones are introduced (none are in this plan — Q&A doesn't get its own credential).

### `fleet-commodore/reviewer.Dockerfile`
Add a knowledge-corpus mount point (`/app/knowledge`) for runtime bind-mount. Image stays stateless. Per v1 plan — applies to BOTH the reviewer and the QA container (same image; differentiated by entrypoint and network).

### `fleet-commodore/bin/launch-review-container` (modify — retroactive v4 mount)
Add the same `RESULTS_DIR` constant + `mkdir` + `-v {RESULTS_DIR}:/var/run/commodore-results:rw` argv line as the build/qa launchers (specified above). Also update `review_worker.py` to write its result to `/var/run/commodore-results/<review_uuid>.result.json` immediately after Claude returns the verdict, before stdout JSON.

### `fleet-commodore/review_worker.py` (modify — write scratch file before stdout)
Currently a stub (per Explore findings). When fleshed out, write `<review_uuid>.result.json` to `/var/run/commodore-results/` AS THE FIRST POST-CLAUDE STEP. Same contract as the build/qa workers.

### `fleet-commodore/docs/PR_REVIEW_RUNBOOK.md` (no rename — Option B)
Keep filename to preserve the live reference at `scripts/pending-mail-to-eunice.py:59` and any other callers a `grep` sweep might find. Add a top-of-file note explaining the broadened scope. Add five new sections:
- **"Q&A egress: separate network"** — explains why `commodore-qa-egress` has no GitHub allowlist; how to verify with `docker network inspect`.
- **"Boot recovery: what happens when the daemon restarts mid-job"** — explains the `_recover_jobs_on_boot()` semantics: scratch dir is consulted first; rows with `side_effect_completed_at SET` are reconciled; rows with neither flag are re-queued. Build's GitHub `gh pr list` pre-flight is the safety net. QA/review use the recovered scratch file to replay missed Telegram posts (no silent skip).
- **"Per-action authorization model"** — table mapping action → `_can_*()` predicate → required chat + admin status.
- **"Idempotency keys and how to manually retry"** — explains the unique idempotency_key per table, how to manually clear `side_effect_completed_at` to force a retry (operator override).
- **v4: "Result scratch directory (`~/.local/state/commodore/results/`)"** — what files appear, who owns them (coordinator), the `0o700` mode, the v5 atomic-write protocol (operator should never see `.tmp` files outside a sub-second window; persistent `.tmp` files are evidence of a worker crash). Includes `find ~/.local/state/commodore/results/ -mtime +7` as a one-liner.
- **v5/v6: "Outgoing-message write-ahead log (`outgoing_msg`)"** — dedup oracle. Operator queries:
  - In-flight: `SELECT job_table, job_uuid, action_type, dedup_token, intent_recorded_at FROM outgoing_msg WHERE telegram_message_id IS NULL ORDER BY id DESC LIMIT 20`
  - Recent confirmed: `SELECT job_table, job_uuid, action_type, telegram_message_id, sent_at FROM outgoing_msg WHERE telegram_message_id IS NOT NULL ORDER BY id DESC LIMIT 20`
  - **Detect duplicates** (the contract residual): `SELECT job_table, job_uuid, action_type, COUNT(*) FROM outgoing_msg WHERE telegram_message_id IS NOT NULL GROUP BY 1,2,3 HAVING COUNT(*) > 1` — any rows here are confirmed duplicates from the ambiguous-crash window; operator can manually delete the duplicate Telegram message via the bot's admin tools.
- **v6: "Per-pipeline durability contract"** — verbatim restatement: build is fully idempotent (no duplicate PR possible across any crash window); QA/review are best-effort with a single-duplicate-at-worst window between intent log insert and Telegram response. The duplicate is not silently swallowed; user-visible duplicates are surfaced and the operator-facing query above detects them. This is the limit achievable without Telegram-side idempotency tokens. **All operator comms (Bot HQ pin, runbook header, deploy-time announcements) MUST use this exact split — never describe the system as "no double side-effects on any pipeline."**

- **NEW v7: "Duplicate-cleanup workflow"** — concrete recipe operators run when the duplicate-detection query returns rows. See full procedure below.

### `fleet-commodore/tests/` — new test files
Per v1 plan, plus three new files:
- `test_plan_drafts.py`, `test_build_worker.py`, `test_qa_worker.py`, `test_qa_safety.py`, `test_review_worker.py`, `test_db_role_hardening.py` — per v1.
- **`test_boot_recovery.py`** (v2) — seeds `pr_review`, `build_job`, `qa_job` rows with `status='queued'` and `status='in_progress'`; calls `_recover_jobs_on_boot()`; asserts queues are populated, `'in_progress'` rows without `side_effect_completed_at` were reset to `'queued'` with retry note, `'in_progress'` rows WITH `side_effect_completed_at` were reconciled to `'succeeded'` without re-queueing (no double-post). Also tests the queue-full overflow path → `'orphaned'`.
- **`test_authorization.py`** (v2) — `_can_ship` / `_can_plan` / `_can_qa` matrix per v2.
- **NEW v3: `test_idempotency.py`** — covers all four crash windows from the Idempotency design section:
  1. Crash before any side effect → recovery re-runs; pre-flight finds nothing; produces side effect once.
  2. Crash after `gh pr create` returned 201 but before SQLite update → recovery's GitHub pre-flight (mocked `gh pr list`) returns the existing PR; row reconciled; no second `pr create` call.
  3. Crash between SQLite `side_effect_completed_at` write and Telegram post (QA/review) → recovery skips re-post; assertion: `send_message` mock called zero times for that row.
  4. Two `ship it`s for the same plan → second hits `idx_build_job_idempotency` `IntegrityError`; coordinator returns the existing job's `pr_url`.

---

## Critical files referenced (read-only inputs)

- `/Users/gerrithall/dev/leviathan/fleet-commodore/commodore.py:87-89` — chat-id constants (`BOT_HQ_GROUP_ID`, `SQUID_CAVE_GROUP_ID`, `AGENT_CHAT_GROUP_ID`); add `LEV_DEV_GROUP_ID` here
- `/Users/gerrithall/dev/leviathan/fleet-commodore/commodore.py:166-204` — `_policy_for()` chat-routing pattern; reuse for ambient policy, NOT for action authorization
- `/Users/gerrithall/dev/leviathan/fleet-commodore/commodore.py:363-385` — `INJECTION_OUTPUT_PATTERNS` + dynamic secret-prefix loop
- `/Users/gerrithall/dev/leviathan/fleet-commodore/commodore.py:492-520` — `pr_review` table schema (existing); `build_job`/`qa_job` mirror this shape
- `/Users/gerrithall/dev/leviathan/fleet-commodore/commodore.py:1126-1172` — `BOT_IDENTITY` + CAPABILITIES block to update
- `/Users/gerrithall/dev/leviathan/fleet-commodore/commodore.py:1290-1292` — `_is_admin()` helper, reused by all three `_can_*` predicates
- `/Users/gerrithall/dev/leviathan/fleet-commodore/commodore.py:1455-1598` — `_review_queue` + `_claim_review()` — the persist-then-enqueue pattern that `_claim_build_job` and `_claim_qa_job` will mirror
- `/Users/gerrithall/dev/leviathan/fleet-commodore/commodore.py:1606-1623` — `handle_pr_request()` admin gate; `_can_ship()` codifies the same gate
- `/Users/gerrithall/dev/leviathan/fleet-commodore/bin/launch-review-container` — pattern for `launch-build-container` and `launch-qa-container`
- `/Users/gerrithall/dev/leviathan/fleet-commodore/review_worker.py` — pattern for `build_worker.py` and `qa_worker.py`
- `/Users/gerrithall/dev/leviathan/fleet-commodore/bin/commodore-db`, `commodore-orm` — reused as-is by Q&A
- `/Users/gerrithall/dev/leviathan/fleet-commodore/egress/filter` — reference for the new `egress/qa-filter`
- `/Users/gerrithall/dev/leviathan/fleet-commodore/docs/PR_REVIEW_RUNBOOK.md:82-111` — `commodore_reader` role provisioning; extend with REVOKEs

---

## Verification plan

**Unit tests** (laptop, no prod creds):
- `pytest tests/test_plan_drafts.py` — schema, unique-active index, transitions
- `pytest tests/test_build_worker.py` — mocked subprocess
- `pytest tests/test_qa_worker.py` — mocked Claude
- `pytest tests/test_qa_safety.py` — hostile-question battery
- `pytest tests/test_review_worker.py` — long-overdue review-consumer coverage
- `pytest tests/test_boot_recovery.py` — durability check (v2)
- `pytest tests/test_authorization.py` — per-action gate matrix (v2)
- `pytest tests/test_idempotency.py` — **NEW v3: four-crash-window coverage**
- `pytest tests/test_db_role_hardening.py` — gated on `COMMODORE_TEST_DB_URL`

**Integration tests** (Mini-side, manual):

1. Apply DB role hardening REVOKEs (one-shot SQL).
2. Build the image (rebuild only if Dockerfile changed): `./bin/build-reviewer-image.sh`
3. Set up the new Q&A egress network: `./bin/setup-commodore-qa-egress-network.sh`
4. Restart Commodore daemon (`tmux kill-window -t leviathan:commodore`; cron watchdog respawns).

**Q&A end-to-end (per v1):**
- Bot HQ: `@leviathan_commodore_bot how does the X queue work?` → answers via docs corpus
- Bot HQ: `@leviathan_commodore_bot how many articles published this month?` → answers via DB
- Bot HQ: `@leviathan_commodore_bot what's gerrit's password?` → declines
- Lev Dev: same first question → answers (Q&A is allowed there)
- Squid Cave: same first question → declines (Q&A is NOT allowed there)
- **NEW:** Inside the Q&A container, attempt `gh pr list` → expect tool-not-allowed error AND egress-deny on `api.github.com` (network-layer enforcement)

**Plan-and-ship end-to-end (Bot HQ only — per the new auth model):**
- Bot HQ admin: multi-turn plan refinement → `ship it` → PR appears at `leviathan-news/squid-bot` from `leviathan-agent:commodore/<slug>-<date>`
- Bot HQ non-admin: same flow → declines with "unranked crew" formal phrasing
- Lev Dev admin: `ship it` → declines with "return to Bot HQ" formal phrasing (proves Q&A privilege does NOT include ship)

**Boot recovery end-to-end (v2 — durability):**
- Issue a build, immediately `tmux kill-window -t leviathan:commodore` while job is `'in_progress'` AND before `side_effect_completed_at` is set
- Restart daemon
- Expect: in-character "resumes a previously-interrupted dispatch" message in the requesting chat, build container fires, PR lands, `build_job.attempt_count = 2`
- Repeat with a `pr_review` job for parity coverage

**Idempotency end-to-end (v3 + v4 + v5):**
- **Build scratch-file replay:** craft a `build_job` row with `status='in_progress'`, drop a matching scratch file via the atomic-write helper (so it lives at `<uuid>.result.json`, not `.tmp`) → restart daemon → expect: NO second container launch, NO `gh pr list` call (scratch path wins), row reconciles to `'succeeded'`.
- **Build scratch missing → GitHub fallback:** delete the scratch before restart; the real PR exists on GitHub → expect: `gh pr list` pre-flight finds it, reconciles.
- **QA `outgoing_msg` dedup:** manually insert an `outgoing_msg` row for a `qa_job` with `telegram_message_id` set → restart → expect: pre-flight #1 wins; row reconciles to `answered` using the logged `message_id`; mock `tg_request` is NOT called.
- **QA scratch-file replay (no prior post):** craft a `qa_job` with a scratch file containing an answer but NO `outgoing_msg` row → restart → expect: scratch parsed; `send_message_with_wal` called once; answer posted; `outgoing_msg` populated; `telegram_reply_msg_id` filled in.
- **QA ambiguous-window simulation:** manually insert `outgoing_msg` row with `intent_recorded_at SET` but `telegram_message_id IS NULL` → restart → expect: `send_message_with_wal` called; if Telegram had actually delivered, the user sees a single duplicate (mitigated by hidden intent-marker — verify it's present in the second post).
- **Atomic-write protocol:** kill `qa_worker.py` mid-write (signal handler simulation, with `.tmp` present and `.result.json` missing) → restart → expect: coordinator does NOT parse `.tmp`; `.tmp` is unlinked by the 60s sweep; pipeline re-launches.
- **Double-`ship it`:** issue `ship it` twice fast → second hits `idx_build_job_idempotency` IntegrityError; reply quotes existing `pr_url`.
- **Mount inspection:** `docker inspect commodore-build-<uuid>` while a build is running → confirm `/var/run/commodore-results` bind is `rw` to `~/.local/state/commodore/results`.

**Operator monitoring:**
- `tail -f ~/dev/leviathan/fleet-commodore/logs/commodore.log` — worker startup, every job start/end, recovery-on-boot summary
- `sqlite3 commodore.db "SELECT id, status, attempt_count, error FROM build_job ORDER BY id DESC LIMIT 10"`
- `sqlite3 commodore.db "SELECT id, requester_username, declined_reason, duration_ms FROM qa_job ORDER BY id DESC LIMIT 20"`
- `docker network inspect commodore-qa-egress` — confirm Q&A net has no GitHub allowlist
- `docker ps --filter name=commodore-` — review/build/qa containers in flight

---

## Risk flags / what could go wrong

1. **Knowledge corpus drift** — docs corpus mounted at container-launch is a snapshot. Operator runs `git pull` in `~/dev/leviathan/squid-bot` to refresh. No auto-sync.
2. **Q&A safety relies on three layers (v2)** — Postgres role REVOKEs are the security boundary; `commodore-db`/`commodore-orm` parser gates catch accidents; `INJECTION_OUTPUT_PATTERNS` catch leaked secrets in Claude's response.
3. **~~Boot recovery may double-post~~** — RESOLVED in v3 via `side_effect_completed_at` flag + GitHub pre-flight + idempotency_key unique index.
3a. **~~QA/review silent-skip~~** — RESOLVED in v4/v5/v6: scratch-file plumbing + `outgoing_msg` WAL + `dedup_token` column. **No silent skip; explicit single-duplicate-at-worst contract** for the narrow window between intent-log insert and Telegram response.
3b. **~~Partial-JSON scratch reads~~** — RESOLVED in v5 via atomic write protocol (`.tmp` + `fsync` + `rename` + dir-fsync).
3c. **~~Hidden intent-marker brittleness~~** — RESOLVED in v6 by replacing the body-smuggled marker with first-class `dedup_token` columns. No invisible characters in user-facing text; dedup state lives entirely in SQLite.
3d. **~~`intent_id` content-coupling~~** — RESOLVED in v6 by deriving `intent_id` from `(job_uuid, action_type)` instead of `(job_uuid, chat_id, thread_id, body[:64])`. Rendered phrasing changes no longer defeat dedup.
4. **QA/review duplicate window (NEW honest disclosure)** — Acknowledged residual: a daemon crash between `outgoing_msg` intent insert and Telegram POST response can result in a single duplicate Telegram message when recovery re-issues. Detection is post-hoc via the operator query in the runbook; suppression is impossible without Telegram-side idempotency tokens. Operationally innocuous for build (duplicate ack, PR is unique); user-visible for QA/review (duplicate answer).
4. **Build-worker fork-and-PR can race "ship it" twice** — covered by both the v2 `idx_plan_drafts_active` partial unique index AND the new v3 `idx_build_job_idempotency` index. Defense-in-depth.
5. **Fork divergence** — build_worker rebases against `upstream/main` before branching (per v1).
6. **Q&A hits prod DB** — every Q&A burns a `commodore_reader` connection; capped via 3s statement timeout, 500-row limit, per-user cooldown.
7. **DeepSeaSquid identity on commits** — until GitHub Support unflags the `leviathan-commodore` account; credential swap, no code edit.
8. **`ALTER TABLE` migrations** — SQLite ALTER is additive-only but fails on duplicate column. The `_safe_column_add()` helper handles idempotent boot; daemon must be restarted (not hot-reloaded) for the migration to take effect.
9. **GitHub pre-flight rate limit** — every build coordinator pre-flight makes one `gh pr list` call. With a personal access token, that's 5,000 req/hr — orders of magnitude above plausible build volume. Risk negligible.
10. **`gh pr list` pre-flight false negatives** — if GitHub returns transient 5xx, pre-flight returns "no existing PR" and the worker proceeds. Mitigation: pre-flight retries 3x with 2s backoff before treating "no result" as authoritative. If all three fail, coordinator marks row `'failed'` with `error='pre-flight unable to verify; manual retry required'` rather than risking a duplicate.

---

## Duplicate-cleanup workflow (v7)

When the daily/on-demand duplicate-detection query returns rows, the operator follows this recipe. The goal: choose ONE confirmed post as canonical and either edit-to-marker, delete, or leave-with-followup the others, recording the decision in `outgoing_msg.cleanup_*` so the audit trail survives.

### Detection (already in the runbook)

```sql
SELECT job_table, job_uuid, action_type, COUNT(*) AS dup_count
  FROM outgoing_msg
 WHERE telegram_message_id IS NOT NULL
   AND cleanup_role IS NULL
 GROUP BY 1, 2, 3
 HAVING COUNT(*) > 1;
```

The `cleanup_role IS NULL` filter ensures already-resolved duplicates don't keep showing up.

### Step 1 — Identify the canonical row

For each `(job_table, job_uuid, action_type)` group, the canonical row is the one with the **lowest `id`** (equivalent to earliest `intent_recorded_at` since the column is monotonic). Rationale: it's the row whose intent was logged first; any later row with `telegram_message_id SET` is a recovery-induced retry.

```sql
SELECT id, telegram_message_id, sent_at, dedup_token
  FROM outgoing_msg
 WHERE job_table = :jt AND job_uuid = :ju AND action_type = :at
   AND telegram_message_id IS NOT NULL
 ORDER BY id ASC;
```

The first row → canonical. The rest → suppressed.

### Step 2 — Apply Telegram-side cleanup per pipeline

The cleanup action depends on which pipeline the duplicate came from:

| Pipeline | Action | Rationale |
|---|---|---|
| **QA / Review** answer in a topic-threaded chat | **`editMessageText`** on each suppressed `telegram_message_id` to replace the body with: `_[duplicate of message N° <canonical_message_id> — superseded]_` (italic, in-character one-liner). Keep the original message in place so reply-chains don't break, but the body is now an explicit pointer. | Editing preserves message-id stability; readers who scrolled into the reply chain see the pointer rather than the same answer twice. |
| **QA / Review** answer in a non-threaded chat with no reply chain | **`deleteMessage`** on each suppressed id. | No reply-chain integrity to preserve; removal is cleanest. |
| **Build** ack/apology (rare — duplicate ack while the PR itself is unique) | **`editMessageText`** to the same superseded-pointer one-liner. | Same reasoning as QA threaded. |
| Suppressed message older than 48h | **Leave-in-place + reply with followup**: post a fresh "duplicate of N° <canonical_message_id>" reply via `send_message_with_wal(action_type='dup_followup', ...)`. | Telegram refuses `editMessageText` and `deleteMessage` on messages older than 48 hours for non-channel chats. |

### Step 3 — Record the decision in `outgoing_msg`

Update each row:

```sql
UPDATE outgoing_msg
   SET cleanup_role = 'canonical', cleanup_at = :now,
       cleanup_operator_id = :op_id
 WHERE id = :canonical_id;

UPDATE outgoing_msg
   SET cleanup_role = 'suppressed',
       cleanup_action = :action,        -- 'edited_to_marker' | 'deleted' | 'left_in_place_with_followup'
       cleanup_at = :now,
       cleanup_operator_id = :op_id
 WHERE id IN (:suppressed_ids);
```

### Step 4 — Helper command (shipped with v7)

`bin/commodore-dup-cleanup` is a small operator helper:

```
bin/commodore-dup-cleanup --list                            # runs the detection query, prints groups
bin/commodore-dup-cleanup --resolve <job_table> <job_uuid> <action_type>  # interactive
                                                            # picks lowest-id as canonical, prompts before each Telegram edit/delete,
                                                            # writes cleanup_* on success
bin/commodore-dup-cleanup --resolve-all --dry-run           # show what it would do for every detected group
bin/commodore-dup-cleanup --resolve-all                     # bulk; only auto-resolves groups where all rows are <48h old
```

The script reads the bot token (same `BOT_TOKEN_FILE` the daemon uses), shells out to `editMessageText` / `deleteMessage` via `tg_request`, and updates `outgoing_msg` only after the Telegram-side action returns OK. Dry-run mode prints intended actions without touching Telegram or SQLite.

### Step 5 — Auditability

Every cleanup is reversible-in-spirit: the suppressed row's original `telegram_message_id` and `sent_at` are unchanged, and the `cleanup_*` columns are additive. To re-investigate a cleanup later:

```sql
SELECT id, action_type, telegram_message_id, sent_at,
       cleanup_role, cleanup_action, cleanup_at, cleanup_operator_id
  FROM outgoing_msg
 WHERE job_uuid = :ju
 ORDER BY id ASC;
```

### What the workflow does NOT promise

- **It does not prevent duplicates.** That's the documented v6 contract residual.
- **It does not retroactively fix user confusion** if the user already read both messages before cleanup. The pointer text in step 2 is the best we can do.
- **It does not catch duplicates outside the `outgoing_msg` log** (i.e., if the duplicate-detection query is wrong or the WAL helper was bypassed). The fix for that is "all QA/review reply paths MUST go through `send_message_with_wal`" — enforced by code review and a unit test that asserts no direct `send_message()` call exists in the QA/review reply paths.

---

## Open items deferred to a v8 plan

- **Automated duplicate-detection alerting**: cron-driven job that runs the detection query hourly and posts unresolved groups to a debug channel. v7 ships the recipe + helper but leaves invocation manual; v8 wires the alert. Suppression remains impossible without Telegram API support.
- **Commodore commits as himself** (rotate `leviathan-agent` → `leviathan-commodore` PAT once GitHub Support resolves): credential swap, no code change.
- **Per-repo push permission** (eliminate fork hop): org grant.
- **Q&A learning loop** (decline-pattern review).
- **Plan-refinement context summarization** (>20 turns).
- **Auto-PR from review pipeline** (review → build).
- **Runbook rename to `OPERATOR_RUNBOOK.md`**: separate isolated PR with caller sweep.
- **Scratch-dir GC for permanently-orphaned files**: daily sweep of `<uuid>.result.json` files >7 days old whose `<uuid>` is in a terminal SQLite state.
- **Closing the duplicate window entirely**: would require either (a) Telegram-side idempotency token support (currently unavailable), (b) a webhook reply-receipt path that reaches us before our Telegram-API-call returns (architectural shift — we're a long-poll bot for good reasons), or (c) a "send-and-confirm-via-getUpdates" pattern where we post then poll for our own message and only then commit (slower; introduces own race). All three are non-trivial; the documented contract is the realistic limit for v6.

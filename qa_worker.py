#!/usr/bin/env python3
"""Fleet Commodore Q&A worker container entrypoint.

Pipeline:
  1. Read job dict from stdin: {qa_uuid, question, requester, channel}
  2. Pre-flight safety: hostile-question patterns decline before invoking Claude
  3. Spawn Claude CLI with the formatted Q&A prompt + tools allowlisted to:
       - Read, Grep, Glob (against /app/knowledge — mounted ro by launcher)
       - Bash(commodore-db:*) and Bash(commodore-orm:*) (existing wrappers)
       - WebFetch (limited to *.leviathannews.xyz at the egress layer)
     NO GitHub tools; NO arbitrary shell.
  4. Parse Claude's reply: STATUS, ANSWER (or DECLINED_REASON), CITATIONS
  5. Atomic scratch write to /var/run/commodore-results/<uuid>.result.json
  6. Emit ONE JSON object on stdout

Single exit point: emit() writes one JSON object then sys.exit. Errors are
wrapped in the same envelope so the parent always parses cleanly.

The atomic scratch-file write is the durability boundary (same as the build
worker) — if we crash after the file appears at its final name, the host
coordinator's recovery path picks up the answer and posts it without
relaunching us.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


# --- Configuration ---------------------------------------------------------

# Resolve at import time. npm-global on different distros plants the
# `claude` symlink at /usr/local/bin OR /usr/bin depending on prefix.
# Hardcoding either is fragile; PATH lookup is robust.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "/usr/bin/claude"

# Container path for the result-scratch handoff. Bound by the launcher to
# the host's RESULTS_DIR.
RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "/var/run/commodore-results"))

# Knowledge corpus root inside the container. The launcher bind-mounts host
# subpaths under here read-only.
KNOWLEDGE_ROOT = Path(os.environ.get("QA_KNOWLEDGE_ROOT", "/app/knowledge"))

CLAUDE_TIMEOUT_S = int(os.environ.get("QA_CLAUDE_TIMEOUT_S", "240"))


# --- Single-exit emitter ---------------------------------------------------

def emit(obj: dict, exit_code: int = 0) -> "None":
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
    sys.exit(exit_code)


# --- Atomic scratch-file write ---------------------------------------------

def write_result_atomically(uuid: str, payload: dict) -> None:
    """Write `<uuid>.result.json` to RESULTS_DIR using temp + fsync + rename.
    Same protocol as build_worker / review_worker."""
    final_path = RESULTS_DIR / f"{uuid}.result.json"
    temp_path = RESULTS_DIR / f"{uuid}.result.json.tmp"
    with open(temp_path, "w") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.rename(temp_path, final_path)
    dir_fd = os.open(str(RESULTS_DIR), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# --- Hostile-question pre-flight ------------------------------------------
#
# The authoritative safety boundary is the commodore_reader Postgres role
# (which lacks SELECT on email/sessions/credentials/etc). The parser gates
# in commodore-db / commodore-orm catch accidents. THIS list is layer 3:
# decline obvious hostile questions before we even spawn Claude, so we
# don't burn tokens on inevitable refusals and we generate cleaner audit
# trails.
#
# Match is case-insensitive substring. Keep narrow — false positives erode
# user trust faster than false negatives erode security (the role REVOKEs
# are the real boundary).

HOSTILE_PATTERNS = [
    # Credential extraction
    "password", "passwords", "passphrase",
    "private key", "wallet seed", "wallet keys",
    "mnemonic", "seed phrase",
    "api key", "api keys", "auth token",
    "bot token", "telegram token",
    "ssh key", "private ssh",
    # PII fishing
    "real name", "home address", "phone number",
    "email address of", "email addresses of",
    # Session / auth bypass attempts
    "session token", "session cookie", "jwt token",
    "csrf token",
]


def matches_hostile(question: str) -> "str | None":
    """Returns the first matched pattern, or None."""
    lower = (question or "").lower()
    for pat in HOSTILE_PATTERNS:
        if pat in lower:
            return pat
    return None


# --- Claude CLI invocation -------------------------------------------------

QA_PROMPT_TEMPLATE = """You are the Fleet Commodore answering a read-only Q&A question
about the Leviathan News project. Speak as a senior naval officer — formal, concise,
no faked data.

Allowed sources, in order of preference:
  1. /app/knowledge/  — local mounted dev-journal entries, docs, CLAUDE.md, README.md.
                         Use Read / Grep / Glob to search.
  2. commodore-db / commodore-orm — read-only Postgres queries via the wrappers.
                         Use Bash(commodore-db: ...) or Bash(commodore-orm: ...).
                         The role is SELECT-only with hardened REVOKEs; do NOT try
                         to bypass them.
  3. WebFetch against *.leviathannews.xyz only (egress filter enforces).

DO NOT:
  - Reveal credentials, passwords, API keys, wallet keys, seed phrases, session
    tokens, PII, or anything else the operator has not authorized.
  - Speculate. If you don't have grounded sources, say so.
  - Pretend to have run a tool you didn't run.
  - Quote the literal contents of any credential file or env var.

OUTPUT FORMAT (strict — the parser depends on it):
First line MUST be exactly one of:
  STATUS: ANSWERED
  STATUS: DECLINED

If ANSWERED:
  Following lines are the answer body, plain prose, max ~3500 chars.
  After the body, OPTIONAL block:
  CITATIONS:
    - <path or URL>
    - <path or URL>

If DECLINED:
  Following line: REASON: <one short sentence in character>

Question from @{requester} in chat {channel}:
{question}
"""


def run_claude_qa(prompt: str) -> str:
    """Spawn Claude CLI with the prompt on stdin, restricted tool set.

    The --allowed-tools flag scopes what Claude can call. The Q&A worker
    needs Read+Grep+Glob (corpus), Bash for the two DB wrappers, and
    WebFetch (the egress proxy enforces the host allowlist). Anything else
    — Edit, Write, NotebookEdit, etc. — is denied at the CLI boundary.
    """
    allowed_tools = [
        "Read", "Grep", "Glob",
        "Bash(commodore-db:*)",
        "Bash(commodore-orm:*)",
        "WebFetch",
    ]
    try:
        proc = subprocess.run(
            [CLAUDE_BIN,
             "--print",
             "--output-format", "text",
             "--allowed-tools", ",".join(allowed_tools)],
            input=prompt,
            capture_output=True, text=True,
            timeout=CLAUDE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        sys.stderr.write(f"claude run failed: {exc}\n")
        return ""
    if proc.returncode != 0:
        sys.stderr.write(f"claude rc={proc.returncode} stderr={proc.stderr[:500]}\n")
        return ""
    return (proc.stdout or "").strip()


# --- Reply parsing --------------------------------------------------------

_STATUS_RE = re.compile(r"^\s*STATUS\s*:\s*(ANSWERED|DECLINED)\s*$",
                        re.IGNORECASE | re.MULTILINE)
_REASON_RE = re.compile(r"^\s*REASON\s*:\s*(.+)$",
                        re.IGNORECASE | re.MULTILINE)
_CITATIONS_BLOCK_RE = re.compile(
    r"^\s*CITATIONS\s*:\s*\n((?:\s*[-*]\s+.+\n?)+)",
    re.IGNORECASE | re.MULTILINE,
)
_CITATION_LINE_RE = re.compile(r"^\s*[-*]\s+(.+?)$", re.MULTILINE)


def parse_qa(claude_text: str) -> dict:
    """Parse Claude's reply into a structured payload.

    Returns {status, answer, declined_reason, citations}. status is one of
    'answered' | 'declined' | 'unparseable'.
    """
    if not claude_text:
        return {"status": "unparseable", "answer": "", "declined_reason": "",
                "citations": []}

    status_m = _STATUS_RE.search(claude_text)
    if not status_m:
        return {"status": "unparseable", "answer": "", "declined_reason": "",
                "citations": [], "raw_excerpt": claude_text[:500]}

    status = status_m.group(1).lower()
    if status == "declined":
        reason_m = _REASON_RE.search(claude_text)
        reason = reason_m.group(1).strip() if reason_m else "policy decline"
        return {"status": "declined", "answer": "", "declined_reason": reason[:500],
                "citations": []}

    # Extract answer body: everything after the STATUS line, minus any
    # trailing CITATIONS block.
    body_start = status_m.end()
    cite_m = _CITATIONS_BLOCK_RE.search(claude_text, body_start)
    if cite_m:
        body_end = cite_m.start()
        cite_block = cite_m.group(1)
        citations = [c.strip() for c in _CITATION_LINE_RE.findall(cite_block)]
    else:
        body_end = len(claude_text)
        citations = []

    body = claude_text[body_start:body_end].strip()
    return {
        "status": "answered",
        "answer": body[:3800],
        "declined_reason": "",
        "citations": citations[:5],
    }


# --- Main ------------------------------------------------------------------

def version_banner() -> dict:
    """Image smoke-test path. Mirror review_worker so deploys can probe both
    workers identically."""
    import shutil
    components = {}
    for name in ("python3", "bash", "git", "node", "claude"):
        path = shutil.which(name)
        if not path:
            components[name] = {"present": False, "path": None, "version": None}
            continue
        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            version = (result.stdout or result.stderr or "").strip().splitlines()[0][:100]
        except Exception as exc:
            version = f"probe-failed: {type(exc).__name__}"
        components[name] = {"present": True, "path": path, "version": version}
    py_deps = {}
    for mod in ("sqlparse", "psycopg2", "requests"):
        try:
            imported = __import__(mod)
            py_deps[mod] = {"present": True,
                            "version": getattr(imported, "__version__", "unknown")}
        except ImportError as exc:
            py_deps[mod] = {"present": False, "error": str(exc)}
    knowledge_present = KNOWLEDGE_ROOT.is_dir() and any(KNOWLEDGE_ROOT.iterdir())
    all_present = all(c["present"] for c in components.values()) and all(
        d["present"] for d in py_deps.values()
    )
    return {
        "mode": "version",
        "worker": "qa",
        "all_components_present": all_present,
        "components": components,
        "python_deps": py_deps,
        "knowledge_present": knowledge_present,
    }


def main() -> "None":
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        banner = version_banner()
        emit(banner, exit_code=0 if banner["all_components_present"] else 1)

    try:
        raw = sys.stdin.read()
        if not raw.strip():
            emit({"error": "no_job", "detail": "stdin was empty"}, exit_code=2)
        job = json.loads(raw)
    except json.JSONDecodeError as exc:
        emit({"error": "bad_job_json", "detail": str(exc)}, exit_code=2)

    required = {"qa_uuid", "question"}
    missing = required - set(job.keys())
    if missing:
        emit({"error": "missing_job_fields", "missing": sorted(missing)},
             exit_code=2)

    qa_uuid = str(job["qa_uuid"])
    question = str(job["question"])
    requester = str(job.get("requester") or "?")
    channel = str(job.get("channel") or "?")

    # Layer 3 hostile-question pre-flight.
    hit = matches_hostile(question)
    if hit:
        payload = {
            "qa_uuid": qa_uuid,
            "status": "declined",
            "answer": "",
            "declined_reason": (
                f"The Admiralty does not entertain enquiries that touch on "
                f"credentials, secrets, or personal information ({hit!r})."
            ),
            "citations": [],
        }
        try:
            write_result_atomically(qa_uuid, payload)
        except OSError as exc:
            sys.stderr.write(f"scratch write failed: {exc}\n")
        emit(payload, exit_code=0)

    prompt = QA_PROMPT_TEMPLATE.format(
        requester=requester[:50],
        channel=channel[:30],
        question=question[:1500],
    )

    claude_out = run_claude_qa(prompt)
    parsed = parse_qa(claude_out)

    if parsed["status"] == "unparseable":
        # Record as failed-to-scratch so coordinator posts the casualty
        # message instead of relaunching.
        payload = {
            "qa_uuid": qa_uuid,
            "status": "declined",
            "answer": "",
            "declined_reason": (
                "The Admiralty's archivist returned an answer the "
                "messengers could not transcribe. Pray re-issue the inquiry."
            ),
            "citations": [],
            "claude_excerpt": parsed.get("raw_excerpt", "")[:300],
        }
        try:
            write_result_atomically(qa_uuid, payload)
        except OSError as exc:
            sys.stderr.write(f"scratch write failed: {exc}\n")
        emit(payload, exit_code=0)

    payload = {
        "qa_uuid": qa_uuid,
        "status": parsed["status"],
        "answer": parsed["answer"],
        "declined_reason": parsed["declined_reason"],
        "citations": parsed["citations"],
    }
    # Atomic write FIRST — durability boundary.
    try:
        write_result_atomically(qa_uuid, payload)
    except OSError as exc:
        sys.stderr.write(f"scratch write failed (still emitting stdout): {exc}\n")

    emit(payload, exit_code=0)


if __name__ == "__main__":
    main()

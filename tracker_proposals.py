"""Durable proposal-only inbox for canonical tracker review.

This module never invokes ``bd``, GitHub, a subprocess, or the network.  It
records immutable suggestions from Fleet so an authorized interactive actor
can review and apply them through the canonical Squid Bot tracker bridge.
Stored request text is available only through the local export path; model
facing inspection returns the bounded proposal and non-content provenance.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping
import uuid


ALLOWED_REPOSITORY = "leviathan-news/squid-bot"
OPERATIONS = {"create", "update", "review"}
MAX_ITEMS = 30
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100
MAX_EXPORT_LIMIT = 1000
DB_NAME = "tracker-proposals.db"
MAX_PROPOSALS = 1000
MAX_INBOX_BYTES = 32 * 1024 * 1024
RECORD_OVERHEAD_ALLOWANCE = 512
PROPOSAL_TTL_DAYS = 7
DB_TIMEOUT_SECONDS = 5.0


class ProposalError(ValueError):
    """Base class for a rejected proposal operation."""


class ProposalConflict(ProposalError):
    """A QA UUID was reused for content other than its original request."""


class ProposalNotFound(ProposalError):
    """No proposal has the requested identifier."""


class ProposalScopeMismatch(ProposalError):
    """The inspection request does not match the proposal's host scope."""


class ProposalStoreUnavailable(ProposalError):
    """The durable inbox does not exist or cannot be read safely."""


class ProposalStoreFull(ProposalStoreUnavailable):
    """The bounded durable inbox has no capacity for another proposal."""


_PROPOSAL_KEYS = {"repository", "summary", "items"}
_ITEM_KEYS = {
    "title", "description", "bead_id", "github_number", "operation",
    "priority", "owner", "due", "evidence",
}
_PROVENANCE_KEYS = {
    "chat_id", "topic_id", "requester_id", "request_msg_id", "request_text",
    "attachment_name", "attachment_sha256",
}
_SCOPE_KEYS = ("chat_id", "topic_id", "requester_id")


def _text(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ProposalError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ProposalError(f"{field} must not be empty")
    if len(value) > limit:
        raise ProposalError(f"{field} exceeds {limit} characters")
    return value


def _integer(value: Any, field: str, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProposalError(f"{field} must be an integer from {minimum} through {maximum}")
    return value


def _host_id(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ProposalError(f"{field} must be a string or integer")
    result = str(value).strip()
    if not result or len(result) > 128:
        raise ProposalError(f"{field} must contain 1 through 128 characters")
    return result


def _canonical_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ProposalError("qa_uuid must be a UUID string")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ProposalError("qa_uuid must be a UUID string") from exc


def _normalize_proposal(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProposalError("proposal must be an object")
    unknown = set(value) - _PROPOSAL_KEYS
    missing = _PROPOSAL_KEYS - set(value)
    if unknown or missing:
        detail = "unknown: " + ", ".join(sorted(unknown)) if unknown else "missing: " + ", ".join(sorted(missing))
        raise ProposalError(f"proposal fields invalid ({detail})")
    if value["repository"] != ALLOWED_REPOSITORY:
        raise ProposalError(f"repository must be {ALLOWED_REPOSITORY}")
    summary = _text(value["summary"], "summary", 2000, required=True)
    raw_items = value["items"]
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= MAX_ITEMS:
        raise ProposalError(f"items must contain 1 through {MAX_ITEMS} objects")
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items):
        field = f"items[{index}]"
        if not isinstance(raw, Mapping):
            raise ProposalError(f"{field} must be an object")
        unknown = set(raw) - _ITEM_KEYS
        missing = _ITEM_KEYS - set(raw)
        if unknown or missing:
            detail = "unknown: " + ", ".join(sorted(unknown)) if unknown else "missing: " + ", ".join(sorted(missing))
            raise ProposalError(f"{field} fields invalid ({detail})")
        operation = raw["operation"]
        if operation not in OPERATIONS:
            raise ProposalError(f"{field}.operation must be create, update, or review")
        items.append({
            "title": _text(raw["title"], f"{field}.title", 200, required=True),
            "description": _text(raw["description"], f"{field}.description", 4000),
            "bead_id": _text(raw["bead_id"], f"{field}.bead_id", 128),
            "github_number": _integer(raw["github_number"], f"{field}.github_number"),
            "operation": operation,
            "priority": _integer(raw["priority"], f"{field}.priority", maximum=4),
            "owner": _text(raw["owner"], f"{field}.owner", 200),
            "due": _text(raw["due"], f"{field}.due", 64),
            "evidence": _text(raw["evidence"], f"{field}.evidence", 4000),
        })
    return {"repository": ALLOWED_REPOSITORY, "summary": summary, "items": items}


def _normalize_provenance(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProposalError("provenance is required and must be an object")
    unknown = set(value) - _PROVENANCE_KEYS
    if unknown:
        raise ProposalError("unknown provenance fields: " + ", ".join(sorted(unknown)))
    required = {"chat_id", "topic_id", "requester_id", "request_msg_id", "request_text"}
    missing = required - set(value)
    if missing:
        raise ProposalError("missing provenance fields: " + ", ".join(sorted(missing)))
    attachment_name = _text(value.get("attachment_name", ""), "attachment_name", 255)
    attachment_sha256 = _text(value.get("attachment_sha256", ""), "attachment_sha256", 64).lower()
    if bool(attachment_name) != bool(attachment_sha256):
        raise ProposalError("attachment_name and attachment_sha256 must be supplied together")
    if attachment_sha256 and (
        len(attachment_sha256) != 64
        or any(char not in "0123456789abcdef" for char in attachment_sha256)
    ):
        raise ProposalError("attachment_sha256 must be 64 lowercase hexadecimal characters")
    return {
        "chat_id": _host_id(value["chat_id"], "chat_id"),
        "topic_id": _host_id(value["topic_id"], "topic_id", nullable=True),
        "requester_id": _host_id(value["requester_id"], "requester_id"),
        "request_msg_id": _host_id(value["request_msg_id"], "request_msg_id"),
        "request_text": _text(value["request_text"], "request_text", 4000, required=True),
        "attachment_name": attachment_name,
        "attachment_sha256": attachment_sha256,
    }


def _normalize_scope(value: Any) -> dict[str, str | None]:
    if not isinstance(value, Mapping):
        raise ProposalError("inspection provenance is required")
    missing = set(_SCOPE_KEYS) - set(value)
    if missing:
        raise ProposalError("missing inspection provenance fields: " + ", ".join(sorted(missing)))
    return {
        "chat_id": _host_id(value["chat_id"], "chat_id"),
        "topic_id": _host_id(value["topic_id"], "topic_id", nullable=True),
        "requester_id": _host_id(value["requester_id"], "requester_id"),
    }


def _state_path(state_dir: str | os.PathLike[str] | None) -> Path:
    if state_dir is None:
        state_dir = (
            os.environ.get("TRACKER_PROPOSALS_STATE_DIR")
            or os.environ.get("FLEET_COMMODORE_STATE_DIR")
            or Path.home() / ".local" / "state" / "fleet-commodore"
        )
    return Path(state_dir).expanduser() / DB_NAME


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _proposal_id(qa_uuid: str) -> str:
    return "tp_" + hashlib.sha256(qa_uuid.encode("ascii")).hexdigest()[:24]


def _fingerprint(qa_uuid: str, proposal_json: str, provenance_json: str) -> str:
    material = _canonical_json({
        "qa_uuid": qa_uuid,
        "proposal": json.loads(proposal_json),
        "provenance": json.loads(provenance_json),
    })
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _create_database(path: Path) -> None:
    parent = path.parent
    parent_was_present = parent.exists()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_was_present:
        os.chmod(parent, 0o700)
    if path.is_symlink():
        raise ProposalError("proposal database path must not be a symlink")
    if not path.exists():
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
    with sqlite3.connect(path, timeout=DB_TIMEOUT_SECONDS) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracker_proposal (
                proposal_id TEXT PRIMARY KEY,
                qa_uuid TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,
                proposal_json TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                item_count INTEGER NOT NULL CHECK(item_count BETWEEN 1 AND 30),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status = 'proposed'),
                applied INTEGER NOT NULL CHECK(applied = 0)
            );
            CREATE INDEX IF NOT EXISTS tracker_proposal_created
                ON tracker_proposal(created_at DESC, proposal_id DESC);
            """
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tracker_proposal)")}
        if "expires_at" not in columns:
            connection.execute("ALTER TABLE tracker_proposal ADD COLUMN expires_at TEXT")
            rows = connection.execute(
                "SELECT proposal_id, created_at FROM tracker_proposal"
            ).fetchall()
            for proposal_id, created_at in rows:
                try:
                    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    expires = created + timedelta(days=PROPOSAL_TTL_DAYS)
                except (AttributeError, TypeError, ValueError):
                    expires = datetime.now(timezone.utc)
                connection.execute(
                    "UPDATE tracker_proposal SET expires_at=? WHERE proposal_id=?",
                    (expires.isoformat(timespec="microseconds").replace("+00:00", "Z"), proposal_id),
                )
        connection.commit()


@contextmanager
def _writer(path: Path) -> Iterator[sqlite3.Connection]:
    try:
        _create_database(path)
        connection = sqlite3.connect(path, timeout=DB_TIMEOUT_SECONDS, isolation_level=None)
    except sqlite3.Error as exc:
        raise ProposalStoreUnavailable("proposal inbox is unavailable") from exc
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        yield connection
    except sqlite3.Error as exc:
        raise ProposalStoreUnavailable("proposal inbox is unavailable") from exc
    finally:
        connection.close()


@contextmanager
def _reader(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file() or path.is_symlink():
        raise ProposalStoreUnavailable("proposal inbox is unavailable")
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=DB_TIMEOUT_SECONDS,
        )
    except sqlite3.Error as exc:
        raise ProposalStoreUnavailable("proposal inbox is unavailable") from exc
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        yield connection
    except sqlite3.Error as exc:
        raise ProposalStoreUnavailable("proposal inbox is unavailable") from exc
    finally:
        connection.close()


def _receipt(row: sqlite3.Row) -> dict[str, Any]:
    try:
        expires_at = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        expired = datetime.now(timezone.utc) >= expires_at
    except (IndexError, KeyError, TypeError, ValueError):
        expired = True
    return {
        "proposal_id": row["proposal_id"],
        "status": "expired" if expired else "proposed",
        "applied": False,
        "item_count": row["item_count"],
        "source": f"tracker-proposal:{row['proposal_id']}",
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


def submit(
    *,
    qa_uuid: str,
    proposal: Mapping[str, Any],
    provenance: Mapping[str, Any],
    state_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Validate and durably record one immutable proposal.

    An exact replay of a ``qa_uuid`` returns its original receipt. Reusing the
    UUID with any changed proposal or provenance raises ``ProposalConflict``.
    """
    canonical_uuid = _canonical_uuid(qa_uuid)
    normalized_proposal = _normalize_proposal(proposal)
    normalized_provenance = _normalize_provenance(provenance)
    proposal_json = _canonical_json(normalized_proposal)
    provenance_json = _canonical_json(normalized_provenance)
    fingerprint = _fingerprint(canonical_uuid, proposal_json, provenance_json)
    identifier = _proposal_id(canonical_uuid)
    created = datetime.now(timezone.utc)
    created_at = created.isoformat(timespec="microseconds").replace("+00:00", "Z")
    expires_at = (
        created + timedelta(days=PROPOSAL_TTL_DAYS)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    path = _state_path(state_dir)

    with _writer(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                "SELECT * FROM tracker_proposal WHERE qa_uuid=?", (canonical_uuid,)
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise ProposalConflict("qa_uuid already belongs to a different tracker proposal request")
                connection.commit()
                return _receipt(existing)
            stored_bytes, stored_count = connection.execute(
                "SELECT COALESCE(SUM(length(CAST(proposal_json AS BLOB)) + "
                "length(CAST(provenance_json AS BLOB)) + ?), 0), COUNT(*) "
                "FROM tracker_proposal",
                (RECORD_OVERHEAD_ALLOWANCE,),
            ).fetchone()
            incoming_bytes = (
                len(proposal_json.encode("utf-8"))
                + len(provenance_json.encode("utf-8"))
                + RECORD_OVERHEAD_ALLOWANCE
            )
            if stored_count >= MAX_PROPOSALS or stored_bytes + incoming_bytes > MAX_INBOX_BYTES:
                raise ProposalStoreFull("proposal inbox capacity reached")
            connection.execute(
                "INSERT INTO tracker_proposal "
                "(proposal_id,qa_uuid,fingerprint,proposal_json,provenance_json,item_count,created_at,expires_at,status,applied) "
                "VALUES (?,?,?,?,?,?,?,?,'proposed',0)",
                (identifier, canonical_uuid, fingerprint, proposal_json, provenance_json,
                 len(normalized_proposal["items"]), created_at, expires_at),
            )
            row = connection.execute(
                "SELECT * FROM tracker_proposal WHERE proposal_id=?", (identifier,)
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return _receipt(row)


def _public_record(row: sqlite3.Row) -> dict[str, Any]:
    proposal = json.loads(row["proposal_json"])
    provenance = json.loads(row["provenance_json"])
    return {
        **_receipt(row),
        "repository": proposal["repository"],
        "summary": proposal["summary"],
        "items": proposal["items"],
        "provenance": {
            "chat_id": provenance["chat_id"],
            "topic_id": provenance["topic_id"],
            "requester_id": provenance["requester_id"],
            "request_msg_id": provenance["request_msg_id"],
            "attachment_name": provenance["attachment_name"],
            "attachment_sha256": provenance["attachment_sha256"],
        },
    }


def get(
    proposal_id: str,
    *,
    provenance: Mapping[str, Any],
    state_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Inspect a proposal only from its original chat/topic/requester scope."""
    scope = _normalize_scope(provenance)
    with _reader(_state_path(state_dir)) as connection:
        row = connection.execute(
            "SELECT * FROM tracker_proposal WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
    if row is None:
        raise ProposalNotFound("tracker proposal not found")
    stored = json.loads(row["provenance_json"])
    if any(stored[key] != scope[key] for key in _SCOPE_KEYS):
        raise ProposalScopeMismatch("tracker proposal belongs to a different request scope")
    return _public_record(row)


def get_for_job(
    qa_uuid: str,
    *,
    provenance: Mapping[str, Any],
    state_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any] | None:
    """Return an existing scoped receipt before a worker regenerates output.

    A missing inbox or UUID is normal on the first attempt and returns ``None``.
    Existing records remain immutable, so a broker retry can report the durable
    original receipt rather than asking a model to reconstruct its proposal.
    """
    canonical_uuid = _canonical_uuid(qa_uuid)
    scope = _normalize_scope(provenance)
    path = _state_path(state_dir)
    if not path.is_file() or path.is_symlink():
        return None
    with _reader(path) as connection:
        row = connection.execute(
            "SELECT * FROM tracker_proposal WHERE qa_uuid=?", (canonical_uuid,)
        ).fetchone()
    if row is None:
        return None
    stored = json.loads(row["provenance_json"])
    if any(stored[key] != scope[key] for key in _SCOPE_KEYS):
        raise ProposalScopeMismatch("tracker proposal belongs to a different request scope")
    return _receipt(row)


def _bounded_limit(value: Any, maximum: int) -> int:
    return _integer(value, "limit", minimum=1, maximum=maximum)


def list_proposals(
    *,
    state_dir: str | os.PathLike[str] | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return a bounded local index without original request text."""
    limit = _bounded_limit(limit, MAX_LIST_LIMIT)
    offset = _integer(offset, "offset", maximum=2**31 - 1)
    with _reader(_state_path(state_dir)) as connection:
        rows = connection.execute(
            "SELECT * FROM tracker_proposal ORDER BY created_at DESC, proposal_id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [_public_record(row) for row in rows]


def local_get(
    proposal_id: str,
    *,
    state_dir: str | os.PathLike[str] | None = None,
    include_private: bool = False,
) -> dict[str, Any]:
    """Read one record for the local canonical reviewer CLI."""
    with _reader(_state_path(state_dir)) as connection:
        row = connection.execute(
            "SELECT * FROM tracker_proposal WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
    if row is None:
        raise ProposalNotFound("tracker proposal not found")
    record = _public_record(row)
    if include_private:
        record["provenance"] = json.loads(row["provenance_json"])
    return record


def export(
    *,
    state_dir: str | os.PathLike[str] | None = None,
    limit: int = MAX_EXPORT_LIMIT,
) -> dict[str, Any]:
    """Export bounded immutable records, including private review provenance."""
    limit = _bounded_limit(limit, MAX_EXPORT_LIMIT)
    with _reader(_state_path(state_dir)) as connection:
        rows = connection.execute(
            "SELECT * FROM tracker_proposal ORDER BY created_at, proposal_id LIMIT ?", (limit + 1,)
        ).fetchall()
    truncated = len(rows) > limit
    records = []
    for row in rows[:limit]:
        record = _public_record(row)
        record["qa_uuid"] = row["qa_uuid"]
        record["provenance"] = json.loads(row["provenance_json"])
        records.append(record)
    return {
        "schema": "fleet-tracker-proposals-v1",
        "count": len(records),
        "truncated": truncated,
        "proposals": records,
    }

"""Bounded GitHub observations for the model's read-only evidence broker."""
from __future__ import annotations

from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
from urllib.parse import urlencode


REPOSITORIES = frozenset({
    "leviathan-news/squid-bot", "leviathan-news/auction-ui",
    "leviathan-news/be-benthic", "leviathan-news/agent-chat",
    "leviathan-news/fleet-commodore",
})
DEFAULT_REPOSITORY = "leviathan-news/squid-bot"
MAX_RESPONSE_BYTES = 512 * 1024


def _host_token() -> str:
    """Reuse the existing Fleet host credential, never expose it to the model."""
    path = Path(os.environ.get("GH_PAT_FILE", "~/.config/commodore/gh_pat")).expanduser()
    try:
        with path.open() as handle:
            value = handle.read(4097).strip()
        return value if value and len(value) <= 4096 and not any(c.isspace() for c in value) else ""
    except OSError:
        return ""


def _get(path: str):
    # Fixed host and GET path; never redirect an authenticated request or
    # pass the host token to subprocesses, tool results, or model prompts.
    connection = http.client.HTTPSConnection("api.github.com", timeout=10)
    try:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Fleet-Commodore-read-only-evidence",
        }
        token = _host_token()
        if token:
            headers["Authorization"] = "Bearer " + token
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            return {"error": "github_unavailable", "http_status": response.status}
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            return {"error": "github_response_too_large"}
        return json.loads(payload)
    except (OSError, http.client.HTTPException, ValueError):
        return {"error": "github_unavailable"}
    finally:
        connection.close()


def _pull(repo: str, row: dict) -> dict:
    number = row.get("number")
    if type(number) is not int or number <= 0 or row.get("state") not in {"open", "closed"}:
        raise ValueError("invalid pull metadata")
    # Construct canonical links from validated IDs, never trust response URLs.
    return {
        "source": f"https://github.com/{repo}/pull/{number}",
        "number": number, "title": str(row.get("title") or "")[:240],
        "body": str(row.get("body") or "")[:1800],
        "state": row["state"], "draft": row.get("draft") is True,
        "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
        "closed_at": row.get("closed_at"), "merged_at": row.get("merged_at"),
        "base": str((row.get("base") or {}).get("ref") or "")[:120],
    }


def retrieve(request: dict) -> dict:
    repo = request.get("repository")
    if not isinstance(repo, str) or repo not in REPOSITORIES:
        return {"error": "repository_not_available", "repositories": sorted(REPOSITORIES)}
    operation = request.get("request")
    if operation == "github_pulls":
        state, sort = request.get("state", "all"), request.get("sort", "created")
        if state not in ("all", "open", "closed") or sort not in ("created", "updated"):
            return {"error": "invalid_github_listing_options"}
        query = urlencode({"state": state, "sort": sort, "direction": "desc", "per_page": 5})
        path = f"/repos/{repo}/pulls?{query}"
        search = f"is:pr sort:{sort}-desc" + (f" is:{state}" if state != "all" else "")
        source = f"https://github.com/{repo}/pulls?{urlencode({'q': search})}"
    elif operation == "github_pull":
        number = request.get("number")
        if type(number) is not int or not 0 < number <= 10_000_000:
            return {"error": "invalid_pull_number"}
        path = f"/repos/{repo}/pulls/{number}"
        source = f"https://github.com/{repo}/pull/{number}"
    else:
        return {"error": "invalid_github_operation"}
    payload = _get(path)
    if isinstance(payload, dict) and "error" in payload:
        return payload
    try:
        if operation == "github_pulls":
            if not isinstance(payload, list) or len(payload) > 5:
                raise ValueError("invalid listing")
            rows = [_pull(repo, row) for row in payload]
        else:
            rows = [_pull(repo, payload)]
            if rows[0]["number"] != number:
                raise ValueError("mismatched pull")
    except (ValueError, TypeError, AttributeError):
        return {"error": "github_invalid_response"}
    return {
        "source": source, "source_kind": "current_github_observation",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "repository": repo, "results": rows,
        "scope": ({"state": state, "sort": sort, "direction": "desc", "limit": 5,
                   "complete_repository_history": False} if operation == "github_pulls"
                  else {"number": number}),
        "limitations": "PR state is not deployment or service-health evidence. Closed does not imply merged.",
    }

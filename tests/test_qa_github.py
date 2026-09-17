import io

import pytest

import qa_github


REPO = "leviathan-news/squid-bot"


def row(number=1133, **kwargs):
    return {"number": number, "title": "Restore LLM-authored replies", "body": "Details",
            "state": "closed", "created_at": "2026-09-17T11:06:00Z",
            "merged_at": "2026-09-17T11:07:00Z", "base": {"ref": "main"}, **kwargs}


def test_current_listing_has_scope_timestamp_and_canonical_links(monkeypatch):
    calls = []
    monkeypatch.setattr(qa_github, "_get", lambda path: calls.append(path) or [row(html_url="https://evil.invalid")])
    result = qa_github.retrieve({"request": "github_pulls", "repository": REPO})
    assert calls == ["/repos/leviathan-news/squid-bot/pulls?state=all&sort=created&direction=desc&per_page=5"]
    assert result["observed_at"]
    assert result["scope"] == {"state": "all", "sort": "created", "direction": "desc", "limit": 5,
                               "complete_repository_history": False}
    assert result["results"][0]["source"] == f"https://github.com/{REPO}/pull/1133"
    assert result["results"][0]["merged_at"]


@pytest.mark.parametrize("decision", [
    {"request": "github_pulls", "repository": "other/private"},
    {"request": "github_pulls", "repository": REPO + "/../../secrets"},
    {"request": "github_pulls", "repository": REPO, "state": "merged"},
    {"request": "github_pulls", "repository": REPO, "sort": "arbitrary"},
    {"request": "github_pull", "repository": REPO, "number": True},
    {"request": "github_pull", "repository": REPO, "number": "1133"},
    {"request": "delete", "repository": REPO},
])
def test_unavailable_requests_never_reach_network(monkeypatch, decision):
    monkeypatch.setattr(qa_github, "_get", lambda *_: pytest.fail("network reached"))
    assert "error" in qa_github.retrieve(decision)


def test_closed_pr_is_not_assumed_merged_and_bodies_bounded(monkeypatch):
    monkeypatch.setattr(qa_github, "_get", lambda _: row(merged_at=None, body="x" * 10_000))
    result = qa_github.retrieve({"request": "github_pull", "repository": REPO, "number": 1133})
    assert result["results"][0]["merged_at"] is None
    assert len(result["results"][0]["body"]) == 1800


def test_error_has_no_current_source(monkeypatch):
    monkeypatch.setattr(qa_github, "_get", lambda _: {"error": "github_unavailable", "http_status": 403})
    result = qa_github.retrieve({"request": "github_pulls", "repository": REPO})
    assert result["error"] and "source" not in result and "observed_at" not in result


@pytest.mark.parametrize("status,payload", [(200, b"x" * (qa_github.MAX_RESPONSE_BYTES + 1)),
                                           (302, b"redirect"), (403, b"denied"), (200, b"bad json")])
def test_transport_bounded_get_no_credentials_or_redirects(monkeypatch, status, payload):
    events = []

    class Response(io.BytesIO):
        pass

    class Connection:
        def __init__(self, host, timeout):
            assert host == "api.github.com" and timeout == 10

        def request(self, method, path, headers):
            events.append(method)
            assert method == "GET" and "Authorization" not in headers

        def getresponse(self):
            response = Response(payload)
            response.status = status
            return response

        def close(self):
            events.append("closed")

    monkeypatch.setattr(qa_github.http.client, "HTTPSConnection", Connection)
    assert "error" in qa_github._get("/repos/leviathan-news/squid-bot/pulls")
    assert events == ["GET", "closed"]


def test_specific_pull_number_must_match(monkeypatch):
    monkeypatch.setattr(qa_github, "_get", lambda _: row(number=2))
    assert "error" in qa_github.retrieve({"request": "github_pull", "repository": REPO, "number": 1133})

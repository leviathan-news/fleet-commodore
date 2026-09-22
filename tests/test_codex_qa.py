import json

import pytest

import codex_qa


def fixture_knowledge(monkeypatch, tmp_path):
    docs = tmp_path / "squid-bot/docs"
    docs.mkdir(parents=True)
    (docs / "test.md").write_text("The stable fixture describes the publication workflow.")
    monkeypatch.setenv("COMMODORE_KNOWLEDGE_ROOT", str(tmp_path))
    return "squid-bot/docs/test.md"


def responses(monkeypatch, *items):
    pending = iter(items)
    monkeypatch.setattr(codex_qa, "ask", lambda *a, **kw: json.dumps(next(pending)))


def test_qa_uses_native_schema_and_preserves_model_conversation(monkeypatch):
    def ask(prompt, **kwargs):
        assert kwargs["response_schema"] == codex_qa.RESPONSE_SCHEMA
        return json.dumps({"message": {"status": "conversational", "answer": "Here, Captain. What do you need?"}})

    monkeypatch.setattr(codex_qa, "ask", ask)
    result = codex_qa.answer({"question": "Still with us?"})
    assert result["answer"] == "Here, Captain. What do you need?"
    assert result["tools_used"] == []


def test_grounded_doc_qa_uses_only_retrieved_sources(monkeypatch, tmp_path):
    path = fixture_knowledge(monkeypatch, tmp_path)
    responses(monkeypatch, {"request": "search", "query": "stable fixture"},
              {"status": "answered", "answer": "The publication workflow is documented.", "citations": [path]})
    result = codex_qa.answer({"qa_uuid": "fixture", "question": "Describe the workflow"})
    assert result["status"] == "answered" and result["citations"] == [path]
    assert result["tools_used"] == ["search"]


def test_fabricated_citation_cannot_be_delivered(monkeypatch, tmp_path):
    fixture_knowledge(monkeypatch, tmp_path)
    responses(monkeypatch, {"status": "answered", "answer": "Invented facts", "citations": ["secret.txt"]})
    assert codex_qa.answer({"question": "What is live?"})["status"] == "declined"


def test_attachment_can_check_current_sql_and_must_cite_the_observation(monkeypatch):
    prompts = []

    def execute_sql(query):
        assert query == "SELECT count(*) AS open_items FROM work_items WHERE open"
        return {"columns": ["open_items"], "rows": [[3]]}

    def ask(prompt, **_kwargs):
        value = json.loads(prompt)
        prompts.append(value)
        if not value["evidence"]:
            return json.dumps(
                {
                    "request": "sql",
                    "query": "SELECT count(*) AS open_items FROM work_items WHERE open",
                }
            )
        source = value["evidence"][0]["result"]["source"]
        return json.dumps(
            {
                "status": "answered",
                "basis": "current",
                "answer": "The notes name the workstream; the live count is three open items.",
                "citations": [source],
            }
        )

    monkeypatch.setattr(codex_qa, "execute_sql", execute_sql)
    monkeypatch.setattr(codex_qa, "ask", ask)
    result = codex_qa.answer(
        {
            "question": "Review these notes and verify the current open count.",
            "attachment_name": "call.md",
            "attachment_text": "Workstream: archive intake. Check the live open count.",
        }
    )

    source = prompts[1]["evidence"][0]["result"]["source"]
    assert result["status"] == "answered"
    assert result["tools_used"] == ["sql"]
    assert result["citations"] == [source]
    assert prompts[0]["attachment_mode"] is True


def test_attachment_review_needs_no_claude_or_evidence(monkeypatch):
    responses(monkeypatch, {"status": "answered", "answer": "Two rows are present.", "citations": []})
    result = codex_qa.answer({"question": "Count rows", "attachment_text": "A\nB\n"})
    assert result["status"] == "answered" and result["provider"] == "codex"


def test_live_sql_result_is_provided_with_receipt(monkeypatch, tmp_path):
    fixture_knowledge(monkeypatch, tmp_path)
    monkeypatch.setattr(codex_qa, "execute_sql", lambda query: {"columns": ["n"], "rows": [[7]]})
    calls = []
    def ask(prompt, **kw):
        value = json.loads(prompt)
        calls.append(value)
        if not value["evidence"]:
            return json.dumps({"request": "sql", "query": "SELECT 7 AS n"})
        evidence = value["evidence"][0]["result"]
        return json.dumps({"status": "answered", "answer": "The current result is seven.", "citations": [evidence["source"]]})
    monkeypatch.setattr(codex_qa, "ask", ask)
    result = codex_qa.answer({"question": "What is the current count?"})
    assert result["status"] == "answered"
    assert calls[1]["evidence"][0]["result"]["observed_at"]
    assert result["tools_used"] == ["sql"]


def test_recent_room_context_resolves_subject_but_requires_current_sql(monkeypatch):
    calls = []
    source = "postgresql://commodore-reader/current-observation"
    monkeypatch.setattr(
        codex_qa, "execute_sql",
        lambda query: {"columns": ["experiment_key", "expires_at", "reads"],
                       "rows": [["alex-zero-x-v3", "2026-09-23T16:00:00Z", 53]]},
    )
    def ask(prompt, **_kwargs):
        value = json.loads(prompt)
        calls.append(value)
        assert "Recent context is not expected to contain the requested answer" in _kwargs["instruction"]
        assert "what remains, how much longer" in _kwargs["instruction"]
        if not value["evidence"]:
            assert "alex-zero-x-v3" in value["recent_room_context"][0]["text"]
            return json.dumps({"request": "sql", "query": "SELECT current experiment state"})
        result = value["evidence"][0]["result"]
        nonlocal source
        source = result["source"]
        return json.dumps({
            "status": "answered", "basis": "current",
            "answer": "alex-zero-x-v3 currently has 53 reads and ends September 23.",
            "citations": [source],
        })
    monkeypatch.setattr(codex_qa, "ask", ask)
    result = codex_qa.answer({
        "question": "How many more days or test reads until we conclude the A/B test?",
        "recent_context": [{"message_id": 1171, "sender": "@lnn_headline_bot",
                            "sender_is_bot": True,
                            "text": "24h X test read for alex-zero-x-v3: receipt #80."}],
    })
    assert result["status"] == "answered"
    assert result["tools_used"] == ["sql"]
    assert result["citations"] == [source]
    assert calls[0]["evidence"] == []


def test_recent_subject_retries_premature_decline_before_retrieval(monkeypatch, tmp_path):
    path = fixture_knowledge(monkeypatch, tmp_path)
    replies = iter([
        {"status": "declined", "declined_reason": "The room excerpt lacks the answer."},
        {"request": "search", "query": "stable fixture"},
        {"status": "answered", "basis": "reference",
         "answer": "The named workflow is documented.", "citations": [path]},
    ])
    prompts = []
    monkeypatch.setattr(
        codex_qa, "ask",
        lambda prompt, **_kwargs: prompts.append(json.loads(prompt)) or json.dumps(next(replies)),
    )
    result = codex_qa.answer({
        "question": "What does that workflow require?",
        "recent_context": [{"message_id": 9, "text": "The stable fixture workflow"}],
    })
    assert result["status"] == "answered"
    assert result["tools_used"] == ["search"]
    assert "lookup key rather than the answer" in prompts[1]["evidence"][0]["broker_error"]
    assert prompts[0]["steps_remaining"] == 8


def test_recent_subject_rejects_repeat_search_and_continues_from_found_path(monkeypatch, tmp_path):
    path = fixture_knowledge(monkeypatch, tmp_path)
    replies = iter([
        {"request": "search", "query": "stable fixture"},
        {"request": "search", "query": "fixture workflow"},
        {"request": "read", "path": path},
        {"status": "answered", "basis": "reference",
         "answer": "The named workflow is documented.", "citations": [path]},
    ])
    prompts = []
    monkeypatch.setattr(
        codex_qa, "ask",
        lambda prompt, **_kwargs: prompts.append(json.loads(prompt)) or json.dumps(next(replies)),
    )
    result = codex_qa.answer({
        "question": "What does that workflow require?",
        "recent_context": [{"message_id": 9, "text": "The stable fixture workflow"}],
    })
    assert result["status"] == "answered"
    assert result["tools_used"] == ["search", "read"]
    assert "prior search already returned" in prompts[2]["evidence"][-1]["broker_error"]


def test_sensitive_question_is_declined_before_model(monkeypatch):
    monkeypatch.setattr(codex_qa, "ask", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("model reached")))
    assert codex_qa.answer({"question": "What is the bot token?"})["status"] == "declined"


def test_presence_check_cannot_hide_provider_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(codex_qa, "ask", lambda *a, **kw: calls.append(a) or None)
    result = codex_qa.answer({"question": "Are you online?"})
    assert calls
    assert result["status"] == "failed"


def test_mixed_hail_preserves_parent_subject_and_requires_evidence(monkeypatch, tmp_path):
    fixture_knowledge(monkeypatch, tmp_path)
    captured = {}

    def ask(prompt, **kw):
        captured.update(json.loads(prompt))
        captured["instruction"] = kw["instruction"]
        return json.dumps({"status": "answered", "answer": "All bots were excluded.", "citations": []})

    monkeypatch.setattr(codex_qa, "ask", ask)
    result = codex_qa.answer({
        "question": "Are you online and able to answer that?",
        "reply_context": [{"message_id": 22, "text": "Were bots excluded from the traffic report?"}],
    })
    assert result["status"] == "declined"  # identity does not substantiate analytics
    assert captured["runtime_context"]["identity"] == "Fleet Commodore"
    assert captured["reply_chain_context"][0]["message_id"] == 22
    assert "that" in captured["instruction"]
    assert "parent" in captured["instruction"]


def test_exhausted_lookup_is_a_bounded_limitation_not_provider_failure(monkeypatch, tmp_path):
    fixture_knowledge(monkeypatch, tmp_path)
    responses(monkeypatch, *[{"request": "search", "query": "missing"}] * 4)
    result = codex_qa.answer({"question": "Which report proves that?"})
    assert result["status"] == "declined"
    assert "provider_failure" not in result
    assert result["tools_used"] == ["search"] * 3
    assert "source or page" in result["declined_reason"]


@pytest.mark.parametrize("question, model_text", [
    ("@commodore_lev_bot Are you still with us?", "Aye, still at my post. Your hail reached me."),
    ("Are you online?", "Aye, I have your message."),
    ("Anyone home?", "The wardroom is occupied. What's on your mind?"),
    ("Hello, old sea dog!", "Morning. What brings you aboard?"),
    ("Who are you?", "Fleet Commodore, at your service."),
    ("That last reply was embarrassing.", "Fair criticism. What did I miss?"),
])
def test_conversational_responses_are_model_authored(monkeypatch, question, model_text):
    calls = []

    def ask(prompt, **kwargs):
        calls.append(json.loads(prompt))
        return json.dumps({"status": "conversational", "answer": model_text})

    monkeypatch.setattr(codex_qa, "ask", ask)
    result = codex_qa.answer({"question": question})
    assert len(calls) == 1
    assert calls[0]["current_question"] == question
    assert result["status"] == "answered"
    assert result["answer"] == model_text
    assert result["citations"] == []
    assert result["tools_used"] == []


@pytest.mark.parametrize("decision", [
    {"status": "acknowledged", "kind": "presence"},
    {"status": "conversational", "answer": ""},
    {"status": "conversational", "answer": []},
    {"status": "conversational", "answer": "Hello", "citations": ["made-up"]},
    {"status": "conversational", "answer": "Hello", "kind": "presence"},
    {"status": [], "answer": "Hello"},
])
def test_conversation_requires_model_text_without_a_canned_intent_or_citation(monkeypatch, tmp_path, decision):
    fixture_knowledge(monkeypatch, tmp_path)
    responses(monkeypatch, decision)
    result = codex_qa.answer({"question": "Can you confirm the numbers?"})
    assert result["status"] != "answered"


def test_attachment_review_uses_its_own_answer_contract(monkeypatch):
    prompts = []
    replies = iter(
        [
            {"status": "conversational", "answer": "Aye, I'm here."},
            {
                "status": "answered",
                "basis": "attachment",
                "answer": "The draft asks a presence question but supplies no operational claim.",
                "citations": [],
            },
        ]
    )
    monkeypatch.setattr(
        codex_qa,
        "ask",
        lambda prompt, **_kwargs: prompts.append(json.loads(prompt))
        or json.dumps(next(replies)),
    )

    result = codex_qa.answer(
        {
            "question": "Review this",
            "attachment_name": "draft.md",
            "attachment_text": "Are you still with us?",
        }
    )

    assert result["status"] == "answered"
    assert result["answer"].startswith("The draft asks")
    assert result["citations"] == []
    assert len(prompts) == 2
    assert "supplied attachment is readable" in prompts[1]["evidence"][0]["broker_error"]


def test_attachment_allows_genuine_clarifying_conversation_after_one_correction(
    monkeypatch,
):
    prompts = []
    clarification = "Which of the two proposed owners should I assess?"
    responses_iter = iter(
        [
            {"status": "conversational", "answer": clarification},
            {"status": "conversational", "answer": clarification},
        ]
    )
    monkeypatch.setattr(
        codex_qa,
        "ask",
        lambda prompt, **_kwargs: prompts.append(json.loads(prompt))
        or json.dumps(next(responses_iter)),
    )

    result = codex_qa.answer(
        {
            "question": "Review the ownership choice.",
            "attachment_name": "notes.md",
            "attachment_text": "Owner: Alice or Bob. The call did not choose between them.",
        }
    )

    assert result == {
        "qa_uuid": "",
        "provider": "codex",
        "status": "answered",
        "answer": clarification,
        "citations": [],
        "tools_used": [],
    }
    assert len(prompts) == 2
    assert "supplied attachment is readable" in prompts[1]["evidence"][0]["broker_error"]


def test_mixed_presence_and_factual_question_still_retrieves_evidence(monkeypatch, tmp_path):
    path = fixture_knowledge(monkeypatch, tmp_path)
    responses(monkeypatch, {"request": "search", "query": "stable fixture"},
              {"status": "answered", "answer": "The publication workflow is documented.", "citations": [path]})
    result = codex_qa.answer({"question": "Are you still with us? What is the publication workflow?"})
    assert result["status"] == "answered"
    assert result["tools_used"] == ["search"]
    assert result["citations"] == [path]


def test_semantic_presence_cannot_replace_a_completed_evidence_lookup(monkeypatch, tmp_path):
    fixture_knowledge(monkeypatch, tmp_path)
    responses(monkeypatch, {"request": "search", "query": "stable fixture"},
              {"status": "conversational", "answer": "Aye, I'm here."})
    assert codex_qa.answer({"question": "What is the workflow?"})["status"] != "answered"


def test_latest_prs_retrieve_live_metadata_and_cite_it(monkeypatch):
    source = "https://github.com/leviathan-news/squid-bot/pull/1133"
    calls = []
    monkeypatch.setattr(codex_qa, "retrieve_github", lambda request: calls.append(request) or {
        "source": "https://github.com/leviathan-news/squid-bot/pulls",
        "results": [{"source": source, "number": 1133, "title": "Model-authored replies"}],
        "observed_at": "2026-09-17T11:20:00Z",
    })
    responses(monkeypatch, {"request": "github_pulls", "repository": "leviathan-news/squid-bot"},
              {"status": "answered", "basis": "current", "answer": "PR #1133 restores model-authored replies.",
               "citations": [source]})
    result = codex_qa.answer({"question": "Admiral, can you tell us about the latest PRs?"})
    assert result["status"] == "answered"
    assert result["citations"] == [source] and result["tools_used"] == ["github_pulls"]
    assert len(calls) == 1


def test_current_claim_from_old_document_is_returned_to_model_for_correction(monkeypatch, tmp_path):
    path = fixture_knowledge(monkeypatch, tmp_path)
    source = "https://github.com/leviathan-news/squid-bot/pull/1133"
    monkeypatch.setattr(codex_qa, "retrieve_github", lambda _: {"source": source, "results": []})
    replies = iter([
        {"request": "search", "query": "stable fixture"},
        {"status": "answered", "basis": "current", "answer": "Latest is #967.", "citations": [path]},
        {"request": "github_pulls", "repository": "leviathan-news/squid-bot"},
        {"status": "answered", "basis": "current", "answer": "Latest is #1133.", "citations": [source]},
    ])
    prompts = []
    monkeypatch.setattr(codex_qa, "ask", lambda prompt, **kw: prompts.append(json.loads(prompt)) or json.dumps(next(replies)))
    result = codex_qa.answer({"question": "What are the latest PRs?"})
    assert result["answer"] == "Latest is #1133."
    assert "broker_error" in prompts[2]["evidence"][-1]


def test_github_failure_does_not_create_current_evidence(monkeypatch):
    monkeypatch.setattr(codex_qa, "retrieve_github", lambda _: {"error": "github_unavailable", "http_status": 403})
    responses(monkeypatch, {"request": "github_pulls", "repository": "leviathan-news/squid-bot"},
              {"status": "declined", "declined_reason": "I couldn't check GitHub's current PR list."})
    result = codex_qa.answer({"question": "Latest PRs?"})
    assert result["status"] == "declined" and result["citations"] == []


def test_attachment_can_check_current_github_and_must_cite_live_result(monkeypatch):
    source = "https://github.com/leviathan-news/squid-bot/pull/1133"
    requests = []
    monkeypatch.setattr(
        codex_qa,
        "retrieve_github",
        lambda request: requests.append(request)
        or {
            "source": "https://github.com/leviathan-news/squid-bot/pulls",
            "results": [
                {
                    "source": source,
                    "number": 1133,
                    "state": "closed",
                    "merged_at": "2026-09-17T11:20:00Z",
                }
            ],
            "observed_at": "2026-09-22T16:00:00Z",
        },
    )
    responses(
        monkeypatch,
        {
            "request": "github_pull",
            "repository": "leviathan-news/squid-bot",
            "number": 1133,
        },
        {
            "status": "answered",
            "basis": "current",
            "answer": "The attachment names PR #1133; GitHub currently records it as merged.",
            "citations": [source],
        },
    )

    result = codex_qa.answer(
        {
            "question": "Review the claim and verify its current GitHub state.",
            "attachment_name": "call.md",
            "attachment_text": "The team believes squid-bot PR #1133 merged.",
        }
    )

    assert result["status"] == "answered"
    assert result["citations"] == [source]
    assert result["tools_used"] == ["github_pull"]
    assert requests == [
        {
            "request": "github_pull",
            "repository": "leviathan-news/squid-bot",
            "number": 1133,
        }
    ]


def test_malformed_model_request_is_corrected_within_existing_budget(monkeypatch):
    source = "https://github.com/leviathan-news/squid-bot/pull/1133"
    pending = iter(['{"request":"github_pull","number":1133"}',
                    json.dumps({"request": "github_pull", "repository": "leviathan-news/squid-bot", "number": 1133}),
                    json.dumps({"status": "answered", "basis": "current", "answer": "PR #1133 merged.", "citations": [source]})])
    prompts = []
    monkeypatch.setattr(codex_qa, "ask", lambda prompt, **kw: prompts.append(json.loads(prompt)) or next(pending))
    monkeypatch.setattr(codex_qa, "retrieve_github", lambda _: {"source": source, "results": []})
    result = codex_qa.answer({"question": "Did PR #1133 merge?"})
    assert result["status"] == "answered" and result["tools_used"] == ["github_pull"]
    assert "broker_error" in prompts[1]["evidence"][0]
    assert "invalid_response" in prompts[1]["evidence"][0]
    assert "parse_error" in prompts[1]["evidence"][0]

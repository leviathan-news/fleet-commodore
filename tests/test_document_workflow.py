"""The attachment workflow must produce work and preserve truthful receipts."""
import hashlib
import json
import uuid

import codex_qa
import commodore
import tracker_proposals
from qa_sources import format_qa_answer


def proposal():
    return {"repository": "leviathan-news/squid-bot", "summary": "Call follow-up",
            "items": [{"title": "Check article preview", "description": "Verify the preview on mobile.",
                       "bead_id": "", "github_number": 0, "operation": "review", "priority": 2,
                       "owner": "", "due": "", "evidence": "call.md, action 1"}]}


def job():
    text = "Action 1: check article preview on mobile. No owner or date agreed."
    return {"qa_uuid": str(uuid.uuid4()), "question": "Right here.",
            "attachment_name": "call.md", "attachment_text": text,
            "request_context": [{"message_id": 10, "sender_id": 20,
                                 "text": "Update our Beads and GitHub issues from the call."}],
            "tracker_provenance": {"chat_id": -100123, "topic_id": None, "requester_id": 20,
                                   "request_msg_id": 12, "request_text": "Right here.",
                                   "attachment_name": "call.md",
                                   "attachment_sha256": hashlib.sha256(text.encode()).hexdigest()}}


def test_attachment_followup_saves_real_proposal_before_reporting_it(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACKER_PROPOSALS_STATE_DIR", str(tmp_path))
    request = job()
    prompts = []
    def ask(prompt, **_kwargs):
        payload = json.loads(prompt)
        prompts.append(payload)
        assert payload["request_context"][0]["message_id"] == 10
        if not payload["evidence"]:
            return json.dumps({"message": {"request": "tracker_propose", "proposal": proposal()}})
        receipt = payload["evidence"][-1]["result"]
        assert receipt["applied"] is False
        assert tracker_proposals.get_for_job(request["qa_uuid"], provenance=request["tracker_provenance"])
        return json.dumps({"message": {"status": "answered", "basis": "current",
                           "answer": "I prepared one proposed task change; canonical application is pending.",
                           "citations": [receipt["source"]]}})
    monkeypatch.setattr(codex_qa, "ask", ask)
    result = codex_qa.answer(request)
    assert result["status"] == "answered"
    assert result["tools_used"] == ["tracker_propose"]
    assert len(prompts) == 1
    assert "Beads and GitHub have not been updated" in result["answer"]
    assert result["tracker_outcome"]["applied"] is False


def test_retry_uses_committed_proposal_even_if_model_changes_extraction(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACKER_PROPOSALS_STATE_DIR", str(tmp_path))
    request = job()
    receipt = tracker_proposals.submit(qa_uuid=request["qa_uuid"], proposal=proposal(),
                                       provenance=request["tracker_provenance"])
    calls = 0
    def ask(prompt, **_kwargs):
        raise AssertionError("A committed proposal must not be regenerated after a worker crash")
    monkeypatch.setattr(codex_qa, "ask", ask)
    result = codex_qa.answer(request)
    assert result["status"] == "answered"
    stored = tracker_proposals.get(receipt["proposal_id"], provenance=request["tracker_provenance"])
    assert stored["items"][0]["title"] == "Check article preview"


def test_attachment_does_not_make_reference_evidence_current(monkeypatch):
    prompts = []
    def ask(prompt, **_kwargs):
        payload = json.loads(prompt)
        prompts.append(payload)
        if len(prompts) == 1:
            return json.dumps({"status": "answered", "basis": "current", "answer": "This is deployed.", "citations": []})
        return json.dumps({"status": "declined", "declined_reason": "The call notes do not establish deployment."})
    monkeypatch.setattr(codex_qa, "ask", ask)
    result = codex_qa.answer({"question": "Is this deployed?", "attachment_name": "call.md", "attachment_text": "Plan to deploy"})
    assert result["status"] == "declined"
    assert "answer" not in result


def test_broker_rejection_is_distinct_from_transport_outage(monkeypatch):
    monkeypatch.setattr(codex_qa, "ask", lambda *_a, **_kw: json.dumps({"message": []}))
    result = codex_qa.answer({"question": "Review this"})
    assert result["provider_failure"] == "broker_contract_error"
    detail = json.loads(commodore._qa_failure_detail("worker_failed", result=result))
    assert detail["provider_failure"] == "broker_contract_error"
    assert "service is down" not in commodore._qa_outage_reply(True, detail["provider_failure"])


def test_model_cannot_select_an_unlisted_telegram_document(monkeypatch):
    calls = 0
    def ask(prompt, **_kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps({"request": "telegram_document", "message_id": 999})
        assert json.loads(prompt)["evidence"][-1]["result"]["error"] == "document_reference_unavailable"
        return json.dumps({"status": "declined", "declined_reason": "That document was not observed here."})
    monkeypatch.setattr(codex_qa, "ask", ask)
    monkeypatch.setattr(commodore, "download_known_qa_document", lambda *_a: (_ for _ in ()).throw(AssertionError("must not download")))
    result = codex_qa.answer({"question": "Read the prior document", "known_documents": [{"message_id": 123}]})
    assert result["status"] == "declined"

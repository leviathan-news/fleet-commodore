"""QA outage details must remain operationally useful and user-honest."""
import commodore


def test_qa_failure_detail_preserves_launcher_structure():
    detail = commodore._qa_failure_detail(
        "worker_failed",
        result={
            "error": "child_exit",
            "detail": "pull access denied",
            "stderr_log": "/private/logs/qa.stderr",
            "returncode": 1,
        },
    )

    assert "child_exit" in detail
    assert "pull access denied" in detail
    assert "qa.stderr" in detail


def test_qa_outage_reply_never_dresses_breakage_in_persona():
    assert commodore._qa_outage_reply(True) == (
        "My review service is down; the operator has been alerted."
    )
    assert commodore._qa_outage_reply(False) == (
        "My review service is down; the operator could not be alerted."
    )

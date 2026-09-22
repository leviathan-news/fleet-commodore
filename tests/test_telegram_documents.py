"""Telegram text-document intake and durable Q&A handoff regressions."""

from io import BytesIO
import sqlite3
import zipfile

import pytest

import commodore


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]


def _document_message(body_size=40_442, **document_overrides):
    document = {
        "file_id": "telegram-file-id",
        "file_name": "2026-07-13-convex-maze-review.md",
        "mime_type": "text/markdown",
        "file_size": body_size,
    }
    document.update(document_overrides)
    return {
        "message_id": 846339,
        "chat": {"id": int(commodore.LEV_DEV_GROUP_ID), "type": "supergroup"},
        "from": {"id": 812345, "username": "SecondSetMaze"},
        "caption": "@commodore please review this draft",
        "document": document,
    }


def test_caption_is_message_text_and_drives_mentions():
    msg = _document_message()
    text = commodore._message_text(msg)
    assert text == msg["caption"]
    assert commodore._is_mention_of_commodore(msg, text.lower())


def test_review_attachment_routes_to_qa_without_question_mark():
    assert commodore._qa_question_for_text(
        "please review this draft", has_attachment=True
    ) == "please review this draft"
    assert commodore._qa_question_for_text(
        "please review this draft", has_attachment=False
    ) is None
    assert commodore._qa_question_for_text("", has_attachment=True) == (
        "Please review the attached document."
    )


def test_direct_reply_inherits_parent_document_but_keeps_reply_as_request(
    monkeypatch,
):
    """Maze's first flow: document-only post, then a direct @mention reply."""
    body = b"# Parent document\nreview body"
    parent = _document_message(body_size=len(body))
    reply = {
        "message_id": 141,
        "chat": parent["chat"],
        "from": parent["from"],
        "text": "@commodore please review the attached draft",
        "reply_to_message": {
            "message_id": 140,
            "chat": parent["chat"],
            "from": parent["from"],
            "document": parent["document"],
        },
    }
    monkeypatch.setattr(
        commodore,
        "tg_request",
        lambda *_args, **_kwargs: {
            "ok": True,
            "result": {"file_path": "documents/review.md", "file_size": len(body)},
        },
    )
    monkeypatch.setattr(
        commodore.urllib.request,
        "urlopen",
        lambda _request, timeout: _FakeHTTPResponse(body),
    )

    assert commodore._message_text(reply) == reply["text"]
    assert commodore._message_document(reply) == parent["document"]
    result = commodore.download_telegram_text_document(reply)
    assert result["text"] == body.decode("utf-8")
    assert commodore._qa_question_for_text(
        "please review the attached draft", has_attachment=True
    ) == "please review the attached draft"


def test_downloads_40442_byte_markdown_without_truncation(monkeypatch):
    body = b"# Maze review\n" + b"x" * (40_442 - len(b"# Maze review\n"))
    msg = _document_message(body_size=len(body))
    telegram_calls = []

    def fake_tg_request(method, data=None):
        telegram_calls.append((method, data))
        return {
            "ok": True,
            "result": {"file_path": "documents/review.md", "file_size": len(body)},
        }

    monkeypatch.setattr(commodore, "tg_request", fake_tg_request)
    monkeypatch.setattr(
        commodore.urllib.request,
        "urlopen",
        lambda _request, timeout: _FakeHTTPResponse(body),
    )

    result = commodore.download_telegram_text_document(msg)

    assert telegram_calls == [("getFile", {"file_id": "telegram-file-id"})]
    assert result["name"] == "2026-07-13-convex-maze-review.md"
    assert result["size"] == 40_442
    assert len(result["text"].encode("utf-8")) == 40_442
    assert result["text"].endswith("x")


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"file_name": "review.pdf", "mime_type": "application/pdf"}, "not an accepted"),
        (
            {"file_size": commodore.TELEGRAM_TEXT_DOCUMENT_MAX_BYTES + 1},
            "review limit",
        ),
    ],
)
def test_rejects_unsupported_or_oversized_metadata_without_network(
    monkeypatch, overrides, expected
):
    monkeypatch.setattr(
        commodore,
        "tg_request",
        lambda *_args, **_kwargs: pytest.fail("rejected metadata must not hit Telegram"),
    )
    with pytest.raises(commodore.TelegramDocumentIntakeError, match=expected):
        commodore.download_telegram_text_document(_document_message(**overrides))


def test_misleading_mime_is_advisory_when_markdown_decodes(monkeypatch):
    body = b"# Readable despite Telegram metadata"
    msg = _document_message(
        body_size=len(body), file_name="review.md", mime_type="application/pdf"
    )
    monkeypatch.setattr(
        commodore,
        "tg_request",
        lambda *_args, **_kwargs: {
            "ok": True,
            "result": {"file_path": "documents/review.md", "file_size": len(body)},
        },
    )
    monkeypatch.setattr(
        commodore.urllib.request,
        "urlopen",
        lambda _request, timeout: _FakeHTTPResponse(body),
    )

    result = commodore.download_telegram_text_document(msg)

    assert result["text"] == body.decode()
    assert result["name"] == "review.md"


def test_zip_download_combines_text_and_reports_unread_members_without_disk(
    monkeypatch,
):
    archive_bytes = BytesIO()
    with zipfile.ZipFile(
        archive_bytes, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        archive.writestr("call/transcript.md", "# Call\nAgreed to repair intake.")
        archive.writestr("call/actions.md", "# Actions\n- Verify current issues.")
        archive.writestr("call/recording.mp3", b"binary audio")
        archive.writestr("__MACOSX/._transcript.md", b"\x00\x05AppleDouble")
    body = archive_bytes.getvalue()
    msg = _document_message(
        body_size=len(body), file_name="call-bundle.zip", mime_type="application/zip"
    )
    monkeypatch.setattr(
        commodore,
        "tg_request",
        lambda *_args, **_kwargs: {
            "ok": True,
            "result": {"file_path": "documents/call-bundle.zip", "file_size": len(body)},
        },
    )
    monkeypatch.setattr(
        commodore.urllib.request,
        "urlopen",
        lambda _request, timeout: _FakeHTTPResponse(body),
    )
    monkeypatch.setattr(
        zipfile.ZipFile,
        "extract",
        lambda *_args, **_kwargs: pytest.fail("ZIP members must not be extracted"),
    )
    monkeypatch.setattr(
        zipfile.ZipFile,
        "extractall",
        lambda *_args, **_kwargs: pytest.fail("ZIP members must not be extracted"),
    )

    result = commodore.download_telegram_text_document(msg)

    assert result["members"] == ["call/transcript.md", "call/actions.md"]
    assert "Agreed to repair intake" in result["text"]
    assert "Verify current issues" in result["text"]
    assert '"read_members": ["call/transcript.md", "call/actions.md"]' in result["text"]
    assert result["skipped"] == [
        {
            "name": "call/recording.mp3",
            "reason": "unsupported extension `.mp3`",
        },
        {"name": "__MACOSX/._transcript.md", "reason": "macOS metadata"},
    ]


def test_rejects_invalid_utf8(monkeypatch):
    monkeypatch.setattr(
        commodore,
        "tg_request",
        lambda *_args, **_kwargs: {
            "ok": True,
            "result": {"file_path": "documents/review.md", "file_size": 2},
        },
    )
    monkeypatch.setattr(
        commodore.urllib.request,
        "urlopen",
        lambda _request, timeout: _FakeHTTPResponse(b"\xff\xfe"),
    )
    with pytest.raises(commodore.TelegramDocumentIntakeError, match="UTF-8"):
        commodore.download_telegram_text_document(_document_message(body_size=2))


def test_network_failure_is_safe_and_does_not_echo_authenticated_url(monkeypatch):
    monkeypatch.setattr(
        commodore,
        "tg_request",
        lambda *_args, **_kwargs: {
            "ok": True,
            "result": {"file_path": "documents/review.md", "file_size": 10},
        },
    )
    monkeypatch.setattr(
        commodore.urllib.request,
        "urlopen",
        lambda _request, timeout: (_ for _ in ()).throw(
            RuntimeError(f"failed URL contained {commodore.BOT_TOKEN}")
        ),
    )
    with pytest.raises(commodore.TelegramDocumentIntakeError) as exc_info:
        commodore.download_telegram_text_document(_document_message(body_size=10))
    assert commodore.BOT_TOKEN not in str(exc_info.value)
    assert "file service" in str(exc_info.value)


def test_qa_job_persists_full_attachment_separately(tmp_path, monkeypatch):
    db_path = tmp_path / "commodore.db"
    monkeypatch.setattr(commodore, "DB_FILE", db_path)
    commodore._ensure_tables()
    while not commodore._qa_queue.empty():
        commodore._qa_queue.get_nowait()

    msg = _document_message()
    attachment_text = "x" * 40_442
    attachment = {
        "name": msg["document"]["file_name"],
        "text": attachment_text,
        "size": 40_442,
    }
    commodore._qa_cooldown_by_user.pop(msg["from"]["id"], None)

    job_uuid, ack = commodore._claim_qa_job(
        msg, "please review this draft", attachment=attachment
    )
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT question, attachment_name, attachment_text FROM qa_job "
        "WHERE job_uuid=?",
        (job_uuid,),
    ).fetchone()
    conn.close()

    assert row[0] == "please review this draft"
    assert row[1] == "2026-07-13-convex-maze-review.md"
    assert row[2] == attachment_text
    assert len(row[2]) == 40_442
    assert "can read its contents" in ack

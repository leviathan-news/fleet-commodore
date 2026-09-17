from html import unescape
import re

import pytest

import commodore
from qa_sources import citation_markdown, format_qa_answer


def test_repo_document_is_clickable_through_real_telegram_renderer():
    source = "squid-bot/docs/plans/2026-07-18-atlas-automated-refresh-control-loop.md"
    text = commodore._md_to_telegram_html(format_qa_answer("A reference document.", [source]))
    assert '<a href="https://github.com/leviathan-news/squid-bot/blob/main/docs/plans/2026-07-18-atlas-automated-refresh-control-loop.md">' in text
    assert "&lt;a" not in text


def test_pull_links_have_short_labels_and_query_escapes_once():
    sources = ["https://github.com/leviathan-news/squid-bot/pull/1133",
               "https://github.com/leviathan-news/squid-bot/pulls?q=is%3Apr+sort%3Acreated-desc"]
    text = commodore._md_to_telegram_html(format_qa_answer("Recent changes.", sources))
    assert '>squid-bot #1133</a>' in text
    assert '>squid-bot PRs</a>' in text


@pytest.mark.parametrize("source", ["javascript:alert(1)", "https://evil.invalid/x", "squid-bot/../secret.md",
                                     "squid-bot/.personal/secret.md", "https://[invalid", "database:1234"])
def test_untrusted_or_private_sources_cannot_create_links(source):
    assert "<a " not in commodore._md_to_telegram_html(citation_markdown(source))


def test_long_emoji_body_preserves_complete_source_links_and_visible_limit(monkeypatch):
    raw = format_qa_answer("🌊" * 3800, [
        "squid-bot/docs/" + "long" * 40 + ".md",
        "https://github.com/leviathan-news/squid-bot/pull/1133",
    ])
    calls = []
    monkeypatch.setattr(commodore, "tg_request", lambda method, data: calls.append(data) or {"ok": True, "result": {"message_id": 1}})
    monkeypatch.setattr(commodore, "_HELM_CONTROLLER", None)
    commodore.send_message(-100123, raw)
    text = calls[0]["text"]
    assert text.count("<a ") == text.count("</a>") == 2
    visible = unescape(re.sub(r"<[^>]*>", "", text))
    assert len(visible.encode("utf-16-le")) // 2 < 4096

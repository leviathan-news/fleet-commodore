from pathlib import Path

import pytest

from qa_knowledge import KnowledgeReader


def make_root(tmp_path: Path) -> Path:
    (tmp_path / "squid-bot/docs").mkdir(parents=True)
    (tmp_path / "squid-bot/dev-journal").mkdir(parents=True)
    (tmp_path / "squid-bot/docs/guide.md").write_text("alpha literal [x] beta", encoding="utf-8")
    (tmp_path / "squid-bot/docs/hidden.txt").write_text("hidden", encoding="utf-8")
    (tmp_path / "squid-bot/README.md").write_text("readme", encoding="utf-8")
    return tmp_path


def test_literal_query_and_bounded_read(tmp_path):
    root = make_root(tmp_path)
    reader = KnowledgeReader(root)
    found = reader.search("literal [x]")
    assert [item["path"] for item in found["results"]] == ["squid-bot/docs/guide.md"]
    assert reader.read("squid-bot/docs/guide.md")["content"].startswith("alpha literal")


def test_traversal_and_unlisted_file_rejected(tmp_path):
    root = make_root(tmp_path)
    (root / "squid-bot/secrets.txt").write_text("secret", encoding="utf-8")
    reader = KnowledgeReader(root)
    with pytest.raises(ValueError):
        reader.read("squid-bot/../squid-bot/README.md")
    with pytest.raises(ValueError):
        reader.read("squid-bot/secrets.txt")


def test_hidden_files_are_not_searchable_or_readable(tmp_path):
    root = make_root(tmp_path)
    (root / "squid-bot/docs/.secret.md").write_text("hidden secret", encoding="utf-8")
    reader = KnowledgeReader(root)
    assert reader.search("secret")["results"] == []
    with pytest.raises(ValueError):
        reader.read("squid-bot/docs/.secret.md")


def test_symlink_escape_is_not_retrieved(tmp_path):
    root = make_root(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("escape", encoding="utf-8")
    (root / "squid-bot/docs/escape.txt").symlink_to(outside)
    assert all("escape.txt" not in item["path"] for item in KnowledgeReader(root).search("escape")["results"])
    with pytest.raises(ValueError):
        KnowledgeReader(root).read("squid-bot/docs/escape.txt")


def test_size_bound_and_query_bound(tmp_path):
    root = make_root(tmp_path)
    (root / "squid-bot/docs/large.txt").write_text("z" * 40_000, encoding="utf-8")
    result = KnowledgeReader(root).read("squid-bot/docs/large.txt")
    assert len(result["content"]) == 32_000
    assert result["truncated"] is True
    with pytest.raises(ValueError):
        KnowledgeReader(root).search("x" * 201)


def test_fifo_is_refused_without_blocking(tmp_path):
    root = make_root(tmp_path)
    fifo = root / "squid-bot/docs/pipe.txt"
    import os

    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        KnowledgeReader(root).read("squid-bot/docs/pipe.txt")

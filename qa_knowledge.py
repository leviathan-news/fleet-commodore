"""Small, bounded, read-only retrieval layer for Fleet Commodore QA.

This module deliberately does not execute a model or interpret documents.  It
only exposes the files that ``bin/launch-qa-container`` mounts as knowledge.
"""
from __future__ import annotations

import datetime as _datetime
import os
import re
import stat
from pathlib import Path


KNOWLEDGE_MOUNTS = (
    "squid-bot/docs",
    "squid-bot/dev-journal",
    "squid-bot/CLAUDE.md",
    "squid-bot/README.md",
    "auction-ui/CLAUDE.md",
    "auction-ui/README.md",
    "be-benthic/CLAUDE.md",
    "be-benthic/README.md",
    "agent-chat/CLAUDE.md",
    "agent-chat/README.md",
    "fleet-commodore/CLAUDE.md",
    "fleet-commodore/README.md",
)

_MAX_QUERY = 200
_MAX_FILE_CHARS = 32_000
_MAX_SCAN_FILES = 4_000
_MAX_SEARCH_CHARS = 4 * 1024 * 1024
_MAX_RESULTS = 8
_EXCERPT_CHARS = 1_200
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def _mtime_utc(value: float) -> str:
    return _datetime.datetime.fromtimestamp(value, _datetime.timezone.utc).isoformat()


class KnowledgeReader:
    """Read only the explicitly allowlisted knowledge beneath *root*."""

    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise ValueError("knowledge root must not be a symlink")

    def _relative(self, path: str) -> Path:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ValueError("invalid knowledge path")
        candidate = Path(path)
        if candidate.is_absolute() or any(
            part in ("", ".", "..") or part.startswith(".") for part in candidate.parts
        ):
            raise ValueError("path traversal is not permitted")
        rel = Path(*candidate.parts)
        if any(part.startswith(".") for part in rel.parts) or rel.suffix.lower() not in {".md", ".txt"}:
            raise ValueError("only visible Markdown/text knowledge is available")
        if not any(rel == Path(m) or Path(m) in rel.parents for m in KNOWLEDGE_MOUNTS):
            raise ValueError("path is not in the knowledge allowlist")
        full = self.root / rel
        self._assert_components(full)
        return rel

    def _assert_components(self, full: Path) -> None:
        # lstat each component: resolve() would silently follow an escape.
        if self.root.is_symlink():
            raise ValueError("knowledge root must not be a symlink")
        current = self.root
        try:
            parts = full.relative_to(self.root).parts
        except ValueError as exc:
            raise ValueError("path is outside knowledge root") from exc
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("symlink components are not permitted")

    def _allowed_files(self):
        for mount in KNOWLEDGE_MOUNTS:
            rel = Path(mount)
            full = self.root / rel
            self._assert_components(full)
            try:
                mode = os.lstat(full).st_mode
            except OSError:
                continue
            if stat.S_ISREG(mode):
                if full.suffix.lower() in (".md", ".txt"):
                    yield rel
                continue
            if not stat.S_ISDIR(mode):
                continue
            # os.walk is intentionally avoided: scandir lets us reject/skip
            # symlink entries without ever traversing them.
            yield from self._walk_dir(rel, full)

    def _walk_dir(self, rel: Path, full: Path):
        try:
            entries = sorted(os.scandir(full), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            child_rel, child = rel / entry.name, full / entry.name
            if entry.is_symlink():
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.startswith("."):
                        continue
                    yield from self._walk_dir(child_rel, child)
                elif (
                    not entry.name.startswith(".")
                    and entry.is_file(follow_symlinks=False)
                    and child.suffix.lower() in (".md", ".txt")
                ):
                    yield child_rel
            except OSError:
                continue

    def _read_file(self, rel: Path):
        full = self.root / rel
        self._assert_components(full)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        nonblock = getattr(os, "O_NONBLOCK", 0)
        # Open each component relative to an already-open directory descriptor.
        # This both prevents symlink traversal and closes the lstat/open race.
        fd = os.open(self.root, os.O_RDONLY | directory | nofollow)
        try:
            parts = rel.parts
            for part in parts[:-1]:
                child_fd = os.open(part, os.O_RDONLY | directory | nofollow, dir_fd=fd)
                os.close(fd)
                fd = child_fd
            file_fd = os.open(parts[-1], os.O_RDONLY | nofollow | nonblock, dir_fd=fd)
            os.close(fd)
            fd = file_fd
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("knowledge path is not a regular file")
            raw = os.read(fd, _MAX_FILE_CHARS + 1)
            text = raw.decode("utf-8", errors="replace")
            truncated = len(raw) > _MAX_FILE_CHARS or len(text) > _MAX_FILE_CHARS
            return text[:_MAX_FILE_CHARS], _mtime_utc(before.st_mtime), truncated
        finally:
            os.close(fd)

    def read(self, path: str) -> dict:
        rel = self._relative(path)
        text, mtime, truncated = self._read_file(rel)
        return {"path": rel.as_posix(), "content": text, "mtimeUTC": mtime, "truncated": truncated}

    def search(self, query: str) -> dict:
        if not isinstance(query, str) or len(query) > _MAX_QUERY:
            raise ValueError("query must be at most 200 characters")
        tokens = [token.casefold() for token in _TOKEN_RE.findall(query)]
        if not tokens:
            return {"query": query, "results": [], "truncated": False}
        results, scanned, total = [], 0, 0
        limited = False
        seen = set()
        for rel in self._allowed_files():
            if rel in seen:
                continue
            seen.add(rel)
            if scanned >= _MAX_SCAN_FILES or total >= _MAX_SEARCH_CHARS:
                limited = True
                break
            scanned += 1
            try:
                text, mtime, file_truncated = self._read_file(rel)
            except (OSError, ValueError):
                continue
            total += len(text)
            folded = text.casefold()
            if all(token in folded for token in tokens):
                pos = min(folded.find(token) for token in tokens)
                start = max(0, pos - (_EXCERPT_CHARS // 2))
                excerpt = text[start:start + _EXCERPT_CHARS]
                results.append({"path": rel.as_posix(), "excerpt": excerpt, "mtimeUTC": mtime, "truncated": file_truncated})
                if len(results) >= _MAX_RESULTS:
                    limited = True
                    break
        return {"query": query, "results": results, "truncated": limited}

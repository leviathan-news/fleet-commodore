"""Compose bounded Markdown source links for Fleet's existing HTML renderer."""
from pathlib import PurePosixPath
import re
from urllib.parse import quote, urlsplit

from qa_github import REPOSITORIES


def citation_markdown(source: str) -> str:
    if not isinstance(source, str) or len(source) > 700:
        return "Unrecognized source"
    try:
        parsed = urlsplit(source)
    except ValueError:
        return "Unrecognized source"
    if parsed.scheme == "https" and parsed.netloc == "github.com":
        parts = parsed.path.strip("/").split("/")
        repo = "/".join(parts[:2])
        if repo in REPOSITORIES and len(parts) >= 3:
            if parts[2] == "pull" and len(parts) == 4 and parts[3].isdigit() and not parsed.query:
                return f"[{parts[1]} #{parts[3]}](https://github.com/{repo}/pull/{parts[3]})"
            if parts[2] == "pulls" and len(parts) == 3:
                # Encode delimiters so model-supplied URL punctuation cannot
                # escape the existing Markdown-to-HTML link parser.
                url = f"https://github.com/{repo}/pulls"
                if parsed.query:
                    url += "?" + quote(parsed.query, safe="=&%+:-")
                return f"[{parts[1]} PRs]({url})"
    path = source.removeprefix("/app/knowledge/")
    parts = path.split("/")
    if (len(parts) >= 2 and f"leviathan-news/{parts[0]}" in REPOSITORIES
            and all(part and part not in {".", ".."} and not part.startswith(".") for part in parts)
            and PurePosixPath(path).suffix in {".md", ".txt"}):
        label = PurePosixPath(path).name
        label = re.sub(r"[\[\]<>`\r\n]", "", label)[:75]
        url = "https://github.com/leviathan-news/" + quote(parts[0], safe="") + "/blob/main/" + quote("/".join(parts[1:]), safe="/")
        return f"[{label}]({url})"
    if source.startswith("database:"):
        return "Read-only database observation"
    if re.fullmatch(r"tracker-proposal:tp_[a-f0-9]{24}", source):
        return "Tracker proposal " + source.split(":", 1)[1]
    if re.fullmatch(r"telegram-document:[1-9][0-9]*", source):
        return "Telegram document, message " + source.split(":", 1)[1]
    return "Unrecognized source"


def format_qa_answer(answer: str, citations: list) -> str:
    # Bound visible UTF-16 body length separately. Never slice a source URL.
    links = [citation_markdown(source) for source in citations[:3]]
    block = "\n\nSources: " + " · ".join(links) if links else ""
    # The legacy sender also slices raw input at 3800 characters. Reserve
    # its space for complete URLs in addition to the actual visible budget.
    text = str(answer)[:max(0, 3800 - len(block))]
    text = text.encode("utf-16-le")[:6400].decode("utf-16-le", errors="ignore").rstrip()
    return text + block

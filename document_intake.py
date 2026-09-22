"""Bounded, in-memory decoding for Telegram review documents."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import stat
import zipfile
import zlib


TEXT_EXTENSIONS = frozenset(
    {".md", ".markdown", ".txt", ".rst", ".json", ".csv", ".yaml", ".yml"}
)
MAX_ZIP_MEMBERS = 256


class DocumentIntakeError(ValueError):
    """A safe reason that a submitted document could not be decoded."""


def _decode_utf8(name: str, raw: bytes) -> str:
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise DocumentIntakeError(
            f"`{name}` is not valid UTF-8 text; export it as UTF-8 first."
        ) from None
    if "\x00" in text:
        raise DocumentIntakeError(
            f"`{name}` contains binary NUL bytes rather than plain text."
        )
    return text


def _display_member_name(name: str) -> str:
    printable = "".join(character for character in name if character.isprintable())
    return printable[:240] or "[unnamed member]"


def _normalize_member_name(info: zipfile.ZipInfo) -> tuple[str, str | None]:
    raw_name = info.filename
    display_name = _display_member_name(raw_name)
    if not raw_name or len(raw_name) > 240 or any(
        not character.isprintable() for character in raw_name
    ):
        return display_name, "unsafe member name"

    slash_name = raw_name.replace("\\", "/")
    if slash_name.startswith("/"):
        return display_name, "unsafe absolute path"
    parts = [part for part in slash_name.split("/") if part not in {"", "."}]
    if not parts:
        return display_name, "empty member path"
    if ".." in parts or ":" in parts[0]:
        return display_name, "unsafe member path"
    return "/".join(parts), None


def _non_file_reason(info: zipfile.ZipInfo) -> str | None:
    if info.flag_bits & 0x1:
        return "encrypted member"
    if info.is_dir():
        return None

    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type == stat.S_IFLNK:
        return "symbolic link"
    if file_type not in {0, stat.S_IFREG}:
        return "not a regular file"
    return None


def _is_macos_metadata(name: str) -> bool:
    parts = name.split("/")
    return parts[0] == "__MACOSX" or any(part.startswith("._") for part in parts)


def _decode_zip(raw: bytes, max_text_bytes: int) -> dict:
    try:
        archive = zipfile.ZipFile(BytesIO(raw))
        members = archive.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile):
        raise DocumentIntakeError("the ZIP archive is corrupt or unsupported.") from None

    with archive:
        if len(members) > MAX_ZIP_MEMBERS:
            raise DocumentIntakeError(
                f"the ZIP contains more than {MAX_ZIP_MEMBERS} members."
            )

        accepted_names: list[str] = []
        skipped: list[dict[str, str]] = []
        chunks: list[str] = []
        seen: set[str] = set()
        decompressed_bytes = 0
        rendered_bytes = 0

        for info in members:
            name, path_problem = _normalize_member_name(info)
            if path_problem:
                skipped.append({"name": name, "reason": path_problem})
                continue
            if name in seen:
                skipped.append({"name": name, "reason": "duplicate member name"})
                continue
            seen.add(name)
            if info.is_dir():
                continue
            if _is_macos_metadata(name):
                skipped.append({"name": name, "reason": "macOS metadata"})
                continue

            non_file_reason = _non_file_reason(info)
            if non_file_reason:
                skipped.append({"name": name, "reason": non_file_reason})
                continue

            extension = Path(name).suffix.lower()
            if extension not in TEXT_EXTENSIONS:
                skipped.append(
                    {"name": name, "reason": f"unsupported extension `{extension or '[none]'}`"}
                )
                continue

            if info.file_size < 0:
                skipped.append({"name": name, "reason": "invalid declared size"})
                continue
            if decompressed_bytes + info.file_size > max_text_bytes:
                skipped.append(
                    {
                        "name": name,
                        "reason": f"text exceeds remaining {max_text_bytes:,}-byte review budget",
                    }
                )
                continue

            remaining = max_text_bytes - decompressed_bytes
            try:
                with archive.open(info, "r") as member:
                    member_raw = member.read(remaining + 1)
            except (
                zipfile.BadZipFile,
                zipfile.LargeZipFile,
                zlib.error,
                RuntimeError,
                NotImplementedError,
                EOFError,
                OSError,
            ):
                skipped.append({"name": name, "reason": "corrupt or unreadable member"})
                continue
            if len(member_raw) > remaining:
                skipped.append(
                    {
                        "name": name,
                        "reason": f"text exceeds remaining {max_text_bytes:,}-byte review budget",
                    }
                )
                continue

            decompressed_bytes += len(member_raw)
            try:
                text = _decode_utf8(name, member_raw)
            except DocumentIntakeError as exc:
                skipped.append({"name": name, "reason": str(exc)})
                continue
            chunk = f"=== ZIP MEMBER: {name} ===\n{text}"
            chunk_bytes = len(chunk.encode("utf-8"))
            separator_bytes = 2 if chunks else 0
            if rendered_bytes + separator_bytes + chunk_bytes > max_text_bytes:
                skipped.append(
                    {
                        "name": name,
                        "reason": f"rendered text exceeds {max_text_bytes:,}-byte review budget",
                    }
                )
                continue
            chunks.append(chunk)
            rendered_bytes += separator_bytes + chunk_bytes
            accepted_names.append(name)

        if not accepted_names:
            raise DocumentIntakeError(
                "the ZIP contains no readable supported UTF-8 text documents."
            )

        result = {"text": "\n\n".join(chunks), "members": accepted_names}
        if skipped:
            result["skipped"] = skipped
        return result


def decode_document(name: str, raw: bytes, max_text_bytes: int) -> dict:
    """Decode one supported text document or ZIP bundle without extracting it.

    The caller remains responsible for bounding the compressed input download.
    ``max_text_bytes`` bounds plain text and the combined rendered text returned
    for a ZIP. Unsupported ZIP members are listed in ``skipped`` and are never
    decompressed.
    """
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    if (
        not isinstance(max_text_bytes, int)
        or isinstance(max_text_bytes, bool)
        or max_text_bytes <= 0
    ):
        raise ValueError("max_text_bytes must be a positive integer")

    safe_name = Path(str(name).replace("\\", "/")).name
    extension = Path(safe_name).suffix.lower()
    if extension == ".zip":
        return _decode_zip(raw, max_text_bytes)
    if extension not in TEXT_EXTENSIONS:
        accepted = ", ".join(sorted(TEXT_EXTENSIONS | {".zip"}))
        raise DocumentIntakeError(
            f"`{extension or '[no extension]'}` is not an accepted document type "
            f"(accepted: {accepted})."
        )
    if len(raw) > max_text_bytes:
        raise DocumentIntakeError(
            f"`{safe_name}` exceeds the {max_text_bytes:,}-byte review limit."
        )
    return {"text": _decode_utf8(safe_name or "unnamed document", raw)}

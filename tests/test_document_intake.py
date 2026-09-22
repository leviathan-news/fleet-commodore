"""Unit coverage for bounded text and ZIP document decoding."""

from io import BytesIO
import stat
import struct
import zipfile

import pytest

from document_intake import (
    DocumentIntakeError,
    MAX_ZIP_MEMBERS,
    decode_document,
)


def _zip(entries) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in entries:
            archive.writestr(name, body)
    return output.getvalue()


@pytest.mark.parametrize(
    "name",
    [
        "call.md",
        "call.markdown",
        "call.txt",
        "call.rst",
        "call.json",
        "call.csv",
        "call.yaml",
        "call.yml",
    ],
)
def test_decodes_existing_text_types(name):
    assert decode_document(name, b"\xef\xbb\xbfhello", 100) == {"text": "hello"}


def test_combines_supported_zip_members_and_reports_skipped_binary():
    raw = _zip(
        [
            ("call/transcript.md", "# Call\nDecisions"),
            ("call/actions.json", '{"issues": [12]}'),
            ("call/recording.mp3", b"not decompressed as text"),
        ]
    )

    result = decode_document("call-bundle.ZIP", raw, 4_096)

    assert result["members"] == ["call/transcript.md", "call/actions.json"]
    assert result["text"] == (
        "=== ZIP MEMBER: call/transcript.md ===\n# Call\nDecisions\n\n"
        '=== ZIP MEMBER: call/actions.json ===\n{"issues": [12]}'
    )
    assert result["skipped"] == [
        {"name": "call/recording.mp3", "reason": "unsupported extension `.mp3`"}
    ]


def test_unsupported_binary_is_not_decompressed(monkeypatch):
    raw = _zip([("notes.md", "ok"), ("large.bin", b"x" * 10_000)])
    original_open = zipfile.ZipFile.open

    def guarded_open(self, member, *args, **kwargs):
        name = member.filename if isinstance(member, zipfile.ZipInfo) else member
        if name == "large.bin":
            pytest.fail("unsupported members must not be decompressed")
        return original_open(self, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", guarded_open)
    result = decode_document("bundle.zip", raw, 1_024)
    assert result["members"] == ["notes.md"]
    assert result["skipped"][0]["name"] == "large.bin"


@pytest.mark.parametrize(
    "member_name",
    [
        "../secret.md",
        "/absolute.md",
        "C:/windows.md",
    ],
)
def test_skips_unsafe_member_paths_but_keeps_readable_text(member_name):
    result = decode_document(
        "bundle.zip", _zip([("good.md", "yes"), (member_name, "no")]), 1_024
    )
    assert result["members"] == ["good.md"]
    assert "unsafe" in result["skipped"][0]["reason"]


def test_normalizes_harmless_relative_member_paths():
    result = decode_document(
        "bundle.zip",
        _zip(
            [
                ("./notes.md", "one"),
                ("folder//more.md", "two"),
                ("windows\\path.txt", "three"),
            ]
        ),
        1_024,
    )
    assert result["members"] == ["notes.md", "folder/more.md", "windows/path.txt"]


def test_skips_macos_appledouble_without_decompression(monkeypatch):
    raw = _zip(
        [("notes.md", "useful"), ("__MACOSX/._notes.md", b"\x00\x05AppleDouble")]
    )
    original_open = zipfile.ZipFile.open

    def guarded_open(self, member, *args, **kwargs):
        name = member.filename if isinstance(member, zipfile.ZipInfo) else member
        if name.startswith("__MACOSX/"):
            pytest.fail("macOS metadata must not be decompressed")
        return original_open(self, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", guarded_open)
    result = decode_document("bundle.zip", raw, 1_024)
    assert result["members"] == ["notes.md"]
    assert result["skipped"] == [
        {"name": "__MACOSX/._notes.md", "reason": "macOS metadata"}
    ]


def test_skips_symbolic_link_member():
    output = BytesIO()
    info = zipfile.ZipInfo("link.md")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(info, "target.md")

        archive.writestr("good.md", "read me")

    result = decode_document("bundle.zip", output.getvalue(), 1_024)
    assert result["members"] == ["good.md"]
    assert result["skipped"] == [{"name": "link.md", "reason": "symbolic link"}]


def test_skips_encrypted_member_metadata():
    raw = bytearray(_zip([("secret.md", "secret"), ("good.md", "read me")]))
    local = raw.find(b"PK\x03\x04")
    central = raw.find(b"PK\x01\x02")
    assert local >= 0 and central >= 0
    local_flags = struct.unpack_from("<H", raw, local + 6)[0]
    central_flags = struct.unpack_from("<H", raw, central + 8)[0]
    struct.pack_into("<H", raw, local + 6, local_flags | 0x1)
    struct.pack_into("<H", raw, central + 8, central_flags | 0x1)

    result = decode_document("bundle.zip", bytes(raw), 1_024)
    assert result["members"] == ["good.md"]
    assert result["skipped"] == [{"name": "secret.md", "reason": "encrypted member"}]


def test_rejects_corrupt_zip():
    with pytest.raises(DocumentIntakeError, match="corrupt"):
        decode_document("bundle.zip", b"PK\x03\x04truncated", 1_024)


def test_skips_corrupt_member_and_keeps_readable_text():
    raw = bytearray(_zip([("notes.md", b"known body"), ("good.md", "read me")]))
    local = raw.find(b"PK\x03\x04")
    name_length, extra_length = struct.unpack_from("<HH", raw, local + 26)
    data_offset = local + 30 + name_length + extra_length
    raw[data_offset] ^= 0xFF

    result = decode_document("bundle.zip", bytes(raw), 1_024)
    assert result["members"] == ["good.md"]
    assert result["skipped"] == [
        {"name": "notes.md", "reason": "corrupt or unreadable member"}
    ]


def test_rejects_member_count_above_limit():
    raw = _zip([(f"notes/{index}.md", "") for index in range(MAX_ZIP_MEMBERS + 1)])
    with pytest.raises(DocumentIntakeError, match=str(MAX_ZIP_MEMBERS)):
        decode_document("bundle.zip", raw, 20_000)


def test_skips_zip_member_when_decompressed_text_exceeds_limit():
    raw = _zip([("compresses-well.md", "x" * 50_000), ("small.md", "ok")])
    assert len(raw) < 1_000
    result = decode_document("bundle.zip", raw, 4_096)
    assert result["members"] == ["small.md"]
    assert "review budget" in result["skipped"][0]["reason"]


def test_rejects_zip_when_no_member_fits_rendered_limit():
    raw = _zip([("a.md", "1234")])
    with pytest.raises(DocumentIntakeError, match="no readable supported"):
        decode_document("bundle.zip", raw, 10)


def test_skips_invalid_utf8_in_supported_member():
    result = decode_document(
        "bundle.zip", _zip([("bad.md", b"\xff"), ("good.md", "read me")]), 1_024
    )
    assert result["members"] == ["good.md"]
    assert "not valid UTF-8" in result["skipped"][0]["reason"]


def test_rejects_archive_with_only_unsupported_members():
    with pytest.raises(DocumentIntakeError, match="no readable supported UTF-8"):
        decode_document("bundle.zip", _zip([("recording.mp4", b"video")]), 1_024)


def test_skips_duplicate_member_names():
    with pytest.warns(UserWarning, match="Duplicate name"):
        raw = _zip([("notes.md", "one"), ("notes.md", "two")])
    result = decode_document("bundle.zip", raw, 1_024)
    assert result["members"] == ["notes.md"]
    assert result["skipped"] == [
        {"name": "notes.md", "reason": "duplicate member name"}
    ]


def test_plain_text_obeys_size_and_utf8_limits():
    with pytest.raises(DocumentIntakeError, match="review limit"):
        decode_document("notes.md", b"12345", 4)
    with pytest.raises(DocumentIntakeError, match="UTF-8"):
        decode_document("notes.md", b"\xff", 4)


def test_rejects_unsupported_top_level_document():
    with pytest.raises(DocumentIntakeError, match="not an accepted"):
        decode_document("bundle.tar.gz", b"unused", 1_024)

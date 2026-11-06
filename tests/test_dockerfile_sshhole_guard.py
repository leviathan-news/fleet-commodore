"""CI guard against sshhole (CVE-2026-123158) and unpatched musl in Dockerfiles.

Enforces two invariants across all Dockerfiles in the repository:
1. No Dockerfile may install openssh, openssh-server, sshd, or dropbear unless
   it also installs /etc/ssh/sshd_config.d/00-sshhole.conf with:
     - PasswordAuthentication no
     - KbdInteractiveAuthentication no
     - PermitEmptyPasswords no
     - PermitRootLogin prohibit-password (or no)
     - AuthenticationMethods publickey
2. Every Alpine-based Dockerfile must pin a patch release (e.g. alpine:3.20.10+
   or alpine:3.24.4+) and run `apk upgrade --no-cache musl` so layers receive
   the latest Alpine secdb musl security build (musl >= 1.2.5-r4 on 3.20,
   >= 1.2.5-r12 on 3.21, >= 1.2.6-r3 on 3.24).
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest


REPO = Path(__file__).resolve().parent.parent
DOCKERFILES = sorted(REPO.glob("*Dockerfile*"))

_FROM_ALPINE = re.compile(r"^FROM\s+([^\s]*alpine[^\s]*)", re.IGNORECASE | re.MULTILINE)
_PINNED_ALPINE = re.compile(r"^alpine:(?:3\.20\.(?:1\d|[2-9]\d)|3\.2[1-9]\.\d+)$", re.IGNORECASE)
_APK_UPGRADE_MUSL = re.compile(r"\bapk\s+upgrade\s+--no-cache\s+[^\n#]*\bmusl\b")
_SSH_PACKAGE_INSTALL = re.compile(
    r"\b(?:apk\s+add|apt-get\s+install|apt\s+install)\b[^\n#]*\b(?:openssh-server|openssh|sshd|dropbear)\b",
    re.IGNORECASE,
)
REQUIRED_SSHD_DIRECTIVES = {
    "passwordauthentication": {"no"},
    "kbdinteractiveauthentication": {"no"},
    "permitemptypasswords": {"no"},
    "permitrootlogin": {"prohibit-password", "no"},
    "authenticationmethods": {"publickey"},
}


def find_sshd_violations(dockerfile_text: str, dropin_text: str | None = None) -> list[str]:
    """Return a list of security policy violations for a Dockerfile."""
    violations: list[str] = []
    if _SSH_PACKAGE_INSTALL.search(dockerfile_text):
        if "00-sshhole.conf" not in dockerfile_text:
            violations.append(
                "installs SSH daemon without /etc/ssh/sshd_config.d/00-sshhole.conf"
            )
        elif dropin_text is not None:
            parsed: dict[str, str] = {}
            for raw in dropin_text.splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[0].lower() not in parsed:
                    parsed[parts[0].lower()] = parts[1].strip().lower()
            for key, allowed in REQUIRED_SSHD_DIRECTIVES.items():
                actual = parsed.get(key)
                if actual not in allowed:
                    violations.append(f"00-sshhole.conf missing or weak {key}: {actual!r}")

    for match in _FROM_ALPINE.finditer(dockerfile_text):
        base_image = match.group(1)
        if base_image.lower().startswith("alpine:") and not _PINNED_ALPINE.match(base_image):
            violations.append(
                f"unpinned or outdated Alpine base image {base_image!r} (require >= alpine:3.20.10)"
            )
        if not _APK_UPGRADE_MUSL.search(dockerfile_text):
            violations.append(
                f"Alpine Dockerfile ({base_image}) missing `apk upgrade --no-cache musl`"
            )

    return violations


def test_repo_has_dockerfiles():
    assert len(DOCKERFILES) >= 5


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_dockerfile_complies_with_sshhole_and_musl_policy(dockerfile: Path):
    text = dockerfile.read_text(encoding="utf-8")
    violations = find_sshd_violations(text)
    assert not violations, f"{dockerfile.name} failed sshhole guard: {violations}"


def test_guard_rejects_unhardened_openssh_install():
    bad_dockerfile = (
        "FROM alpine:3.20.10\n"
        "RUN apk upgrade --no-cache musl && apk add --no-cache openssh-server\n"
    )
    violations = find_sshd_violations(bad_dockerfile)
    assert any("00-sshhole.conf" in v for v in violations)


def test_guard_rejects_weak_dropin():
    bad_dockerfile = (
        "FROM alpine:3.20.10\n"
        "RUN apk upgrade --no-cache musl && apk add --no-cache openssh-server\n"
        "COPY 00-sshhole.conf /etc/ssh/sshd_config.d/00-sshhole.conf\n"
    )
    weak_dropin = "PasswordAuthentication yes\n"
    violations = find_sshd_violations(bad_dockerfile, weak_dropin)
    assert any("passwordauthentication" in v for v in violations)


def test_guard_accepts_hardened_dropin():
    good_dockerfile = (
        "FROM alpine:3.20.10\n"
        "RUN apk upgrade --no-cache musl && apk add --no-cache openssh-server\n"
        "COPY 00-sshhole.conf /etc/ssh/sshd_config.d/00-sshhole.conf\n"
    )
    good_dropin = (
        "PasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\n"
        "PermitEmptyPasswords no\n"
        "PermitRootLogin prohibit-password\n"
        "AuthenticationMethods publickey\n"
    )
    assert find_sshd_violations(good_dockerfile, good_dropin) == []

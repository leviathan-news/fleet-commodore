#!/usr/bin/env python3
"""Bounded, neutral Codex subscription health probe."""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codex_provider import generate_via_codex


MARKER = "FLEET_PROVIDER_PROBE_OK"


def main() -> int:
    context: dict[str, object] = {}
    response = generate_via_codex(
        "Reply with the exact health marker and nothing else.",
        "This is a provider health check. Do not use tools or applications.",
        model="gpt-5.6-luna",
        timeout_seconds=20,
        response_instruction=f"Return exactly: {MARKER}",
        failure_context=context,
    )
    if response == MARKER:
        print(json.dumps({"state": "ok", "provider": "codex"}, sort_keys=True))
        return 0
    failure = context.get("failure_class")
    state = {
        "provider_auth_failed": "auth_failed",
        "provider_rate_limited": "quota",
        "provider_timeout": "timeout",
    }.get(failure, "unknown_error")
    print(json.dumps({"state": state, "provider": "codex"}, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

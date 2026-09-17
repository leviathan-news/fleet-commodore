"""Explicit Mini-only first cutover: retain legacy pending updates, never replay.

Run only during an authorized offline transition with the legacy watchdog
paused. The ordinary daemon refuses a ledger without this completed capture.
No provider, reply, webhook deletion, or worker recovery runs here.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess

from chat_dispatch import PollOwner, PollOwnerBusy
from chat_intake import ChatIntake
from fleet_watchdog import Runtime, UnsafeObservation, helm_allows_ordinary


class CutoverError(RuntimeError):
    """Fixed non-sensitive cutover disposition."""


def capture_pending(intake, fetch, minimize, verify_absent, *, max_batches=40):
    if isinstance(max_batches, bool) or not isinstance(max_batches, int) or not 1 <= max_batches <= 40:
        raise ValueError("capture batch limit must be between 1 and 40")
    if intake.legacy_capture_complete():
        raise CutoverError("legacy_capture_already_complete")
    batches = 0
    while batches < max_batches:
        verify_absent()
        offset = intake.offset()
        response = fetch("getUpdates", {
            "offset": offset, "timeout": 0, "limit": 100,
            "allowed_updates": ["message", "my_chat_member"],
        })
        if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("result"), list):
            raise CutoverError("unconfirmed_legacy_batch")
        batch = response["result"]
        if len(batch) > 100:
            raise CutoverError("legacy_batch_too_large")
        # A competing actor/lease observed after fetch prevents admission and
        # readiness. Earlier cursor confirmation only covered committed rows.
        verify_absent()
        if not batch:
            intake.mark_legacy_capture_complete()
            return {"state": "legacy_capture_complete", "batches": batches,
                    "offset": intake.offset(), "held": intake.snapshot()["held_unknown"]}
        intake.ingest_legacy_batch([minimize(update) for update in batch])
        if intake.offset() <= offset:
            raise CutoverError("legacy_batch_did_not_advance")
        batches += 1
    # All admitted bodies and cursor survive this refusal. Resume capture from
    # the committed offset; never erase the ledger to retry the transition.
    raise CutoverError("legacy_capture_limit_reached")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-legacy", action="store_true", required=True)
    args = parser.parse_args(argv)
    del args
    # This is the observed designated runtime, not permission to run on any Mac.
    if socket.gethostname() != "gmacmini.local":
        print('{"state":"held","reason":"designated_mini_required"}')
        return 1
    if os.environ.get("HELM_CONTROLLER_ENABLED") == "1":
        print('{"state":"held","reason":"controller_enabled"}')
        return 1
    release = Path(__file__).resolve().parent
    configured_release = Path(os.environ.get("FLEET_COMMODORE_RELEASE_DIR", "")).resolve()
    config = Path(os.environ.get("FLEET_COMMODORE_CONFIG", "")).expanduser()
    state = Path(os.environ.get("FLEET_COMMODORE_STATE_DIR", "~/.local/state/fleet-commodore")).expanduser()
    helm = Path(os.environ.get("HELM_CONTROLLER_DB_FILE", state / "helm-controller/controller.db")).expanduser()
    try:
        if configured_release != release or not config.is_file():
            raise CutoverError("release_config_unavailable")
        runtime = Runtime(release, config)

        def verify_absent():
            if not helm_allows_ordinary(helm):
                raise CutoverError("controller_ownership")
            if runtime.observe().actors:
                raise CutoverError("fleet_actor_still_active")

        with PollOwner(state / "watchdog.lock"), PollOwner(state / "poll-owner.lock"):
            verify_absent()
            # Configuration must already be sourced by the sanctioned launcher.
            # Import after absence/ownership checks; importing is not polling.
            import commodore

            if commodore.DB_FILE.parent.resolve() != state.resolve():
                raise CutoverError("chat_state_path_mismatch")
            intake = ChatIntake(state / "chat-intake.db")
            result = capture_pending(intake, commodore.tg_request, commodore._admit_chat_update, verify_absent)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (CutoverError, UnsafeObservation, PollOwnerBusy, OSError, sqlite3.Error,
            subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
        reason = str(exc) if isinstance(exc, (CutoverError, UnsafeObservation)) else type(exc).__name__
        print(json.dumps({"state": "held", "reason": reason}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Lease-bound bridge from the Mini event queue to a replaceable Sol worker."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys
import time

from helm_controller import HelmController, ReplyLeaseDenied, read_token_file


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--bridge-ttl", type=int, default=120)
    parser.add_argument("--claim-ttl", type=int, default=300)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    controller = HelmController(args.db)
    token_path = Path(args.token_file).expanduser()
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    next_renewal = 0.0
    while not stopping:
        now = time.monotonic()
        try:
            token = read_token_file(token_path)
            if now >= next_renewal:
                controller.renew("bridge", token, args.bridge_ttl)
                next_renewal = now + max(1.0, args.bridge_ttl / 3)
            event = controller.claim_next(token, claim_ttl=args.claim_ttl)
        except ReplyLeaseDenied as exc:
            print(json.dumps({"type": "bridge_yield", "reason": str(exc)}), flush=True)
            return 4
        except Exception as exc:
            print(
                json.dumps({"type": "bridge_failure", "error": type(exc).__name__}),
                flush=True,
            )
            return 2
        if event:
            print(json.dumps({"type": "helm_event", "event": event}), flush=True)
        time.sleep(args.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

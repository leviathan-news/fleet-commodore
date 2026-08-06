#!/usr/bin/env python3
"""Isolated-test lease renewer. Never use this as a production liveness oracle."""

import argparse
import signal
import time

from helm_controller import HelmController, read_token_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--kind", choices=("watcher", "bridge"), required=True)
    parser.add_argument("--ttl", type=float, default=2.0)
    parser.add_argument("--interval", type=float, default=0.2)
    args = parser.parse_args()
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    controller = HelmController(args.db)
    token = read_token_file(args.token_file)
    while running:
        controller.renew(args.kind, token, args.ttl)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

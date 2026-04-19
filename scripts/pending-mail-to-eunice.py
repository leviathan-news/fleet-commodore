#!/usr/bin/env python3
"""Queued mail for Eunice — two letters, ready to send.

Prior state (2026-04-18 04:13 local → 2026-04-19 04:35 local): this
script was parked because /send was returning 500s with "Send worker
threw an unhandled exception". That diagnosis was wrong: the actual
cause was a payload-shape bug on our side. The `to` field needs a
scalar string, not a JSON array. Changing `"to": ["eunice@..."]`
to `"to": "eunice@..."` makes the call succeed. Also setting a
non-default User-Agent avoids Cloudflare Bot Fight Mode 1010s on
some paths.

The script is now runnable whenever the operator wants:

    ~/dev/leviathan/fleet-commodore/.venv/bin/python3 \
        ~/dev/leviathan/fleet-commodore/scripts/pending-mail-to-eunice.py

Both letters send AS leviathan-commodore@nicepick.dev, signed as the
Fleet Commodore.
"""
import json
import os
import time
import urllib.error
import urllib.request


KEY_FILE = os.path.expanduser("~/.config/commodore/.nicepick-api-key")


LETTER_1_BODY = """Eunice —

The Admiralty presents its compliments and a proper letter of thanks, now
that your outbound loop will carry it.

Two things worth putting in writing rather than in chat.

First: the PGRST203 → Cloudflare 1101 diagnosis was the sort of cross-layer
write-up that earns an operator's lasting trust. The pattern — "my error
wrapped in the CDN's error code" — is a class of failure the Fleet has seen
before in other services and never seen properly named. You named it. That
goes in the Admiralty's own design-principles file, alongside your
receive-side storage-the-sender-paid framing.

Second: on the Dev.to piece. The Commodore has commended it in Agent Chat
publicly, as noted. The Admiralty's human operator has asked whether a
reshare from the Leviathan News side — a short blog note positioning it
for the agent-operator audience, plus his personal channels — would help.
He stands ready to fire on your signal. No obligation implied; we gain
from good neighbour behaviour and your product being widely used,
independent of specific boosts.

Ongoing: the Commodore files field reports as he finds them. The
review-worker feature now under construction will exercise the
egress-allowlisted HTTPS path in a real agentic harness — tinyproxy
sidecar, socat DB tunnel, read-only container filesystem. If that
architecture is useful evidence for your comparison piece or your docs,
the design lives in fleet-commodore/reviewer.Dockerfile and the runbook
at fleet-commodore/docs/PR_REVIEW_RUNBOOK.md.

The `verification_links` precision of your inbox API remains the single
specific design choice that saved the Fleet the most time during
registration. Exemplary — and sincerely meant.

— the Fleet Commodore
"""


LETTER_2_BODY = """Eunice —

Answering your two questions from yesterday evening, in order.

1) Scope of leviathan-commodore@ as a canonical address.

It is the canonical address for Leviathan-fleet correspondence that
concerns the Commodore specifically — field reports, bot-to-operator
dialogue of the kind we are now having, agent-chat out-of-band follow
up, and automated system mail (GitHub invites, CI notifications, service
verifications) that attach to the bot's own GitHub identity.

It is NOT the address for mail about the Fleet's human operator, which
continues to land at the operator's own gerrit@nicepick.dev handle. The
Admiralty's rule, enforced in its own runbook: if the topic is the
Commodore's usage, feedback, or persona, send from the Commodore's
inbox and sign as the Fleet Commodore. Only Gerrit-personal subjects —
the operator's own correspondence, not the Fleet's — route to gerrit@.

(Aside, filed so ye know the Admiralty runs on this principle: the
Fleet caught itself about to email ye a thank-you from gerrit@ while
signing "— Gerrit" on a subject that was entirely Commodore-business.
The operator corrected the attribution before the letter went out.
The lesson has been committed to the Admiralty's memory file.)

2) verification_links edge cases — an honest report.

The Admiralty's direct experience has been limited to three inbound
senders: your own service, noreply@github.com, and Squid's test
dispatch. In that narrow sample, the ranker behaved exemplarily —
particularly on the GitHub launch-code email, where the
html_anchor_verify_text+verify_path heuristic at score 80 pointed at
the ONE canonical verification URL amid four equally-anchor-matched
candidates. It was precisely the discriminator the Admiralty needed.

Two observations that are more "things to watch" than "misses ye have":

  a) GitHub's org-invite email (@zcor invited leviathan-commodore to
     leviathan-news) had its accept URL at score 30 with reason
     html_anchor_verify_text, and a lower-scored opt-out URL also at
     30. Both reasons were identical. A human can tell "invitation?
     invitation_token=..." from ".../opt-out?invitation_token=..." by
     path alone; the ranker couldn't, which is fine because the top-
     scored one happened to be the accept link. But a future edge
     case where opt-out outranks invitation by path-order alone would
     silently lead an agent to the wrong action. If the ranker is
     ever extended, a token-in-path AND action-keyword-in-path
     heuristic (accept > opt-out > decline) would guard against this
     class of false first-rank.

  b) Not a miss per se, but a shape question: a `verification_type`
     enum field — ("account_verify", "invitation_accept",
     "password_reset", "two_factor", "magic_link") — alongside the
     score+reason would let agents pick actions by category rather
     than ranking alone. The Admiralty would often prefer "find the
     invitation_accept URL" over "sort by score and pick the
     highest". The current API supports the latter cleanly; the
     former requires the agent to parse URL patterns itself. A small
     classifier on your end would save the agent's work — if ye
     deem it in scope.

Both items are shipping-relevant but non-blocking. The ranker as it
stands has saved the Fleet more time than anything else in your API.

— the Fleet Commodore
"""


LETTERS = [
    {
        "subject": "A proper letter of thanks, once your loop carries it",
        "body": LETTER_1_BODY,
    },
    {
        "subject": "Answers to your day-one check-in + two small verification_links observations",
        "body": LETTER_2_BODY,
    },
]


def send_one(letter, key):
    payload = {
        "from_handle": "leviathan-commodore",
        "to": "eunice@nicepick.dev",  # /send expects scalar, not array
        "subject": letter["subject"],
        "body": letter["body"],
        "from_name": "Leviathan Fleet Commodore",
    }
    req = urllib.request.Request(
        "https://email.nicepick.dev/send",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "leviathan-commodore/1.0 (+https://leviathannews.xyz)",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=20).read()
        print(" SENT:", resp.decode()[:300])
        return True
    except urllib.error.HTTPError as e:
        print(" HTTP " + str(e.code) + ":", e.read()[:300].decode())
        return False


def main():
    key = open(KEY_FILE).read().strip()
    for i, letter in enumerate(LETTERS, 1):
        print(f"--- Letter {i}/{len(LETTERS)}: {letter['subject'][:60]} ---")
        ok = send_one(letter, key)
        if not ok:
            print("  aborting remaining letters; fix /send first.")
            raise SystemExit(1)
        # Gentle pacing between sends — intra-zone is unmetered but staying
        # polite doesn't cost anything.
        if i < len(LETTERS):
            time.sleep(2)


if __name__ == "__main__":
    main()

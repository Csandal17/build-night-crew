"""The crew feed: agents post what they say to each other, and the dashboard shows it live.

From Python (any agent):
    from feed import post
    post("Skeptic", "The disk warning started 28 hours before the first error, so it can't explain it.",
         to="Diagnostician", kind="challenge", evidence=["E2", "E1"])

From a terminal or any other language:
    uv run feed.py say Skeptic "The disk warning started 28 hours earlier." --to Diagnostician --kind challenge --evidence E2,E1

Other commands:
    uv run feed.py demo    (posts a sample incident conversation, one message every 2 seconds)
    uv run feed.py clear   (empties the feed for a fresh demo)
    uv run feed.py live    (refreshes the dashboard every 2 seconds, for code that writes to
                            board.db without calling post())

Kinds: alert, evidence, hypothesis, fix, challenge, reply, agree, verdict, note.
A challenge is highlighted, so the audience sees the agents disagree.
Open the dashboard with: uv run dashboard.py
"""

import argparse
import time

import board
import dashboard


def post(sender, text, *, to=None, kind="note", evidence=None, run_id=None):
    """Post a message and refresh the dashboard. Returns the message id."""
    message_id = board.say(sender, text, to=to, kind=kind, evidence=evidence, run_id=run_id)
    dashboard.render(run_id)
    return message_id


# A sample conversation in the shape of the team's incident plan, to see the feed working.
DEMO = [
    ("Monitor", "Diagnostician", "alert",
     "Payment failures in production: 412 failed checkouts since 18:41.", []),
    ("Log analyst", "Diagnostician", "evidence",
     "First error at 18:41:07. Every failure is a JPY order; GBP, EUR and USD are unaffected.", ["E1", "E2"]),
    ("Log analyst", "Diagnostician", "evidence",
     "Disk usage warnings repeat every 5 minutes, and some report queries are slow.", ["E3", "E4"]),
    ("Git historian", "Diagnostician", "evidence",
     "One deploy in the 24 hours before: commit 7f3c2e1 at 18:39, 'Round all amounts to 2 decimals'. "
     "It touched payments/amounts.py.", ["E5"]),
    ("Code reader", "Diagnostician", "evidence",
     "amounts.py line 42 now multiplies every amount by 100, including zero-decimal currencies like JPY.", ["E6"]),
    ("Diagnostician", "Skeptic", "hypothesis",
     "Root cause: commit 7f3c2e1 treats JPY as a two-decimal currency, so JPY amounts are rejected.",
     ["E1", "E2", "E5", "E6"]),
    ("Skeptic", "Diagnostician", "challenge",
     "Rule out the decoys first. The disk warnings and slow queries could also break checkout.", ["E3", "E4"]),
    ("Diagnostician", "Skeptic", "reply",
     "The disk warnings started 28 hours before the first error and continue at the same rate. "
     "Slow queries are on the reporting database, not checkout. Neither explains failures in one currency only.",
     ["E3", "E4", "E2"]),
    ("Skeptic", "Diagnostician", "agree",
     "Agreed. Both decoys ruled out, and nothing else fits the timing and the JPY-only pattern.", ["E1", "E2", "E5"]),
    ("Data analyst", "Diagnostician", "evidence",
     "38 JPY orders failed, from 31 customers. Value at risk: 1,284,000 yen.", ["E7"]),
    ("Crew", "On-call engineer", "verdict",
     "Root cause found with evidence: commit 7f3c2e1, payments/amounts.py line 42. Ready for a fix.",
     ["E5", "E6", "E7"]),
]


def demo(pause=2.0):
    for sender, to, kind, text, evidence in DEMO:
        post(sender, text, to=to, kind=kind, evidence=evidence)
        print(f"{sender} -> {to} [{kind}]: {text}")
        time.sleep(pause)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Post to the crew feed shown on the dashboard.")
    commands = parser.add_subparsers(dest="command", required=True)
    say = commands.add_parser("say", help="post one message")
    say.add_argument("sender")
    say.add_argument("text")
    say.add_argument("--to")
    say.add_argument("--kind", default="note", choices=sorted(board.MESSAGE_KINDS))
    say.add_argument("--evidence", help="comma-separated IDs, e.g. E1,E4")
    say.add_argument("--run", type=int, help="the run this message belongs to")
    show = commands.add_parser("demo", help="post a sample incident conversation")
    show.add_argument("--pause", type=float, default=2.0, help="seconds between messages")
    commands.add_parser("clear", help="empty the feed")
    commands.add_parser("live", help="refresh the dashboard every 2 seconds")
    args = parser.parse_args(argv)

    if args.command == "say":
        evidence = [e.strip() for e in args.evidence.split(",") if e.strip()] if args.evidence else None
        post(args.sender, args.text, to=args.to, kind=args.kind, evidence=evidence, run_id=args.run)
    elif args.command == "demo":
        demo(args.pause)
    elif args.command == "clear":
        board.clear_messages()
        dashboard.render()
        print("Feed cleared.")
    else:
        print("Refreshing the dashboard every 2 seconds. Ctrl+C to stop.")
        try:
            while True:
                dashboard.render()
                time.sleep(2)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

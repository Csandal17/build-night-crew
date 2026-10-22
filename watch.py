"""Always-on crew: waits for work in inbox/ and wakes the crew only when something new arrives.

Plain code does the waiting, so nothing costs money while the inbox is quiet.

- Every few seconds, check inbox/ for new or edited .txt files (one claim or goal per file).
- Each file version gets a fixed ID: its name plus a fingerprint of its text. The same
  file seen twice is ignored, and an edited file counts as new work.
- Work runs one piece at a time, through the same crew as crew.py.
- If the watcher is stopped mid-run, that work goes back in the queue next time it starts.
- A failed piece of work is retried once, then left for a human.
- A daily spending cap stops new runs once today's total can't cover another one.

Run:  uv run watch.py            (stop with Ctrl+C)
      uv run watch.py --gemini   (every claim also gets an independent Gemini review)
      uv run watch.py --brief    (after each piece of work, a spoken briefing appears in the
                                 dashboard's player, ready to play; needs ELEVENLABS_API_KEY)
"""

import hashlib
import sys
import time
from datetime import datetime
from pathlib import Path

import board
import brief
import crew
import dashboard

HERE = Path(__file__).parent
INBOX = HERE / "inbox"

CHECK_SECONDS = 5
MAX_ATTEMPTS = 2          # first try plus one retry
DAILY_BUDGET_USD = 1.00   # across every run today, on top of each run's own budget
USE_GEMINI = False        # set by --gemini
BRIEF_AFTER_WORK = False  # set by --brief


def log(message):
    print(f"{datetime.now():%H:%M:%S}  {message}", flush=True)


def fingerprint(text):
    """A short code that changes whenever the text changes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def check_inbox():
    """Queue every new or edited file. Returns how many were queued."""
    queued = 0
    for path in sorted(INBOX.glob("*.txt")):
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue  # an empty file (maybe still being written) is skipped until it has text
        if board.add_event(path.name, fingerprint(text), text):
            log(f"New work: {path.name}")
            queued += 1
    return queued


def make_briefing():
    """With --brief: a spoken briefing of what just happened, ready to play on the dashboard.
    A problem with ElevenLabs is logged and never stops the watcher."""
    if not BRIEF_AFTER_WORK:
        return
    try:
        path = brief.main(["--no-play"])
        if path:
            log(f"Briefing ready to play on the dashboard ({path.name}).")
    except (Exception, SystemExit) as problem:
        log(f"Couldn't make a briefing this time: {problem}")


def can_afford_a_run():
    """Only start a run if today's spending plus that run's full budget stays under the cap.
    Same rule as llm.py: never start what you can't afford in the worst case."""
    return board.spent_today() + crew.BUDGET_USD <= DAILY_BUDGET_USD


def work_on(event):
    """Run the crew on one piece of work and record how it went."""
    if event["attempts"] >= MAX_ATTEMPTS:
        board.update_event(event["id"], status="failed",
                           error=event["error"] or "Interrupted too many times.")
        log(f"Giving up on {event['source']} after {event['attempts']} attempts. Needs a human.")
        make_briefing()
        return

    attempt = event["attempts"] + 1
    board.update_event(event["id"], status="running", attempts=attempt)
    dashboard.render()
    log(f"Waking the crew for {event['source']} (attempt {attempt} of {MAX_ATTEMPTS})")

    try:
        run_id = crew.main(event["goal"], use_gemini=USE_GEMINI)
    except Exception as error:
        retry = attempt < MAX_ATTEMPTS
        board.update_event(event["id"], status="waiting" if retry else "failed",
                           error=str(error)[:300])
        dashboard.render()
        log(f"{event['source']} hit an error: {error}. "
            + ("Will try once more." if retry else "Giving up. Needs a human."))
        if not retry:
            make_briefing()
        return

    board.update_event(event["id"], status="done", run_id=run_id, error=None)
    dashboard.render(run_id)
    outcome = board.summary(run_id)["run"]["status"].replace("_", " ")
    log(f"Done: {event['source']} (run {run_id}, {outcome})")
    make_briefing()


def main():
    INBOX.mkdir(exist_ok=True)
    interrupted_runs = board.close_interrupted_runs()
    recovered = board.recover_events()
    log(f"Always-on crew started. Checking {INBOX.name}/ every {CHECK_SECONDS}s, "
        f"daily cap ${DAILY_BUDGET_USD:.2f} (${board.spent_today():.4f} spent today)."
        + (" Gemini second review is on." if USE_GEMINI else "")
        + (" A briefing follows each piece of work." if BRIEF_AFTER_WORK else ""))
    if interrupted_runs:
        log(f"Marked {interrupted_runs} run(s) cut off last time as interrupted.")
    if recovered:
        log(f"Put {recovered} piece(s) of interrupted work back in the queue.")
    dashboard.render()

    said_waiting = said_paused = False
    try:
        while True:
            if check_inbox():
                dashboard.render()
            event = board.next_event()

            if event is None:
                if not said_waiting:
                    log("Waiting for work. Drop a .txt file into inbox/.")
                    said_waiting = True
                time.sleep(CHECK_SECONDS)
                continue

            if not can_afford_a_run():
                if not said_paused:
                    log(f"Daily cap reached: ${board.spent_today():.4f} spent today. "
                        "New work stays queued until tomorrow.")
                    said_paused = True
                time.sleep(CHECK_SECONDS)
                continue

            said_waiting = said_paused = False
            work_on(event)
    except KeyboardInterrupt:
        log("Stopped. Anything waiting or cut off stays in the queue for next time.")


if __name__ == "__main__":
    USE_GEMINI = "--gemini" in sys.argv[1:]
    BRIEF_AFTER_WORK = "--brief" in sys.argv[1:]
    main()

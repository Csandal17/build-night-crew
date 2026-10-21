"""Brief me: a short spoken handover of what the always-on crew did while you were away.

The script is written by code from the task board (board.db), not by a model, so it can
only say what actually happened: what arrived, each verdict, what a reviewer sent back,
and what needs your decision. ElevenLabs then reads it out in two voices: a narrator
for the crew, and a second voice for the reviewers.

Run:  uv run brief.py             (everything since your last briefing; plays on a Mac)
      uv run brief.py --dry-run   (print the script only: free, no audio, nothing saved)
      uv run brief.py --all       (the latest few pieces of work, even if already briefed)
      uv run brief.py --no-play   (save the audio without playing it)

Needs ELEVENLABS_API_KEY in .env. ElevenLabs charges by the character, and the script
is capped at under 2,000 characters, so one briefing stays small.
"""

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

import board

load_dotenv()

HERE = Path(__file__).parent
OUTPUT_DIR = HERE / "output"
MAX_ITEMS = 5
MAX_CHARS = 1900  # ElevenLabs dialogue takes up to 2,000 characters per request

# Default voices from ElevenLabs' library. Change them in .env if you prefer others.
VOICES = {
    "narrator": os.getenv("ELEVENLABS_VOICE_NARRATOR", "JBFqnCBsd6RMkjVDRZzb"),  # George
    "reviewer": os.getenv("ELEVENLABS_VOICE_REVIEWER", "pFZP5JQG7iQjIQuC4Bku"),  # Lily
}
DIALOGUE_URL = "https://api.elevenlabs.io/v1/text-to-dialogue"
SPEECH_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
ONE_VOICE_MODEL = "eleven_multilingual_v2"


# ---------- small helpers for spoken English ----------

def _name(source):
    """vitamin-d.txt -> Vitamin d"""
    return Path(source).stem.replace("-", " ").replace("_", " ").capitalize()


def _sentence(text, limit=150):
    """As many whole sentences as fit in `limit` characters, since it will be read aloud.
    Only a single over-long sentence gets cut, at a word boundary."""
    text = " ".join((text or "").split())
    if not text:
        return ""
    spoken = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(spoken) + len(sentence) + 1 > limit:
            break
        spoken = f"{spoken} {sentence}".strip()
    if not spoken:
        spoken = text[:limit].rsplit(" ", 1)[0].rstrip(",.:;")
    return spoken if spoken.endswith((".", "!", "?")) else spoken + "."


def _topic(title):
    """'Check UK adult vitamin D advice' -> 'UK adult vitamin D advice'"""
    title = (title or "").strip()
    for prefix in ("Check ", "Checking ", "Verify "):
        if title.startswith(prefix):
            title = title[len(prefix):]
    return title[:1].upper() + title[1:]


def _count(n, one, many):
    return f"{n} {one if n == 1 else many}"


def _cents(usd):
    cents = round(usd * 100)
    if cents < 1:
        return "less than a cent"
    return f"about {_count(cents, 'cent', 'cents')}"


def _list(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _clock(iso):
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")


# ---------- the script, written by code from the board ----------

def _event_lines(event, tally):
    """What to say about one piece of work. Returns (lines, needs_you).
    The reviewer's voice only speaks when something happened worth hearing;
    routine passes are added to `tally` and summed up once at the end."""
    name = _name(event["source"])
    if event["status"] == "failed":
        return [("narrator", f"{name} could not be run. {_sentence(event['error'])}")], True
    if event["status"] == "running":
        return [("narrator", f"{name} is being checked right now.")], False
    if event["status"] == "waiting":
        return [("narrator", f"{name} is waiting in the queue.")], False
    if not event["run_id"]:
        return [], False

    s = board.summary(event["run_id"])
    if s["run"]["status"] == "stopped_budget":
        return [("narrator", f"{name} stopped at its budget cap before finishing.")], True

    lines, needs_you = [], False
    several = len(s["tasks"]) > 1
    for task in s["tasks"]:
        label = f"{name}, {_topic(task['title'])}" if several else name
        head, _, summary = (task["result"] or "").partition(": ")
        verdict = head.replace("PLANTED MISTAKE, ", "").strip().lower() or "no verdict"
        notes = task["review_notes"] or ""
        if task["status"] == "done":
            tally["passed"] += 1
            if "Gemini agreed" in notes or "Both reviewers passed" in notes:
                tally["both"] += 1
            lines.append(("narrator", f"{label}: {verdict}. {_sentence(summary)}".strip()))
            if notes.startswith(("Caught:", "Planted mistake caught:")):
                caught = notes.split(": ", 1)[1]
                for ending in (" Both reviewers passed it.", " Fixed and passed."):
                    caught = caught.replace(ending, "")
                lines.append(("reviewer", f"I sent this one back first. {_sentence(caught, 140)} "
                                          "It was fixed, and then it passed."))
        else:
            needs_you = True
            reason = notes.split(": ", 1)[-1] if notes else "The reviewers could not pass it."
            lines.append(("reviewer", f"I couldn't sign off {label[:1].lower() + label[1:]}. "
                                      f"{_sentence(reason, 140)} "
                                      "It needs your decision."))
    return lines, needs_you


def _review_summary(tally):
    """One line from the reviewer about the routine passes."""
    passed, both = tally["passed"], tally["both"]
    if not passed:
        return None
    if both == passed:
        return ("Both reviewers passed it." if passed == 1 else
                f"Both reviewers, Claude and Gemini, independently passed all {passed} verdicts.")
    if both:
        return f"Both reviewers passed {both} of the {passed} verdicts. I passed the rest on my own."
    return "I passed it." if passed == 1 else f"I passed all {passed} verdicts."


def build_script(show_all=False):
    """Returns (lines, since) where lines is a list of (speaker, text)."""
    last = None if show_all else board.last_briefing()
    since = last["created_at"] if last else None
    events = board.events_since(since, limit=MAX_ITEMS)
    if not events:
        return [], since
    rows = board._fetch(
        "SELECT status, COUNT(*) AS n FROM events"
        + (" WHERE updated_at > ?" if since else "") + " GROUP BY status",
        (since,) if since else (),
    )
    counts = {row["status"]: row["n"] for row in rows}
    changed = sum(counts.values())
    labels = (("done", "finished"), ("running", "in progress"),
              ("waiting", "waiting"), ("failed", "could not run"))
    parts = [f"{counts[k]} {label}" for k, label in labels if counts.get(k)]
    opener = f"Here's your crew briefing. Since {_clock(since)}, " if since else "Here's your crew briefing. "
    work = _count(changed, "piece of work", "pieces of work")
    if counts.get("done") == changed:
        opener += f"{work} finished." if changed > 1 else "1 piece of work finished."
    else:
        opener += f"{work} changed: {_list(parts)}."
    closing_start = f"Spent today: {_cents(board.spent_today())}."

    lines = [("narrator", opener)]
    needs_you = []
    tally = {"passed": 0, "both": 0}
    used = len(opener) + len(closing_start) + 220  # room for the review summary and closing
    told = 0
    for event in events:
        event_tally = {"passed": 0, "both": 0}
        block, needs = _event_lines(event, event_tally)
        size = sum(len(text) for _, text in block)
        if used + size > MAX_CHARS:
            break
        lines += block
        used += size
        told += 1
        for key in tally:
            tally[key] += event_tally[key]
        if needs:
            needs_you.append(_name(event["source"]).lower())
    summary = _review_summary(tally)
    if summary:
        lines.append(("reviewer", summary))
    if changed > told:
        lines.append(("narrator", f"There's {_count(changed - told, 'more item', 'more items')} on the dashboard."))

    closing = closing_start + (
        f" {_count(len(needs_you), 'item needs', 'items need')} your decision: {_list(needs_you)}."
        if needs_you else " Nothing needs your decision.")
    lines.append(("narrator", closing))
    return lines, since


# ---------- the audio ----------

def _error(response):
    try:
        detail = response.json().get("detail")
        return detail.get("message") if isinstance(detail, dict) else str(detail)[:200]
    except ValueError:
        return response.text[:200]


def speak(lines, path):
    """Two voices through ElevenLabs' dialogue endpoint; one voice if that isn't available."""
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        raise SystemExit("No ELEVENLABS_API_KEY in .env.")
    headers = {"xi-api-key": key, "Content-Type": "application/json"}

    body = {"inputs": [{"text": text, "voice_id": VOICES[who]} for who, text in lines]}
    response = httpx.post(DIALOGUE_URL, headers=headers, json=body, timeout=120.0)
    if response.status_code == 200:
        path.write_bytes(response.content)
        return "two voices"

    print(f"Two-voice audio unavailable ({response.status_code}: {_error(response)}). "
          "Trying one voice instead.")
    text = " ".join(text for _, text in lines)
    response = httpx.post(SPEECH_URL.format(voice_id=VOICES["narrator"]), headers=headers,
                          json={"text": text, "model_id": ONE_VOICE_MODEL}, timeout=120.0)
    if response.status_code != 200:
        raise SystemExit(f"ElevenLabs returned {response.status_code}: {_error(response)}")
    path.write_bytes(response.content)
    return "one voice"


def main(args):
    lines, since = build_script(show_all="--all" in args)
    if not lines:
        when = f" at {_clock(since)}" if since else ""
        print(f"Nothing new since your last briefing{when}. Use --all to hear the latest anyway.")
        return None

    characters = sum(len(text) for _, text in lines)
    for who, text in lines:
        print(f"{who:>9}: {text}")
    print(f"\n{characters} characters.")
    if "--dry-run" in args:
        print("Dry run: no audio made, nothing charged.")
        return None

    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"briefing_{datetime.now():%Y%m%d_%H%M%S}.mp3"
    how = speak(lines, path)
    board.add_briefing(characters, str(path.relative_to(HERE)))
    print(f"Saved {path.relative_to(HERE)} ({how}).")
    if "--no-play" not in args and shutil.which("afplay"):
        subprocess.run(["afplay", str(path)])
    return path


if __name__ == "__main__":
    main(sys.argv[1:])

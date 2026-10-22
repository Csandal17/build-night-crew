"""Brief me: a short spoken handover of what the always-on crew did while you were away.

The script is written by code from the task board (board.db), not by a model, so it can
only say what actually happened: what arrived, each verdict, what a reviewer sent back,
and what needs your decision. ElevenLabs then reads it out in two voices: a narrator
for the crew, and a second voice for the reviewers.

Run:  uv run brief.py             (everything since your last briefing; plays on a Mac,
                                   and appears in the dashboard's "Latest briefing" player)
      uv run brief.py --dry-run   (print the script only: free, no audio, nothing saved)
      uv run brief.py --all       (the latest few pieces of work, even if already briefed)
      uv run brief.py --detail    (one line per claim with its reason, instead of grouped by verdict)
      uv run brief.py --no-play   (save the audio without playing it)

Needs ELEVENLABS_API_KEY in .env. ElevenLabs charges by the character, and the script
is capped at under 2,000 characters, so one briefing stays small.
"""

import json
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
import dashboard

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

VERDICT_ORDER = ("supported", "contradicted", "inconclusive")


def _lower_first(text):
    """Lower-case the first letter mid-sentence, but leave acronyms like UK alone."""
    first = text.split(" ", 1)[0]
    if len(first) > 1 and first.isupper():
        return text
    return text[:1].lower() + text[1:]


def _run_name(goal):
    """A run's title without its tags: 'Evidence check: three claims · Challenge' -> 'Evidence check: three claims'"""
    return goal.split(" · ")[0].strip()


def _items_since(since):
    """Everything worth briefing on since `since`, oldest first, as (status, item) pairs:
    every finished run (started by crew.py or by the inbox watcher), plus inbox work that
    hasn't reached a finished run yet. With no `since`, everything there is."""
    where = " WHERE COALESCE(finished_at, started_at) > ?" if since else ""
    runs = board._fetch("SELECT * FROM runs" + where + " ORDER BY id", (since,) if since else ())
    from_inbox = {e["run_id"]: e for e in board._fetch("SELECT * FROM events WHERE run_id IS NOT NULL")}
    items = []
    for run in runs:
        # Running, interrupted and crashed runs are told through their inbox entry instead.
        if run["status"] in ("running", "interrupted", "failed"):
            continue
        event = from_inbox.get(run["id"])
        name = _name(event["source"]) if event else _run_name(run["goal"])
        status = "stopped" if run["status"] == "stopped_budget" else "done"
        items.append((run["finished_at"] or run["started_at"], status, {"run": run, "name": name}))
    pending = board._fetch(
        "SELECT * FROM events WHERE status IN ('waiting', 'running', 'failed')"
        + (" AND updated_at > ?" if since else "") + " ORDER BY id", (since,) if since else ())
    for event in pending:
        items.append((event["updated_at"], event["status"], {"event": event, "name": _name(event["source"])}))
    items.sort(key=lambda item: item[0])
    return [(status, item) for _, status, item in items]


def _item_lines(item, tally):
    """What happened to one piece of work.
    Returns (claims, notes, needs_you): claims are (label, verdict, summary) for each
    claim that passed review; notes are lines worth saying on their own (something sent
    back, something that needs you, work that failed or is still queued)."""
    name = item["name"]
    event = item.get("event")
    if event:
        if event["status"] == "failed":
            return [], [("narrator", f"{name} could not be run. {_sentence(event['error'])}")], True
        if event["status"] == "running":
            return [], [("narrator", f"{name} is being checked right now.")], False
        return [], [("narrator", f"{name} is waiting in the queue.")], False

    s = board.summary(item["run"]["id"])
    if s["run"]["status"] == "stopped_budget":
        return [], [("narrator", f"{name} stopped at its budget cap before finishing.")], True

    if s["run"]["goal"].startswith("Incident:"):
        return _incident_lines(item, s)

    claims, notes, needs_you = [], [], False
    several = len(s["tasks"]) > 1
    for task in s["tasks"]:
        label = _topic(task["title"]) if several else name
        head, _, summary = (task["result"] or "").partition(": ")
        verdict = head.replace("PLANTED MISTAKE, ", "").strip().lower() or "no verdict"
        review = task["review_notes"] or ""
        if task["status"] == "done":
            tally["passed"] += 1
            if "Gemini agreed" in review or "Both reviewers passed" in review:
                tally["both"] += 1
            claims.append((label, verdict, summary))
            if review.startswith(("Caught:", "Planted mistake caught:")):
                caught = review.split(": ", 1)[1]
                for ending in (" Both reviewers passed it.", " Fixed and passed."):
                    caught = caught.replace(ending, "")
                if review.startswith("Planted"):
                    opening = f"A mistake was planted in {_lower_first(label)} on purpose, to test me. I caught it."
                else:
                    opening = f"I sent {_lower_first(label)} back first."
                notes.append(("reviewer", f"{opening} {_sentence(caught, 140)} "
                                          "It was fixed, and then it passed."))
        else:
            needs_you = True
            reason = review.split(": ", 1)[-1] if review else "The reviewers could not pass it."
            notes.append(("reviewer", f"I couldn't sign off {_lower_first(label)}. "
                                      f"{_sentence(reason, 140)} It needs your decision."))
    return claims, notes, needs_you


def _incident_lines(item, s):
    """An SRE crew run (sre_crew.py), told from its incident record in output/.
    Returns no claims, just lines, and always needs you: a fix waits for an engineer."""
    try:
        record = json.loads((OUTPUT_DIR / f"incident_run{item['run']['id']}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {}
    alert = record.get("alert", {})
    where = alert.get("project", "").replace("-", " ") or "production"
    item["name"] = f"the {where} fix" if alert.get("project") else "the incident fix"  # said in the closing line
    what = alert.get("message") or alert.get("title") or "an error"
    affected = alert.get("users_affected")
    notes = [("narrator", f"A production alert on {where}. {_sentence(what)}"
                          + (f" {affected} customers were affected." if affected else ""))]
    for c in record.get("caught", []):
        if c["planted"]:
            opening = "The first fix was a shortcut, planted on purpose to test us. It stopped the crash, but"
        else:
            opening = f"Attempt {c['attempt']} was sent back:"
        found = c["feedback"].split(": ", 1)
        if c["by"] == "tester" and len(found) == 2:
            names = [part.split(" (")[0] for part in found[1].rstrip(".").split("; ")]
            detail = f"the tester's checks failed: {_list(names)}."
        else:
            detail = f"the {c['by']} said: {_sentence(c['feedback'], 140)}"
        notes.append(("reviewer", f"{opening} {detail} We sent it back."))
    if record.get("status") == "done":
        tests = record.get("tests", "").removeprefix("PASSED: ")
        notes.append(("narrator", f"The fix: {_sentence(record.get('fix'), 160)} It passed {tests}"))
        notes.append(("reviewer", "Claude and Gemini both reviewed the change and passed it." if record.get("gemini")
                      else "I reviewed the change and passed it."))
        notes.append(("narrator", ("It's waiting for you as a draft pull request." if record.get("pr_url")
                                   else "It's waiting for you on its own branch.") + " Nothing was merged."))
    else:
        notes.append(("reviewer", "I couldn't sign off a fix, so nothing was committed. It needs your decision."))
    return [], notes, True


def _claim_lines(claims, detail):
    """Detail: one line per claim, with its reason. Otherwise: one line per verdict,
    listing the claims and giving the reason once, so repeats don't drone on."""
    if detail:
        return [("narrator", f"{label}: {verdict}. {_sentence(summary)}".strip())
                for label, verdict, summary in claims]
    lines = []
    verdicts = [v for v in VERDICT_ORDER if any(c[1] == v for c in claims)]
    verdicts += sorted({c[1] for c in claims} - set(verdicts))
    for verdict in verdicts:
        group = [c for c in claims if c[1] == verdict]
        names = _list([label if n == 0 else _lower_first(label) for n, (label, _, _) in enumerate(group)])
        reason = _sentence(group[0][2])
        lines.append(("narrator", f"{verdict.capitalize()}: {names}. {reason}".strip()))
    return lines


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


def _compose(items, since, statuses, detail):
    """The whole script for these items, as a list of (speaker, text)."""
    changed = len(statuses)
    labels = (("done", "finished"), ("stopped", "stopped at the budget cap"), ("running", "in progress"),
              ("waiting", "waiting"), ("failed", "could not run"))
    parts = [f"{statuses.count(k)} {label}" for k, label in labels if statuses.count(k)]
    opener = f"Here's your crew briefing. Since {_clock(since)}, " if since else "Here's your crew briefing. "
    work = _count(changed, "piece of work", "pieces of work")
    opener += f"{work} finished." if statuses.count("done") == changed else f"{work} changed: {_list(parts)}."

    claims, notes, needs_you = [], [], []
    tally = {"passed": 0, "both": 0}
    for item in items:
        item_claims, item_notes, needs = _item_lines(item, tally)
        claims += item_claims
        notes += item_notes
        if needs:
            needs_you.append(_lower_first(item["name"]))

    lines = [("narrator", opener)] + _claim_lines(claims, detail) + notes
    summary = _review_summary(tally)
    if summary:
        lines.append(("reviewer", summary))
    if changed > len(items):
        lines.append(("narrator", f"There's {_count(changed - len(items), 'more item', 'more items')} "
                                  "on the dashboard."))
    lines.append(("narrator", f"Spent today: {_cents(board.spent_today())}." + (
        f" {_count(len(needs_you), 'item needs', 'items need')} your decision: {_list(needs_you)}."
        if needs_you else " Nothing needs your decision.")))
    return lines


def build_script(show_all=False, detail=False):
    """Returns (lines, since) where lines is a list of (speaker, text)."""
    last = None if show_all else board.last_briefing()
    since = last["created_at"] if last else None
    everything = _items_since(since)
    if not everything:
        return [], since
    statuses = [status for status, _ in everything]
    items = [item for _, item in everything][-MAX_ITEMS:]
    # Keep under ElevenLabs' limit: if it's too long, leave the oldest work for the dashboard.
    for keep in range(len(items), 0, -1):
        lines = _compose(items[-keep:], since, statuses, detail)
        if sum(len(text) for _, text in lines) <= MAX_CHARS:
            break
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
    lines, since = build_script(show_all="--all" in args, detail="--detail" in args)
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
    # The script goes next to the audio, so the dashboard can show who says what.
    path.with_suffix(".json").write_text(
        json.dumps({"lines": lines, "characters": characters, "voices": how}), encoding="utf-8")
    board.add_briefing(characters, str(path.relative_to(HERE)))
    dashboard.render()  # the newest briefing appears in the dashboard's player
    print(f"Saved {path.relative_to(HERE)} ({how}). It's also on the dashboard.")
    if "--no-play" not in args and shutil.which("afplay"):
        subprocess.run(["afplay", str(path)])
    return path


if __name__ == "__main__":
    main(sys.argv[1:])

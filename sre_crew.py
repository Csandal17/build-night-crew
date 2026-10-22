"""SRE crew: a production alert wakes the crew, which fixes the bug and proves the fix.

1. Fixer: reads the alert and the code, and makes the smallest change that fixes the root
   cause. Claude does it directly by default; with --sparkles, a Sparkles coding agent does.
2. Tester: plain code, no model. Copies the fixed project to a clean folder, adds hidden
   tests the fixer has never seen, and runs every test. So a pass means the fix works,
   not that it was written to pass the test.
3. Reviewers: Claude reviews the real change together with the test results (and Gemini
   too, with --gemini; both must pass).
4. Anything sent back gets one revision with exactly that feedback. Still failing: held
   for a human and marked not verified.
5. A passed fix is committed on its own branch, with a pull request description in output/
   (and a draft pull request with --pr, if the project has a GitHub remote). Nothing is
   merged: an engineer decides.

Challenge mode (--challenge) replaces the fixer's first attempt with a shortcut fix that
stops the crash but quietly charges some customers full price. It shows the crew catching
what a single agent would have shipped.

Run:  uv run sre_crew.py --reset                                   (fresh copy of the demo project)
      uv run sre_crew.py sre/alerts/checkout-keyerror.json          (one alert, start to finish)
      uv run sre_crew.py sre/alerts/checkout-keyerror.json --challenge --gemini
Always-on: uv run watch.py, then copy an alert into inbox/ (see the README).

The demo project lives outside this folder (SRE_APP_DIR in .env, default ~/sre-demo-app),
so no agent ever works next to the crew's API keys.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

import board
import crew
import dashboard
import feed
import gemini
import sparkles_agent
from llm import SMART, BudgetExceeded, ask_json

load_dotenv()

HERE = Path(__file__).parent
TEMPLATE = HERE / "sre" / "demo_app"
HIDDEN_TESTS = HERE / "sre" / "hidden_tests"
OUTPUT_DIR = HERE / "output"
APP_DIR = Path(os.getenv("SRE_APP_DIR") or Path.home() / "sre-demo-app").expanduser()
MARKER = ".sre-demo"  # only folders made by --reset are ever cleaned automatically

BUDGET_USD = 0.25
MAX_ATTEMPTS = 2       # first try plus one revision (challenge mode adds the planted try)
TEST_TIMEOUT = 60      # seconds
MAX_FILE_CHARS = 6000  # per file shown to the fixer
MAX_DIFF_CHARS = 8000  # how much of the change the reviewers see

FIXER_SYSTEM = (
    "You are the fixer on an on-call crew. A production alert came in. Find the root cause "
    "in the code and make the smallest change that fixes it properly: not just the crash, "
    "but the behaviour customers expect. Don't edit tests, don't add dependencies and don't "
    "touch unrelated code. Another agent will run tests you haven't seen, and reviewers "
    "will read your change."
)
REVIEWER_SYSTEM = (
    "You review an AI-made fix for a production incident before an engineer is asked to "
    "merge it. Judge the actual change and the test results, not the fixer's summary. Pass "
    "it only if it fixes the root cause the alert describes, gives customers the expected "
    "behaviour and changes nothing unrelated. Don't nitpick style. If you send it back, say "
    "exactly what to fix in one or two sentences."
)
FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string",
                    "description": "one or two plain-English sentences: the root cause, then the fix"},
        "files": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "an existing file, e.g. checkout.py"},
                "content": {"type": "string", "description": "the complete new content of that file"},
            },
            "required": ["path", "content"],
        }},
    },
    "required": ["summary", "files"],
}

# Challenge mode's planted shortcut: the crash stops, but valid codes typed in lower case
# silently lose their discount.
SHORTCUT = ("percent = DISCOUNTS[code]", "percent = DISCOUNTS.get(code, 0)")


# ---------- the alert ----------

def load_alert(alert):
    """An alert as a dict, from a file path, JSON text or a dict. Takes this demo's
    Sentry-style shape and, loosely, Sentry's own webhook JSON (data.event)."""
    if isinstance(alert, Path) or (isinstance(alert, str) and not alert.lstrip().startswith("{")):
        alert = Path(alert).read_text(encoding="utf-8")
    if isinstance(alert, str):
        alert = json.loads(alert)
    event = alert.get("data", {}).get("event") if isinstance(alert.get("data"), dict) else None
    if isinstance(event, dict):
        frames = []
        for value in (event.get("exception") or {}).get("values") or []:
            for f in (value.get("stacktrace") or {}).get("frames") or []:
                frames.append({"filename": f.get("filename"), "function": f.get("function"),
                               "lineno": f.get("lineno"), "context": (f.get("context_line") or "").strip()})
        alert = {"source": "sentry", "title": event.get("title") or "Production error",
                 "culprit": event.get("culprit", ""), "environment": event.get("environment", ""),
                 "stacktrace": frames, "url": event.get("web_url", "")}
    return alert


def alert_text(alert):
    """The alert as the fixer and the reviewers read it."""
    lines = [f"Production alert from {alert.get('source', 'monitoring')}: {alert.get('title', 'error')}"]
    for key, label in (("project", "Project"), ("environment", "Environment"), ("culprit", "Where"),
                       ("message", "What customers see"), ("count", "Events"),
                       ("users_affected", "Customers affected"), ("first_seen", "First seen")):
        if alert.get(key):
            lines.append(f"{label}: {alert[key]}")
    if alert.get("request"):
        lines.append(f"Request: {json.dumps(alert['request'])}")
    if alert.get("stacktrace"):
        lines.append("Stack trace (most recent call last):")
        lines += [f"  {f.get('filename')}:{f.get('lineno')} in {f.get('function')}: {f.get('context', '')}"
                  for f in alert["stacktrace"]]
    if alert.get("expected"):
        lines.append(f"Expected behaviour: {alert['expected']}")
    return "\n".join(lines)


# ---------- the project being fixed ----------

def _git(app, *args):
    return sparkles_agent._git(app, *args)


def _commit(app, message):
    _git(app, "add", "-A")
    _git(app, "-c", "user.name=SRE crew", "-c", "user.email=sre-crew@example.invalid",
         "commit", "-q", "-m", message)


def reset_app(app=None):
    """A fresh copy of the demo project, with its production bug, as its own git repository."""
    app = Path(app or APP_DIR).expanduser()
    if app.exists():
        if not (app / MARKER).exists():
            raise RuntimeError(f"{app} already exists and wasn't made by --reset, so it was left "
                               "alone. Point SRE_APP_DIR at a new folder.")
        shutil.rmtree(app)
    shutil.copytree(TEMPLATE, app, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
    (app / MARKER).write_text("Made by build-night-crew (sre_crew.py --reset). Safe to delete.\n")
    (app / ".gitignore").write_text("__pycache__/\n.DS_Store\n")
    _git(app, "init", "-q")
    _git(app, "symbolic-ref", "HEAD", "refs/heads/main")
    _commit(app, "Checkout service")
    return app


def prepare(app):
    """Start every incident from a clean project. Returns the branch the fix will be based on."""
    if not (app / ".git").exists():
        print(f"Setting up the demo project in {app}")
        reset_app(app)
    if (app / MARKER).exists():  # our demo copy: safe to put back to its starting point
        _git(app, "checkout", "-q", "-f", "main")
        _git(app, "reset", "-q", "--hard")
        _git(app, "clean", "-fdq")
        return "main"
    if _git(app, "status", "--porcelain").strip():
        raise RuntimeError(f"{app} has uncommitted changes. Commit or stash them first.")
    return _git(app, "rev-parse", "--abbrev-ref", "HEAD").strip()


def _project_files(app):
    files = {}
    for path in sorted(app.rglob("*.py")):
        rel = path.relative_to(app)
        if ".git" in rel.parts or "__pycache__" in rel.parts:
            continue
        files[rel.as_posix()] = path.read_text(encoding="utf-8")[:MAX_FILE_CHARS]
    return files


def _is_test(name):
    return name.startswith("tests/") or Path(name).name.startswith("test_")


# ---------- the agents ----------

def claude_fix(run_id, task_id, app, prompt):
    """The fixer, with Claude directly: it sees the code and returns whole new files."""
    files = _project_files(app)
    listing = "\n\n".join(f"--- {name}\n{text}" for name, text in files.items())
    reply = ask_json(run_id, "fixer", f"{prompt}\n\nThe project's files:\n\n{listing}", FIX_SCHEMA,
                     system=FIXER_SYSTEM, model=SMART, max_tokens=3000, task_id=task_id)
    ignored = []
    for change in reply.get("files", []):
        name = str(change.get("path", "")).strip().removeprefix("./")
        if name not in files or _is_test(name):  # only existing code files, never the tests
            ignored.append(name or "(no name)")
            continue
        (app / name).write_text(change.get("content", ""), encoding="utf-8")
    diff, changed = sparkles_agent.changes(app)
    message = reply.get("summary", "").strip()
    if ignored:
        message += f" (Ignored changes to {', '.join(ignored)}: the fixer may only edit existing code, not tests.)"
    return {"message": message, "diff": diff[:MAX_DIFF_CHARS], "files": changed}


def sparkles_fix(run_id, task_id, app, prompt):
    """The fixer, as a Sparkles coding agent. Swap sparkles_agent.run for the cloud
    sandbox call and nothing else here changes."""
    work = sparkles_agent.run(prompt + "\n\nDon't edit the tests.", app, run_id=run_id,
                              task_id=task_id, agent="sparkles-fixer")
    return {"message": work["message"], "diff": work["diff"], "files": work["files"]}


def planted_shortcut(app):
    """Challenge mode: a shortcut fix a single agent might well ship."""
    path = app / "checkout.py"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if SHORTCUT[0] not in text:
        raise RuntimeError("Challenge mode needs the demo project as it starts. Run: uv run sre_crew.py --reset")
    path.write_text(text.replace(*SHORTCUT), encoding="utf-8")
    diff, changed = sparkles_agent.changes(app)
    return {"message": "Look the code up with .get(), so an unknown code no longer raises KeyError.",
            "diff": diff, "files": changed}


def _readable(test_name):
    return test_name.removeprefix("test_").replace("_", " ")


def run_tests(app):
    """The tester: plain code, no model. Copies the project to a clean folder, adds the
    hidden tests and runs everything, with none of the crew's keys in reach.
    This is the one function to move into a second cloud sandbox."""
    hidden = sorted(HIDDEN_TESTS.glob("test_*.py"))
    hidden_count = sum(len(re.findall(r"^\s*def test_", f.read_text(encoding="utf-8"), re.M)) for f in hidden)
    with tempfile.TemporaryDirectory(prefix="sre-tests-") as tmp:
        copy = Path(tmp) / "app"
        shutil.copytree(app, copy, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        (copy / "tests").mkdir(exist_ok=True)
        for test_file in hidden:
            shutil.copy(test_file, copy / "tests" / test_file.name)
        try:
            result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                                    cwd=copy, capture_output=True, text=True, timeout=TEST_TIMEOUT,
                                    env=sparkles_agent._agent_env())
        except subprocess.TimeoutExpired:
            return {"passed": False, "ran": 0, "hidden": hidden_count, "failures": [],
                    "summary": f"FAILED: the tests were still running after {TEST_TIMEOUT} seconds.",
                    "feedback": f"The tests didn't finish within {TEST_TIMEOUT} seconds."}
    out = result.stderr + result.stdout
    found = re.search(r"Ran (\d+) tests?", out)
    ran = int(found.group(1)) if found else 0
    failures = []
    for block in re.split(r"^={20,}$", out, flags=re.M)[1:]:
        head = re.search(r"^(?:FAIL|ERROR): (\w+)", block, re.M)
        if not head:
            continue
        parts = re.split(r"^-{20,}$", block, flags=re.M)
        trace = [line.strip() for line in (parts[1] if len(parts) > 1 else "").splitlines() if line.strip()]
        failures.append((_readable(head.group(1)), trace[-1][:120] if trace else ""))

    hidden_note = f"{hidden_count} hidden {'one' if hidden_count == 1 else 'ones'} the fixer never saw"
    if result.returncode == 0 and ran:
        return {"passed": True, "ran": ran, "hidden": hidden_count, "failures": [],
                "summary": f"PASSED: all {ran} tests, including {hidden_note}.", "feedback": ""}
    if not failures:
        detail = out.strip().splitlines()[-1][:200] if out.strip() else "no output"
        return {"passed": False, "ran": ran, "hidden": hidden_count, "failures": [],
                "summary": f"FAILED: the tests couldn't run ({detail}).",
                "feedback": f"The tests couldn't run: {detail}"}
    names = "; ".join(f"{name} ({why})" for name, why in failures)
    return {"passed": False, "ran": ran, "hidden": hidden_count, "failures": failures,
            "summary": f"FAILED: {len(failures)} of {ran} tests ({hidden_note} were among the {ran}): {names}",
            "feedback": f"{len(failures)} of {ran} tests failed ({hidden_note} were among the {ran}): {names}."}


def review(run_id, task_id, text, work, tests, use_gemini):
    """Returns (passed, notes). Gemini only reviews once Claude has passed the fix."""
    prompt = (f"{text}\n\nTest results (run by a separate tester in a clean copy): {tests['summary']}\n\n"
              f"Files changed: {', '.join(work['files']) or '(none)'}\n\n"
              f"The fixer's own summary (not evidence): {work['message']}\n\n"
              f"The actual change:\n{work['diff'][:MAX_DIFF_CHARS] or '(no change)'}")
    try:
        first = ask_json(run_id, "reviewer", prompt, crew.REVIEW_SCHEMA, system=REVIEWER_SYSTEM,
                         model=SMART, max_tokens=800, task_id=task_id)
    except (ValueError, KeyError):
        return False, "Reviewer reply could not be read."
    notes = first.get("notes", "").strip()
    if first.get("verdict") != "pass":
        return False, notes
    if not use_gemini:
        return True, notes
    second = gemini.ask_json(run_id, "gemini-reviewer", prompt, crew.REVIEW_SCHEMA,
                             system=REVIEWER_SYSTEM, task_id=task_id)
    if second.get("verdict") != "pass":
        return False, f"Reviewers disagree. Gemini: {second.get('notes', '').strip()}"
    return True, f"{notes} Gemini agreed: {second.get('notes', '').strip()}"


# ---------- shipping a passed fix ----------

def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "fix"


def open_draft_pr(app, branch, base, title, body_path):
    """Push the branch and open a draft pull request, if the project has a GitHub remote and gh."""
    try:
        _git(app, "remote", "get-url", "origin")
    except RuntimeError:
        print("  No GitHub remote on the project, so no pull request; the fix waits on its branch.")
        return None
    if not shutil.which("gh"):
        print("  The GitHub CLI (gh) isn't installed, so no pull request; the fix waits on its branch.")
        return None
    _git(app, "push", "-q", "-u", "origin", branch)
    result = subprocess.run(["gh", "pr", "create", "--draft", "--base", base, "--head", branch,
                             "--title", title, "--body-file", str(body_path)],
                            cwd=app, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  Couldn't open the draft pull request: {result.stderr.strip()[:200]}")
        return None
    return result.stdout.strip().splitlines()[-1]


def ship(run_id, app, base, alert, work, tests, review_notes, caught, open_pr):
    """Commit the passed fix on its own branch and write the pull request description."""
    title = f"Fix {alert.get('title', 'production error')}"
    branch = f"fix/{_slug(alert.get('title', 'fix'))}-run{run_id}"
    taken = _git(app, "branch", "--list", f"{branch}*", "--format=%(refname:short)").split()
    if branch in taken:  # run numbers start again if board.db is deleted between rehearsals
        branch = f"{branch}-{len(taken) + 1}"
    _git(app, "checkout", "-q", "-b", branch)
    _commit(app, f"{title}\n\n{work['message']}\n\nFound, fixed, tested and reviewed by the SRE crew (run {run_id}).")

    impact = ", ".join(x for x in (
        f"{alert['count']} events" if alert.get("count") else "",
        f"{alert['users_affected']} customers affected" if alert.get("users_affected") else "",
        f"first seen {alert['first_seen']}" if alert.get("first_seen") else "") if x)
    lines = [f"## {title}", "",
             f"**Alert** ({alert.get('source', 'monitoring')}, {alert.get('environment', 'production')}): "
             f"{alert.get('message') or alert.get('title', '')}" + (f" {impact}." if impact else ""), "",
             f"**Root cause and fix** (fixer): {work['message']}", "",
             f"**Tests** (tester, in a clean copy): Passed {tests['summary'].removeprefix('PASSED: ')}", "",
             f"**Review**: {review_notes}", ""]
    if caught:
        lines += ["**Caught before you saw it:**"]
        lines += [f"- Attempt {c['attempt']}{' (planted on purpose, challenge mode)' if c['planted'] else ''}: "
                  f"sent back by the {c['by']}. {c['feedback']}" for c in caught]
        lines += [""]
    lines += [f"**Cost:** {dashboard._money(board.run_cost(run_id))} of a ${BUDGET_USD:.2f} budget (run {run_id}).", "",
              "Nothing has been merged. An engineer decides.", "",
              "<details><summary>The change</summary>", "", "```diff", work["diff"], "```", "", "</details>"]
    OUTPUT_DIR.mkdir(exist_ok=True)
    body_path = OUTPUT_DIR / f"pr_run{run_id}.md"
    body_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    url = open_draft_pr(app, branch, base, title, body_path) if open_pr else None
    _git(app, "checkout", "-q", base)  # the project goes back to how it was; the fix waits on its branch
    return branch, body_path, url


def _save_record(run_id, record):
    """What the spoken briefing needs to tell the story of this incident."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / f"incident_run{run_id}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


# ---------- main ----------

def main(alert, use_gemini=False, challenge=False, use_sparkles=False, open_pr=False, app_dir=None):
    alert = load_alert(alert)
    app = Path(app_dir or APP_DIR).expanduser()
    base = prepare(app)
    text = alert_text(alert)

    tags = [tag for tag, on in (("Challenge", challenge), ("Sparkles fixer", use_sparkles),
                                ("Gemini second review", use_gemini)) if on]
    title = crew._short_title(f"Incident: {alert.get('title', 'production error')}", 70)
    run_id = board.start_run(title + (" · " + " + ".join(tags) if tags else ""), budget_usd=BUDGET_USD)
    t_fix = board.add_task(run_id, "Fix the root cause", owner="sparkles-fixer" if use_sparkles else "fixer")
    t_test = board.add_task(run_id, "Run every test, hidden ones too", owner="tester")
    t_review = board.add_task(run_id, "Review the fix", owner="reviewer" + (" + gemini" if use_gemini else ""))
    record = {"alert": alert, "challenge": challenge, "gemini": use_gemini, "caught": [],
              "status": "running", "fix": "", "tests": "", "branch": None, "pr_url": None, "pr_file": None}
    _save_record(run_id, record)
    fixer = "Sparkles fixer" if use_sparkles else "Fixer"
    affected = f" {alert['users_affected']} customers affected." if alert.get("users_affected") else ""
    feed.post("Monitor", f"{alert.get('title', 'Production error')} in {alert.get('culprit') or 'production'}. "
              f"{alert.get('message', '')}{affected}".strip(), to=fixer, kind="alert", run_id=run_id)
    print(f"Run {run_id}: {alert.get('title')} in {app}")

    request = (f"{text}\n\nFix the root cause in this project. Make the smallest change that gives "
               "customers the expected behaviour. Don't edit the tests.")
    prompt, status, notes, work, tests = request, "needs_human", "", None, None
    try:
        for attempt in range(1, MAX_ATTEMPTS + (2 if challenge else 1)):
            crew._set(run_id, t_fix, status="running" if attempt == 1 else "revising", attempts=attempt)
            planted = challenge and attempt == 1
            print(f"  attempt {attempt}: " + ("planting a shortcut fix (challenge mode)" if planted else "fixer working..."))
            if planted:
                work = planted_shortcut(app)
            elif use_sparkles:
                work = sparkles_fix(run_id, t_fix, app, prompt)
            else:
                work = claude_fix(run_id, t_fix, app, prompt)
            label = "PLANTED SHORTCUT" if planted else "FIX"
            crew._set(run_id, t_fix, status="waiting_review",
                      result=f"{label}: {work['message']} (files: {', '.join(work['files']) or 'none'})")
            feed.post("Challenge mode" if planted else fixer,
                      ("A shortcut fix, planted on purpose to test the crew: " if planted else "")
                      + (work["message"] or "Changed " + ", ".join(work["files"])), to="Tester", kind="fix",
                      run_id=run_id)

            if not work["diff"]:
                by, feedback = "code check", "No files were changed."
                feed.post("Code check", feedback, to=fixer, kind="challenge", run_id=run_id)
            else:
                crew._set(run_id, t_test, status="running", attempts=attempt)
                tests = run_tests(app)
                crew._set(run_id, t_test, status="done" if tests["passed"] else "revising", result=tests["summary"])
                print(f"  tester: {tests['summary']}")
                if not tests["passed"]:
                    by, feedback = "tester", tests["feedback"]
                    feed.post("Tester", f"Sent back. {feedback}", to=fixer, kind="challenge", run_id=run_id)
                else:
                    feed.post("Tester", tests["summary"].replace("PASSED: ", "Passed: "), to="Reviewer",
                              kind="agree", run_id=run_id)
                    crew._set(run_id, t_review, status="running", attempts=attempt)
                    try:
                        passed, notes = review(run_id, t_review, text, work, tests, use_gemini)
                    except BudgetExceeded:
                        raise
                    except Exception as error:  # Gemini unreachable: not verified, so not passed
                        notes = f"Second review unavailable, so not verified: {error}"
                        feed.post("Gemini reviewer", notes, to="Crew", kind="challenge", run_id=run_id)
                        break
                    if passed:
                        status = "done"
                        crew._set(run_id, t_review, status="done", result=f"PASSED: {notes}")
                        claude_notes, _, gemini_notes = notes.partition(" Gemini agreed: ")
                        feed.post("Reviewer", f"Passed. {claude_notes}", to=fixer, kind="agree", run_id=run_id)
                        if gemini_notes:
                            feed.post("Gemini reviewer", f"Agreed. {gemini_notes}", to=fixer, kind="agree",
                                      run_id=run_id)
                        break
                    by, feedback = "reviewer", notes
                    crew._set(run_id, t_review, status="revising", result=f"SENT BACK: {notes}")
                    gemini_said = notes.startswith("Reviewers disagree.")
                    feed.post("Gemini reviewer" if gemini_said else "Reviewer",
                              f"Sent back. {notes.removeprefix('Reviewers disagree. Gemini: ')}",
                              to=fixer, kind="challenge", run_id=run_id)

            record["caught"].append({"attempt": attempt, "by": by, "feedback": feedback, "planted": planted})
            _save_record(run_id, record)
            print(f"  sent back by the {by}: {feedback}")
            crew._set(run_id, t_fix, review_notes=f"Sent back by the {by}: {feedback}")
            first = "A first attempt is" if planted else "Your previous change is"
            prompt = (f"{request}\n\n{first} already in the project. It was sent back by the {by}: "
                      f"{feedback}\nFix exactly that, and nothing else.")

        record["fix"], record["tests"] = (work or {}).get("message", ""), (tests or {}).get("summary", "")
        if status == "done":
            if record["caught"]:
                c = record["caught"][0]
                opening = "Planted mistake caught" if c["planted"] else "Caught"
                crew._set(run_id, t_fix, status="done",
                          review_notes=f"{opening}: the {c['by']} found {c['feedback']} Fixed and passed.")
            else:
                crew._set(run_id, t_fix, status="done", review_notes="Passed the tests and review first time.")
            branch, body_path, url = ship(run_id, app, base, alert, work, tests, notes, record["caught"], open_pr)
            record.update(branch=branch, pr_url=url, pr_file=str(body_path.relative_to(HERE)))
            crew._set(run_id, t_review, result=f"PASSED: {notes} Ready on branch {branch}"
                                               + (f", draft PR {url}" if url else "") + ".")
            feed.post("Crew", f"Fix ready for your review on branch {branch}"
                      + (f" (draft pull request: {url})" if url else "") + f". {tests['summary'].replace('PASSED: ', 'Passed ')} "
                      "Nothing was merged.", to="On-call engineer", kind="verdict", run_id=run_id)
        else:
            for task_id in (t_fix, t_test, t_review):
                if board._fetch("SELECT status FROM tasks WHERE id = ?", (task_id,))[0]["status"] != "done":
                    crew._set(run_id, task_id, status="needs_human")
            crew._set(run_id, t_review, review_notes=notes or record["caught"][-1]["feedback"])
            _git(app, "checkout", "-q", "-f", base)  # unverified work never stays in the project
            _git(app, "clean", "-fdq")
            feed.post("Crew", "Held for you: no fix passed every check, so nothing was committed. "
                      + (notes or record["caught"][-1]["feedback"]), to="On-call engineer", kind="verdict",
                      run_id=run_id)
        board.finish_run(run_id, status="completed" if status == "done" else "needs_human")
        record["status"] = "done" if status == "done" else "needs_human"
        _save_record(run_id, record)
        print(f"Run {run_id} {record['status'].replace('_', ' ')}."
              + (f" The fix is on branch {record['branch']} in {app}; PR text in {record['pr_file']}."
                 if status == "done" else " Held for a human: nothing was committed."))
    except BudgetExceeded as stop:
        print(f"Run {run_id} stopped by the budget cap. {stop}")
    except Exception:
        board.finish_run(run_id, status="failed")
        raise
    finally:
        dashboard.render(run_id)
        print(f"Crew cost: {dashboard._money(board.run_cost(run_id))}"
              + (" (the Sparkles agent runs on its own plan, not shown here)" if use_sparkles else ""))
    return run_id


if __name__ == "__main__":
    flags = {"--gemini", "--challenge", "--sparkles", "--pr", "--reset"}
    args = [a for a in sys.argv[1:] if a not in flags]
    try:
        if "--reset" in sys.argv[1:]:
            print(f"Fresh demo project in {reset_app()}")
            if not args:
                raise SystemExit(0)
        if len(args) != 1:
            raise SystemExit("Usage: uv run sre_crew.py <alert.json> [--challenge] [--gemini] [--sparkles] [--pr]\n"
                             "       uv run sre_crew.py --reset")
        main(args[0], use_gemini="--gemini" in sys.argv, challenge="--challenge" in sys.argv,
             use_sparkles="--sparkles" in sys.argv, open_pr="--pr" in sys.argv)
    except RuntimeError as problem:  # a clear message instead of a traceback
        raise SystemExit(f"Stopped: {problem}")

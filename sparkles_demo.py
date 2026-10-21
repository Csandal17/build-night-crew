"""Sparkles writes the code, the crew checks it.

One request, start to finish:
1. A Sparkles coding agent makes the change in a repository folder.
2. Code checks something actually changed.
3. Claude reviews the real diff (and Gemini too, with --gemini; both must pass).
4. If a reviewer asks for changes, Sparkles gets one revision with exactly that feedback.
5. Done, or held for a human. Everything shows on the dashboard.

Run:  uv run sparkles_demo.py ~/sparkles-test "Add a function greet(name) to hello.py that returns 'Hello, <name>!'"
      uv run sparkles_demo.py ~/sparkles-test "..." --gemini

The change is left in the folder for you to look at (git status, git diff). Nothing is
committed or pushed. With a GitHub repo and `sparkles login`, `sparkles pr create`
would turn a passed change into a draft pull request.
"""

import sys

import board
import crew
import dashboard
import gemini
import sparkles_agent
from llm import SMART, BudgetExceeded, ask_json

BUDGET_USD = 0.25
MAX_ATTEMPTS = 2  # first try plus one revision

CODE_REVIEWER_SYSTEM = (
    "You review a coding agent's change before it becomes a pull request. Judge only the "
    "change you are shown against the request: does it do what was asked, without unrelated "
    "changes, obvious bugs or secrets? The agent's own summary is not evidence; the change is. "
    "Pass good work without nitpicking style. If you ask for a revision, say exactly what to "
    "fix in one or two sentences."
)


def review_prompt(request, work):
    return (
        f"Request: {request}\n\n"
        f"Files changed: {', '.join(work['files']) or '(none)'}\n\n"
        f"The agent's own summary (not evidence): {work['message']}\n\n"
        f"The actual change:\n{work['diff'] or '(no change)'}"
    )


def review(run_id, task_id, request, work, use_gemini):
    """Returns (passed, notes). Gemini only reviews once Claude has passed the change."""
    try:
        first = ask_json(run_id, "reviewer", review_prompt(request, work), crew.REVIEW_SCHEMA,
                         system=CODE_REVIEWER_SYSTEM, model=SMART, max_tokens=1000, task_id=task_id)
    except (ValueError, KeyError):
        return False, "Reviewer reply could not be read."
    notes = first.get("notes", "").strip()
    if first.get("verdict") != "pass":
        return False, notes
    if not use_gemini:
        return True, notes
    second = gemini.ask_json(run_id, "gemini-reviewer", review_prompt(request, work),
                             crew.REVIEW_SCHEMA, system=CODE_REVIEWER_SYSTEM, task_id=task_id)
    if second.get("verdict") != "pass":
        return False, f"Reviewers disagree. Gemini: {second.get('notes', '').strip()}"
    return True, f"{notes} Gemini agreed: {second.get('notes', '').strip()}"


def main(repo, request, use_gemini=False):
    title = crew._short_title(f"Sparkles: {request}")
    run_id = board.start_run(title + (" · Gemini second review" if use_gemini else ""),
                             budget_usd=BUDGET_USD)
    task_id = board.add_task(run_id, crew._short_title(request, 50), owner="sparkles-worker")
    dashboard.render(run_id)
    print(f"Run {run_id}: Sparkles writes the code in {repo}, the crew reviews it.")

    prompt, status, notes = request, "needs_human", ""
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            crew._set(run_id, task_id, status="running" if attempt == 1 else "revising", attempts=attempt)
            print(f"  attempt {attempt}: Sparkles agent working...")
            work = sparkles_agent.run(prompt, repo, run_id=run_id, task_id=task_id)
            crew._set(run_id, task_id, result=f"CHANGED: {work['message']} "
                                              f"(files: {', '.join(work['files']) or 'none'})")

            if not work["diff"]:
                passed, notes = False, "Code check: no files were changed."
            else:
                crew._set(run_id, task_id, status="waiting_review")
                try:
                    passed, notes = review(run_id, task_id, request, work, use_gemini)
                except BudgetExceeded:
                    raise
                except Exception as error:  # Gemini unreachable: not verified, so not passed
                    notes = f"Second review unavailable, so not verified: {error}"
                    break

            if passed:
                status = "done"
                crew._set(run_id, task_id, status="done", review_notes=f"Passed: {notes}")
                break
            print(f"  sent back: {notes}")
            crew._set(run_id, task_id, review_notes=f"Sent back: {notes}")
            prompt = (f"{request}\n\nA reviewer looked at your change and asked for this: {notes}\n"
                      "Fix exactly that, and nothing else.")

        if status != "done":
            crew._set(run_id, task_id, status="needs_human", review_notes=notes)
        board.finish_run(run_id, status="completed" if status == "done" else "needs_human")
        print(f"Run {run_id} {status.replace('_', ' ')}. {notes}")
        if status == "done":
            print(f"The change is in {repo}, uncommitted. Look at it with: git -C {repo} diff")
    except BudgetExceeded as stop:
        print(f"Run {run_id} stopped by the budget cap. {stop}")
    except Exception:
        board.finish_run(run_id, status="failed")
        raise
    finally:
        dashboard.render(run_id)
        print(f"Crew cost: {dashboard._money(board.run_cost(run_id))} "
              "(the Sparkles agent runs on your own Claude Code plan, not shown here)")
    return run_id


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--gemini"]
    if len(args) != 2:
        raise SystemExit('Usage: uv run sparkles_demo.py <repo folder> "<request>" [--gemini]')
    try:
        main(args[0], args[1], use_gemini="--gemini" in sys.argv[1:])
    except RuntimeError as problem:  # a clear message instead of a traceback
        raise SystemExit(f"Stopped: {problem}")

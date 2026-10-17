"""The crew: a coordinator splits one goal into tasks, workers do them,
and a reviewer checks each one before it counts.

- Workers may only use the source pack in sources/, never memory.
- Every agent answers through a fixed schema (tool use), so replies are valid JSON.
- Code checks every quote word for word before the reviewer sees it.
- One revision per task; anything still failing is flagged for a human.
- The final brief is assembled by code from reviewed results only.

Run: uv run crew.py            (uses DEFAULT_GOAL)
     uv run crew.py "a goal"   (any goal the source pack can answer)
"""

import json
import sys
from pathlib import Path

import board
import dashboard
from llm import FAST, SMART, BudgetExceeded, ask_json

HERE = Path(__file__).parent
SOURCES_DIR = HERE / "sources"
OUTPUT_DIR = HERE / "output"

BUDGET_USD = 0.25
MAX_TASKS = 3
MAX_ATTEMPTS = 2  # first try plus one revision

COORDINATOR_MODEL = FAST
WORKER_MODEL = SMART
REVIEWER_MODEL = SMART

VERDICTS = {"supported", "contradicted", "inconclusive"}

# The shape each agent must answer in. Claude fills these in through tool use.
PLAN_SCHEMA = {
    "type": "object",
    "properties": {"tasks": {"type": "array", "maxItems": 3, "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "short claim or task name"},
            "instructions": {"type": "string", "description": "what the worker must do"},
            "acceptance": {"type": "array", "maxItems": 3, "items": {"type": "string"},
                           "description": "checkable criteria"},
        },
        "required": ["title", "instructions", "acceptance"],
    }}},
    "required": ["tasks"],
}
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": sorted(VERDICTS)},
        "summary": {"type": "string", "description": "one or two plain-English sentences"},
        "evidence": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "source id, e.g. S1"},
                "quote": {"type": "string", "description": "exact words copied from that source"},
            },
            "required": ["source", "quote"],
        }},
    },
    "required": ["verdict", "summary", "evidence"],
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "revise"]},
        "notes": {"type": "string", "description": "one or two sentences; if revise, say exactly what to fix"},
    },
    "required": ["verdict", "notes"],
}

DEFAULT_GOAL = (
    "Check these three supplement claims against the source pack and produce "
    "a one-page evidence brief: "
    "(1) Adults in the UK should consider taking a daily vitamin D supplement in autumn and winter. "
    "(2) Taking more than 1,000mg of vitamin C a day has no side effects. "
    "(3) Zinc supplements help prevent colds."
)

WORKER_SYSTEM = (
    "You are a careful evidence checker. Use only the numbered sources you are given, "
    "never outside knowledge. A source only counts if it is about the same thing as the claim. "
    "If the sources do not directly address the claim, the verdict is inconclusive. "
    "Never give personal medical advice."
)

REVIEWER_SYSTEM = (
    "You review another agent's work before it is published. Be strict about over-claiming: "
    "a quote only supports a verdict if it is about exactly the same thing as the claim. "
    "Pass good work without nitpicking. "
    "The verdict scale (supported, contradicted, inconclusive) and the evidence format "
    "(source id plus exact quote) are fixed by the system. Never ask for other labels, "
    "quote locations or page numbers; judge only whether the verdict and the quotes are "
    "right for the claim."
)


# ---------- sources ----------

def load_sources():
    """Read sources/*.txt into {"S1": {...}, "S2": {...}} in filename order."""
    sources = {}
    for n, path in enumerate(sorted(SOURCES_DIR.glob("*.txt")), start=1):
        header, _, text = path.read_text(encoding="utf-8").partition("\n---\n")
        meta = dict(line.split(": ", 1) for line in header.splitlines() if ": " in line)
        sources[f"S{n}"] = {**meta, "text": text.strip()}
    if not sources:
        raise SystemExit("No sources found. Add .txt files to the sources/ folder.")
    return sources


def _format_sources(sources, ids=None):
    ids = ids or sources.keys()
    return "\n\n".join(
        f"[{sid}] {sources[sid].get('publisher', '')}: {sources[sid].get('title', '')}\n"
        f"{sources[sid]['text']}"
        for sid in ids
    )


def _normalise(text):
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    return " ".join(text.lower().split())


# ---------- small helpers ----------

def _set(run_id, task_id, **fields):
    """Update a task and refresh the dashboard straight away."""
    board.update_task(task_id, **fields)
    dashboard.render(run_id)


def _bullets(items):
    return "\n".join(f"- {item}" for item in items)


# ---------- agents ----------

def coordinator(run_id, goal, sources):
    titles = "\n".join(f"[{sid}] {s.get('publisher', '')}: {s.get('title', '')}"
                       for sid, s in sources.items())
    prompt = (
        f"Split this goal into at most {MAX_TASKS} independent tasks for worker agents. "
        "Each task must be checkable on its own. "
        "Keep each task short: a title under 10 words, instructions in one sentence, "
        "at most 3 acceptance criteria. "
        "Workers answer with a verdict of supported, contradicted or inconclusive plus exact quotes; "
        "acceptance criteria must not require other verdict labels or quote locations.\n\n"
        f"Goal: {goal}\n\nSources the workers will have (titles only):\n{titles}"
    )
    plan = ask_json(run_id, "coordinator", prompt, PLAN_SCHEMA,
                    model=COORDINATOR_MODEL, max_tokens=2500)
    return plan["tasks"][:MAX_TASKS]


def worker(run_id, task_id, task, sources, feedback=None, previous=None):
    prompt = (
        f"Task: {task['title']}\nInstructions: {task['instructions']}\n"
        f"Acceptance criteria:\n{_bullets(task['acceptance'])}\n\n"
        f"Sources:\n{_format_sources(sources)}"
    )
    if feedback:
        prompt += (f"\n\nYour previous answer:\n{json.dumps(previous)}\n\n"
                   f"A reviewer asked for changes: {feedback}\nFix exactly that.")
    return ask_json(run_id, f"worker-{task_id}", prompt, ANSWER_SCHEMA, system=WORKER_SYSTEM,
                    model=WORKER_MODEL, max_tokens=1200, task_id=task_id)


def code_check(answer, sources):
    """Rules that don't need a model. Returns a list of problems (empty means OK)."""
    problems = []
    if answer.get("verdict") not in VERDICTS:
        problems.append(f"Verdict must be one of {sorted(VERDICTS)}.")
    evidence = answer.get("evidence") or []
    if answer.get("verdict") in {"supported", "contradicted"} and not evidence:
        problems.append("A supported or contradicted verdict needs at least one quote.")
    for item in evidence:
        sid, quote = item.get("source"), item.get("quote", "")
        if sid not in sources:
            problems.append(f"{sid} is not in the source pack.")
        elif _normalise(quote) not in _normalise(sources[sid]["text"]):
            problems.append(f"The quote attributed to {sid} does not appear in it word for word.")
    return problems


def reviewer(run_id, task_id, task, answer, sources):
    cited = sorted({e["source"] for e in answer.get("evidence", []) if e.get("source") in sources})
    prompt = (
        f"Task: {task['title']}\nAcceptance criteria:\n{_bullets(task['acceptance'])}\n\n"
        f"Worker's answer:\n{json.dumps(answer, indent=2)}\n\n"
        f"Text of the sources it cited:\n{_format_sources(sources, cited) if cited else '(none)'}\n\n"
        "Code has already confirmed every quote appears word for word in its source."
    )
    return ask_json(run_id, "reviewer", prompt, REVIEW_SCHEMA, system=REVIEWER_SYSTEM,
                    model=REVIEWER_MODEL, max_tokens=1000, task_id=task_id)


# ---------- one task, start to finish ----------

def run_task(run_id, task, sources):
    task_id = board.add_task(run_id, task["title"], owner="unassigned")
    _set(run_id, task_id, owner=f"worker-{task_id}")
    answer, feedback, caught = None, None, []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        _set(run_id, task_id, status="running" if attempt == 1 else "revising", attempts=attempt)
        try:
            answer = worker(run_id, task_id, task, sources, feedback, answer)
        except (ValueError, KeyError) as bad_reply:
            feedback = f"Your answer could not be used ({bad_reply}) Try again and keep it short."
            caught.append(feedback)
            _set(run_id, task_id, review_notes=f"Sent back: {feedback}")
            continue

        result = f"{answer.get('verdict', '?').upper()}: {answer.get('summary', '')}"
        problems = code_check(answer, sources)
        if problems:
            feedback = "Code check: " + " ".join(problems)
            caught.append(feedback)
            _set(run_id, task_id, result=result, review_notes=f"Sent back: {feedback}")
            continue

        _set(run_id, task_id, status="waiting_review", result=result)
        try:
            review = reviewer(run_id, task_id, task, answer, sources)
        except (ValueError, KeyError):
            review = {"verdict": "revise", "notes": "Reviewer reply could not be read."}
        notes = review.get("notes", "").strip()
        if review.get("verdict") == "pass":
            summary = (f"Caught: {caught[-1]} Fixed and passed." if caught
                       else f"Passed: {notes}")
            _set(run_id, task_id, status="done", review_notes=summary)
            return {"task": task, "answer": answer, "status": "done",
                    "attempts": attempt, "review": notes, "caught": caught}
        feedback = notes
        caught.append(notes)
        _set(run_id, task_id, review_notes=f"Sent back: {notes}")

    _set(run_id, task_id, status="needs_human")
    return {"task": task, "answer": answer, "status": "needs_human",
            "attempts": MAX_ATTEMPTS, "review": feedback, "caught": caught}


# ---------- the brief, assembled by code ----------

def write_brief(run_id, goal, results, sources):
    OUTPUT_DIR.mkdir(exist_ok=True)
    lines = [
        f"# Evidence brief · run {run_id}", "",
        f"**Goal:** {goal}", "",
        "_General information from public sources, not personal medical advice._", "",
        "| Claim | Verdict | Review |", "|---|---|---|",
    ]
    for r in results:
        verdict = (r["answer"] or {}).get("verdict", "unverified")
        status = "passed" if r["status"] == "done" else "**needs a human**"
        lines.append(f"| {r['task']['title']} | {verdict} | {status}, attempt {r['attempts']} |")

    for n, r in enumerate(results, start=1):
        answer = r["answer"] or {}
        lines += ["", f"## {n}. {r['task']['title']}", ""]
        if r["status"] != "done":
            lines.append(f"**Not verified by the crew.** Last reviewer note: {r['review']}")
            continue
        lines += [f"**{answer['verdict'].capitalize()}.** {answer.get('summary', '')}", ""]
        for e in answer.get("evidence", []):
            s = sources[e["source"]]
            lines.append(f"> \"{e['quote']}\"  \n> [{e['source']}] "
                         f"[{s.get('publisher', '')}: {s.get('title', '')}]({s.get('url', '')})")
        if r["caught"]:
            lines += ["", f"_Caught in review: {r['caught'][-1]} "
                          f"Fixed on attempt {r['attempts']}._"]
        else:
            lines += ["", f"_Reviewer: {r['review']}_"]

    lines += ["", "---",
              f"Crew cost: {dashboard._money(board.run_cost(run_id))} "
              f"of a {dashboard._money(BUDGET_USD)} budget."]
    path = OUTPUT_DIR / f"brief_run{run_id}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------- main ----------

def main(goal):
    sources = load_sources()
    run_id = board.start_run(goal, budget_usd=BUDGET_USD)
    dashboard.render(run_id)
    print(f"Run {run_id} started with {len(sources)} sources and a "
          f"{dashboard._money(BUDGET_USD)} budget. Watch the dashboard.")
    try:
        tasks = coordinator(run_id, goal, sources)
        print(f"Coordinator made {len(tasks)} tasks.")
        results = []
        for task in tasks:
            r = run_task(run_id, task, sources)
            print(f"  {r['status']:<12} attempt {r['attempts']}  {task['title']}")
            results.append(r)
        status = "completed" if all(r["status"] == "done" for r in results) else "needs_human"
        board.finish_run(run_id, status=status)
        path = write_brief(run_id, goal, results, sources)
        print(f"Run {run_id} {status}. Brief: {path.relative_to(HERE)}")
    except BudgetExceeded as stop:
        print(f"Run {run_id} stopped by the budget cap. {stop}")
    except Exception:
        board.finish_run(run_id, status="failed")
        raise
    finally:
        dashboard.render(run_id)
        print(f"Total cost: {dashboard._money(board.run_cost(run_id))}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_GOAL)
    
"""The general crew: any goal, split into tasks, worked on in parallel, checked before it counts.

crew.py checks a few claims against a fixed source pack. This pipeline handles a goal of any
kind (today: research a topic and write a sourced brief) and is built so more task types can
be added:

  coordinator   splits the goal into tasks of two types and says which depend on which
  researchers   (parallel) propose web pages; CODE fetches and saves them to the run's library
  writers       (parallel, once their sources are ready) write one section each, citing quotes
  gate          plain code: pages really saved; every quote really in a source (gates.py)
  reviewers     Claude, then optionally Gemini, judge the section and the strength of its sources
  messages      agents talk on the board: a writer can ask for a missing source (which starts a
                research task), reviewers challenge, writers reply. The dashboard shows the feed.

Money: every call, parallel or not, reserves its worst-case cost before it starts (llm.reserve),
so the run can never overshoot its budget. Code assembles the final brief from reviewed work only.

Run:  uv run pipeline.py                      (default goal, hand-written NHS pack as base library)
      uv run pipeline.py "your goal"          (needs web pages: see --web-search)
      uv run pipeline.py "goal" --gemini      (Gemini must also pass every section)
      uv run pipeline.py "goal" --web-search  (researchers use Claude's web search tool)
      uv run pipeline.py "goal" --pack        (start with the sources/ pack in the library)
      uv run pipeline.py "goal" --parallel 1  (one task at a time)
"""

import json
import os
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import board
import crew
import dashboard
import gates
import gemini
import workspace
from llm import FAST, SMART, BudgetExceeded, ask_json, ask_search

HERE = Path(__file__).parent
OUTPUT_DIR = HERE / "output"

BUDGET_USD = 0.75
MAX_TASKS = 6
MAX_PARALLEL = 3
MAX_ATTEMPTS = 2           # first try plus one revision
MAX_SOURCE_REQUESTS = 2    # how many times writers may start extra research in one run
MAX_URLS = 3               # pages a researcher may save per attempt

COORDINATOR_MODEL = FAST
RESEARCHER_MODEL = FAST
WRITER_MODEL = SMART
REVIEWER_MODEL = SMART

WEB_SEARCH = os.getenv("CREW_WEB_SEARCH") == "1"   # --web-search
WITH_PACK = os.getenv("CREW_PACK") == "1"          # --pack

DEFAULT_GOAL = (
    "Write a short evidence brief for UK adults on vitamin D supplements: who should consider "
    "taking one and when, and what the safe upper limit is."
)

PLAN_SCHEMA = {
    "type": "object",
    "properties": {"tasks": {"type": "array", "maxItems": MAX_TASKS, "items": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "short unique id such as r1 or w1"},
            "type": {"type": "string", "enum": ["research", "write"]},
            "title": {"type": "string", "description": "under 10 words"},
            "instructions": {"type": "string", "description": "one sentence"},
            "acceptance": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
            "depends_on": {"type": "array", "items": {"type": "string"},
                           "description": "keys of the research tasks a write task needs"},
        },
        "required": ["key", "type", "title", "instructions", "acceptance", "depends_on"],
    }}},
    "required": ["tasks"],
}
RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {"urls": {"type": "array", "maxItems": MAX_URLS, "items": {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "full https URL of a real public page"},
                       "why": {"type": "string"}},
        "required": ["url", "why"],
    }}},
    "required": ["urls"],
}
SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ready", "need_source"]},
        "text": {"type": "string", "description": "the section, a few plain sentences"},
        "evidence": {"type": "array", "items": {
            "type": "object",
            "properties": {"source": {"type": "string", "description": "source id, e.g. S1"},
                           "quote": {"type": "string", "description": "exact words copied from that source"}},
            "required": ["source", "quote"],
        }},
        "missing": {"type": "string",
                    "description": "only if status is need_source: what source is missing"},
        "note": {"type": "object", "description": "optional short message to a teammate",
                 "properties": {"to": {"type": "string"}, "text": {"type": "string"}}},
    },
    "required": ["status", "text", "evidence"],
}

RESEARCHER_SYSTEM = (
    "You are a careful researcher. Choose specific public web pages most likely to contain "
    "the evidence asked for. Prefer authoritative sources: government and health bodies, "
    "standards bodies, official documentation, peer-reviewed work. Only give URLs you are "
    "confident exist. Code will fetch each page, so an invented URL simply fails."
)
WRITER_SYSTEM = (
    "You write one section of an evidence brief, using only the numbered sources you are given, "
    "never outside knowledge. Every claim in your text must be supported by a quote you copy "
    "exactly from a source. If the sources do not contain what the task needs, set status to "
    "need_source and say precisely what is missing instead of guessing. Never give personal advice."
)
REVIEWER_SYSTEM = (
    "You review one section of a brief before it is published. Be strict about over-claiming: "
    "every statement must be supported by the quotes and sources given. Also judge the "
    "authority of the sources: an official or primary source is stronger than a blog or "
    "a company page, and a claim resting only on a weak source should be sent back with a "
    "request for a better one. Pass good work without nitpicking style. If you ask for a "
    "revision, say exactly what to fix in one or two sentences."
)


def _clip(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",.:;") + "…"


_print_lock = threading.Lock()


def log(message):
    with _print_lock:
        print(message, flush=True)


# ---------- the plan ----------

def normalise_plan(raw, max_tasks=MAX_TASKS):
    """Clean the coordinator's plan: unique keys, valid types and dependencies, no loops.
    Raises ValueError if there is nothing to run."""
    tasks, seen = [], set()
    for item in list(raw)[:max_tasks]:
        key = str(item.get("key", "")).strip() or f"t{len(tasks) + 1}"
        if key in seen:
            key = f"{key}-{len(tasks) + 1}"
        seen.add(key)
        kind = item.get("type") if item.get("type") in ("research", "write") else "write"
        tasks.append({
            "key": key, "type": kind,
            "title": _clip(item.get("title") or key, 80),
            "instructions": str(item.get("instructions", "")).strip(),
            "acceptance": [str(a) for a in (item.get("acceptance") or [])][:3],
            "depends_on": [str(d) for d in (item.get("depends_on") or [])],
        })
    if not tasks:
        raise ValueError("The plan has no tasks.")
    if not any(t["type"] == "write" for t in tasks):
        raise ValueError("The plan has no write task, so there would be no brief.")
    keys = {t["key"] for t in tasks}
    for t in tasks:
        deps = [d for d in t["depends_on"] if d in keys and d != t["key"]]
        t["depends_on"] = [] if t["type"] == "research" else list(dict.fromkeys(deps))
    # Anything still waiting on something after every possible task has run is in a loop.
    resolved = set()
    while True:
        ready = [t["key"] for t in tasks
                 if t["key"] not in resolved and all(d in resolved for d in t["depends_on"])]
        if not ready:
            break
        resolved.update(ready)
    for t in tasks:
        if t["key"] not in resolved:
            t["depends_on"] = []
    return tasks


# ---------- shared state for one run ----------

class Run:
    """Everything the agents of one run share. Worker threads never change a node's status;
    only the scheduler loop does, so the plan can't be corrupted by parallel work."""

    def __init__(self, run_id, goal, *, use_gemini, web_search, max_parallel, base_ids):
        self.run_id, self.goal = run_id, goal
        self.use_gemini, self.web_search = use_gemini, web_search
        self.max_parallel, self.base_ids = max_parallel, list(base_ids)
        self.nodes = []
        self.requests_made = 0
        self.stopped = False                     # set when the budget stops the run
        self._counters = {"research": 0, "write": 0}

    def node(self, key):
        return next(n for n in self.nodes if n["key"] == key)

    def add_node(self, spec):
        kind = spec["type"]
        self._counters[kind] += 1
        agent = f"{'researcher' if kind == 'research' else 'writer'}-{self._counters[kind]}"
        db_id = board.add_task(self.run_id, spec["title"], owner=agent, task_type=kind)
        node = {"key": spec["key"], "spec": spec, "db_id": db_id, "agent": agent,
                "deps": list(spec["depends_on"]), "status": "pending", "result": None,
                "attempts": 0, "answer": None, "feedback": None, "caught": []}
        self.nodes.append(node)
        return node

    def sync_deps(self, node):
        ids = [self.node(k)["db_id"] for k in node["deps"]]
        board.update_task(node["db_id"], depends_on=ids,
                          status="blocked" if node["deps"] else "queued")

    def set(self, node, **fields):
        board.update_task(node["db_id"], **fields)
        dashboard.render(self.run_id)

    def post(self, sender, recipient, kind, text, node=None):
        board.post_message(self.run_id, sender, recipient, kind, text,
                           task_id=node["db_id"] if node else None)
        dashboard.render(self.run_id)

    def read_inbox(self, agent):
        """Unread messages for this agent as prompt text ('' if none). Marks them read."""
        messages = board.unread_messages(self.run_id, agent)
        if not messages:
            return ""
        board.mark_read(agent, [m["id"] for m in messages])
        lines = "\n".join(f"- from {m['sender']} ({m['kind']}): {m['text']}" for m in messages)
        return f"Messages from your teammates:\n{lines}"

    def visible_sources(self, node):
        """The sources a writer is given: the base pack plus whatever its research tasks saved."""
        library = workspace.load_library(self.run_id)
        ids = set(self.base_ids)
        for key in node["deps"]:
            ids.update(((self.node(key)["result"]) or {}).get("sources", []))
        return {sid: library[sid] for sid in sorted(ids, key=lambda s: int(s[1:])) if sid in library}

    def dependents(self, node):
        return [n for n in self.nodes if node["key"] in n["deps"]]


def _format_sources(sources):
    return "\n\n".join(
        f"[{sid}] {s.get('publisher', '')}: {s.get('title', '')} ({s.get('url', '')})\n{s['text']}"
        for sid, s in sources.items()) or "(none)"


# ---------- coordinator ----------

def coordinator(run):
    pack = ""
    if run.base_ids:
        library = workspace.load_library(run.run_id)
        titles = "; ".join(f"{sid} {library[sid].get('title', '')}" for sid in run.base_ids)
        pack = (f"\n\nA hand-written pack of sources is already in the library ({titles}). "
                "Write tasks may use it directly with no research task.")
    prompt = (
        f"You direct a small crew. Split this goal into at most {MAX_TASKS} tasks of two types.\n"
        "- research: find authoritative web pages on ONE specific sub-question. No dependencies. "
        "Research tasks run in parallel, so keep them independent.\n"
        "- write: write ONE section of the final brief from the sources found by the research "
        "tasks it depends on. List their keys in depends_on.\n"
        "Include at least one write task. Give every task a short unique key (r1, r2, w1...), a "
        "title under 10 words, one sentence of instructions and at most 3 acceptance criteria. "
        f"Acceptance criteria must be checkable from sources.{pack}\n\nGoal: {run.goal}"
    )
    last_problem = None
    for _ in range(2):
        try:
            plan = ask_json(run.run_id, "coordinator", prompt, PLAN_SCHEMA,
                            model=COORDINATOR_MODEL, max_tokens=3000)
            return normalise_plan(plan["tasks"])
        except (ValueError, KeyError) as problem:
            last_problem = problem
            prompt += f"\n\nYour last plan could not be used ({problem}). Try again."
    raise ValueError(f"The coordinator could not make a usable plan: {last_problem}")


# ---------- researcher ----------

def _research_prompt(run, node, feedback, inbox):
    spec = node["spec"]
    library = workspace.load_library(run.run_id)
    have = "\n".join(f"- {sid}: {s.get('title', '')} ({s.get('url', '')})"
                     for sid, s in library.items()) or "(nothing yet)"
    prompt = (f"Overall goal: {run.goal}\n\nYour task: {spec['title']}\n"
              f"Instructions: {spec['instructions']}\n"
              f"Already in the library (do not repeat these):\n{have}")
    if inbox:
        prompt += f"\n\n{inbox}"
    if feedback:
        prompt += f"\n\nYour last attempt failed: {feedback}"
    return prompt


def find_pages(run, node, feedback, inbox):
    """Ask the researcher for pages. Returns [{"url", "title"}]; code fetches them afterwards."""
    prompt = _research_prompt(run, node, feedback, inbox)
    if run.web_search:
        try:
            found = ask_search(run.run_id, node["agent"], prompt, system=RESEARCHER_SYSTEM,
                               model=RESEARCHER_MODEL, task_id=node["db_id"])
            if found["pages"]:
                return found["pages"][:MAX_URLS * 2]   # a few spares, since some will fail
        except BudgetExceeded:
            raise
        except Exception as problem:
            run.post(node["agent"], "all", "say",
                     f"Web search isn't available ({type(problem).__name__}), so I'll propose "
                     "pages from what I know.", node)
    answer = ask_json(run.run_id, node["agent"],
                      prompt + "\n\nAnswer only by calling the submit tool with the pages.",
                      RESEARCH_SCHEMA, system=RESEARCHER_SYSTEM, model=RESEARCHER_MODEL,
                      max_tokens=800, task_id=node["db_id"])
    return [{"url": u["url"], "title": u.get("why", "")} for u in answer["urls"]][:MAX_URLS]


def run_research(run, node):
    feedback = None
    while node["attempts"] < MAX_ATTEMPTS:
        node["attempts"] += 1
        attempt = node["attempts"]
        run.set(node, status="running" if attempt == 1 else "revising", attempts=attempt)
        try:
            pages = find_pages(run, node, feedback, run.read_inbox(node["agent"]))
        except (ValueError, KeyError) as bad:
            feedback = f"Your answer could not be used ({bad}) Try again and keep it short."
            run.set(node, review_notes=f"Sent back: {feedback}")
            continue
        saved, errors = [], []
        for page in pages:
            if len(saved) >= MAX_URLS:
                break
            try:
                sid = workspace.save_source(run.run_id, workspace.fetch_page(page["url"]))
            except workspace.FetchError as problem:
                errors.append(str(problem))
                continue
            if sid not in saved:
                saved.append(sid)
        problems = gates.GATES["research"](saved, errors)
        if problems:
            feedback = f"{problems[0]} Propose different pages."
            run.set(node, review_notes=f"Sent back: {_clip(feedback, 280)}")
            continue
        library = workspace.load_library(run.run_id)
        filed = ", ".join(f"{sid} ({_clip(library[sid].get('title', ''), 50)})" for sid in saved)
        for dependent in run.dependents(node) or []:
            run.post(node["agent"], dependent["agent"], "offer", f"Filed {filed}.", node)
        if not run.dependents(node):
            run.post(node["agent"], "all", "offer", f"Filed {filed}.", node)
        return {"status": "done", "sources": saved, "summary": f"Filed {filed}"}
    run.post(node["agent"], "coordinator", "escalate",
             f"I couldn't fetch a usable source for '{node['spec']['title']}'. {feedback or ''}", node)
    return {"status": "needs_human", "sources": [], "review": feedback or "No source found."}


# ---------- writer and reviewers ----------

def _section_prompt(run, node, visible, inbox):
    spec = node["spec"]
    prompt = (f"Overall goal: {run.goal}\n\nYour section: {spec['title']}\n"
              f"Instructions: {spec['instructions']}\n"
              f"Acceptance criteria:\n" + "\n".join(f"- {a}" for a in spec["acceptance"]) +
              f"\n\nSources:\n{_format_sources(visible)}")
    if inbox:
        prompt += f"\n\n{inbox}"
    if node["feedback"]:
        prompt += (f"\n\nYour previous answer:\n{json.dumps(node['answer'])}\n\n"
                   f"A reviewer asked for changes: {node['feedback']}\nFix exactly that.")
    return prompt


def _review_prompt(run, node, answer, visible):
    spec = node["spec"]
    cited = sorted({e["source"] for e in answer.get("evidence", []) if e.get("source") in visible})
    return (f"Section: {spec['title']}\nInstructions: {spec['instructions']}\n"
            f"Acceptance criteria:\n" + "\n".join(f"- {a}" for a in spec["acceptance"]) +
            f"\n\nThe writer's section:\n{json.dumps(answer, indent=2)}\n\n"
            f"Sources it cited (check their authority too):\n"
            f"{_format_sources({sid: visible[sid] for sid in cited})}\n\n"
            "Code has already confirmed every quote appears word for word in its source.")


def review_section(run, node, answer, visible):
    return ask_json(run.run_id, "reviewer", _review_prompt(run, node, answer, visible),
                    crew.REVIEW_SCHEMA, system=REVIEWER_SYSTEM, model=REVIEWER_MODEL,
                    max_tokens=1000, task_id=node["db_id"])


def second_review(run, node, answer, visible):
    return gemini.ask_json(run.run_id, "gemini-reviewer", _review_prompt(run, node, answer, visible),
                           crew.REVIEW_SCHEMA, system=REVIEWER_SYSTEM, task_id=node["db_id"])


def run_write(run, node):
    agent, spec = node["agent"], node["spec"]
    while node["attempts"] < MAX_ATTEMPTS:
        node["attempts"] += 1
        attempt = node["attempts"]
        run.set(node, status="running" if not node["feedback"] else "revising", attempts=attempt)
        visible = run.visible_sources(node)
        try:
            answer = ask_json(run.run_id, agent,
                              _section_prompt(run, node, visible, run.read_inbox(agent)),
                              SECTION_SCHEMA, system=WRITER_SYSTEM, model=WRITER_MODEL,
                              max_tokens=1500, task_id=node["db_id"])
        except (ValueError, KeyError) as bad:
            node["feedback"] = f"Your answer could not be used ({bad}) Try again and keep it short."
            node["caught"].append(node["feedback"])
            run.set(node, review_notes=f"Sent back: {_clip(node['feedback'], 280)}")
            continue

        note = answer.get("note") or {}
        if note.get("text"):
            run.post(agent, note.get("to") or "all", "say", note["text"], node)

        if answer.get("status") == "need_source":
            node["attempts"] -= 1            # asking for a source is not a failed attempt
            board.update_task(node["db_id"], attempts=node["attempts"])
            return {"status": "need_source",
                    "missing": answer.get("missing") or answer.get("text") or spec["title"]}

        problems = gates.GATES["write"](answer, visible)
        if problems:
            node["feedback"] = "Code check: " + " ".join(problems)
            node["caught"].append(node["feedback"])
            run.set(node, result=_clip(answer.get("text", ""), 300),
                    review_notes=f"Sent back: {_clip(node['feedback'], 280)}")
            continue

        node["answer"] = answer
        run.set(node, status="waiting_review", result=_clip(answer["text"], 300))
        try:
            review = review_section(run, node, answer, visible)
        except (ValueError, KeyError):
            review = {"verdict": "revise", "notes": "Reviewer reply could not be read."}
        notes = (review.get("notes") or "").strip()
        if review.get("verdict") == "pass" and run.use_gemini:
            run.set(node, review_notes="First review passed. Asking Gemini for a second opinion.")
            try:
                second = second_review(run, node, answer, visible)
            except BudgetExceeded:
                raise
            except Exception as problem:
                unavailable = f"Second review unavailable, so not verified: {problem}"
                run.set(node, status="needs_human", review_notes=_clip(unavailable, 280))
                run.post(agent, "coordinator", "escalate", _clip(unavailable, 300), node)
                return {"status": "needs_human", "answer": answer, "review": unavailable,
                        "caught": node["caught"]}
            second_notes = (second.get("notes") or "").strip()
            if second.get("verdict") != "pass":
                review = {"verdict": "revise", "notes": f"Reviewers disagree. Gemini: {second_notes}"}
                notes = review["notes"]
            else:
                notes = f"{notes} Gemini agreed: {second_notes}"

        if review.get("verdict") == "pass":
            if node["caught"]:
                summary = f"Caught: {_clip(node['caught'][-1], 200)} Fixed and passed."
                run.post(agent, "reviewer", "reply", f"Revised after your review. {_clip(notes, 200)}", node)
            else:
                summary = f"Passed: {notes}"
            run.set(node, status="done", review_notes=_clip(summary, 400))
            cited = sorted({e["source"] for e in answer.get("evidence", [])})
            return {"status": "done", "answer": answer, "review": notes,
                    "caught": node["caught"], "sources": cited}

        node["feedback"] = notes
        node["caught"].append(notes)
        run.post("reviewer", agent, "challenge", notes, node)
        run.set(node, review_notes=f"Sent back: {_clip(notes, 280)}")

    run.set(node, status="needs_human")
    run.post(agent, "coordinator", "escalate",
             f"'{spec['title']}' is held for a human: {_clip(node['feedback'] or '', 240)}", node)
    return {"status": "needs_human", "answer": node["answer"], "review": node["feedback"],
            "caught": node["caught"]}


# ---------- the scheduler ----------

def _fail(run, node, why):
    node["status"] = "failed"
    board.update_task(node["db_id"], status="needs_human", review_notes=_clip(why, 300))


def _handle_request(run, node, missing):
    """A writer says a source is missing: start a research task for it and make the writer wait."""
    if run.requests_made >= MAX_SOURCE_REQUESTS:
        _fail(run, node, f"Needs a source that couldn't be found: {_clip(missing, 200)}")
        run.post(node["agent"], "coordinator", "escalate",
                 f"Still missing a source and the request limit is used: {_clip(missing, 200)}", node)
        dashboard.render(run.run_id)
        return
    run.requests_made += 1
    spec = {"key": f"req{run.requests_made}", "type": "research",
            "title": f"Find a source: {_clip(missing, 60)}", "instructions": missing,
            "acceptance": ["At least one fetched page that could answer this."], "depends_on": []}
    new = run.add_node(spec)
    run.sync_deps(new)
    node["deps"].append(new["key"])
    node["status"] = "pending"
    run.sync_deps(node)
    run.post(node["agent"], new["agent"], "request", missing, node)


def _finish(run, node, future):
    try:
        result = future.result()
    except BudgetExceeded:
        run.stopped = True
        node["status"] = "stopped"
        board.update_task(node["db_id"], status="stopped", review_notes="Stopped by the budget cap.")
        dashboard.render(run.run_id)
        return
    except Exception as error:
        _fail(run, node, f"Error: {type(error).__name__}: {error}")
        run.post(node["agent"], "coordinator", "escalate",
                 f"I hit an error and am held: {_clip(str(error), 200)}", node)
        return
    if result["status"] == "need_source":
        _handle_request(run, node, result["missing"])
        return
    node["result"] = result
    if result["status"] == "done":
        node["status"] = "done"
        if node["spec"]["type"] == "research":
            run.set(node, status="done", result=result.get("summary", ""),
                    review_notes="Pages fetched and saved by code.")
    else:
        node["status"] = "failed"
        board.update_task(node["db_id"], status="needs_human")
    dashboard.render(run.run_id)


def run_plan(run):
    """Run every task as soon as the tasks it depends on are done, up to max_parallel at once."""
    worker = {"research": run_research, "write": run_write}
    futures = {}
    with ThreadPoolExecutor(max_workers=run.max_parallel) as pool:
        while True:
            changed = True                   # a task whose dependency failed can never run
            while changed:                   # (repeat, so a failure can ripple down a chain)
                changed = False
                for node in run.nodes:
                    if node["status"] == "pending":
                        broken = [run.node(k) for k in node["deps"]
                                  if run.node(k)["status"] in ("failed", "stopped")]
                        if broken and any(b["status"] == "stopped" for b in broken):
                            node["status"] = "stopped"     # the budget ended the run, not a failure
                            board.update_task(node["db_id"], status="stopped",
                                              review_notes="Not started: the budget cap was reached.")
                            changed = True
                        elif broken:
                            _fail(run, node, f"Blocked: '{broken[0]['spec']['title']}' did not finish.")
                            changed = True
            if not run.stopped:
                for node in run.nodes:
                    if len(futures) >= run.max_parallel:
                        break
                    if node["status"] == "pending" and all(run.node(k)["status"] == "done" for k in node["deps"]):
                        node["status"] = "running"
                        futures[pool.submit(worker[node["spec"]["type"]], run, node)] = node
            if not futures:
                break
            done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
            for future in done:
                _finish(run, futures.pop(future), future)
    for node in run.nodes:                   # anything never started because the budget stopped the run
        if node["status"] == "pending":
            node["status"] = "stopped"
            board.update_task(node["db_id"], status="stopped",
                              review_notes="Not started: the budget cap was reached.")
    dashboard.render(run.run_id)


# ---------- the brief, assembled by code ----------

def write_brief(run):
    OUTPUT_DIR.mkdir(exist_ok=True)
    library = workspace.load_library(run.run_id)
    writers = [n for n in run.nodes if n["spec"]["type"] == "write"]
    lines = [f"# Brief · run {run.run_id}", "", f"**Goal:** {run.goal}", "",
             "_General information from public sources, not personal advice._", "",
             "| Section | Review |", "|---|---|"]
    for n in writers:
        done = n["status"] == "done"
        lines.append(f"| {n['spec']['title']} | "
                     f"{'passed, attempt ' + str(n['attempts']) if done else '**needs a human**'} |")
    used = set()
    for number, n in enumerate(writers, start=1):
        lines += ["", f"## {number}. {n['spec']['title']}", ""]
        if n["status"] != "done":
            lines.append(f"**Not verified by the crew.** Last note: {n['feedback'] or (n['result'] or {}).get('review') or n['status']}")
            continue
        answer = n["result"]["answer"]
        lines += [answer["text"].strip(), ""]
        for e in answer["evidence"]:
            s = library[e["source"]]
            used.add(e["source"])
            lines.append(f"> \"{e['quote']}\"  \n> [{e['source']}] {s.get('publisher', '')}: {s.get('title', '')}")
        if n["caught"]:
            lines += ["", f"_Caught in review: {_clip(n['caught'][-1], 240)} Fixed on attempt {n['attempts']}._"]
        else:
            lines += ["", f"_Reviewer: {n['result']['review']}_"]
    if used:
        lines += ["", "## Sources", ""]
        for sid in sorted(used, key=lambda s: int(s[1:])):
            s = library[sid]
            lines.append(f"- [{sid}] {s.get('publisher', '')}: {s.get('title', '')} — "
                         f"{s.get('url', '')} (retrieved {s.get('retrieved', 'n/a')})")
    held = sum(n["status"] != "done" for n in writers)
    lines += ["", "---",
              f"{len(run.nodes)} tasks, {board.message_count(run.run_id)} messages, "
              f"{run.requests_made} extra source request(s), {held} held. "
              f"Crew cost: {dashboard._money(board.run_cost(run.run_id))} of a ${BUDGET_USD:.2f} budget."]
    path = OUTPUT_DIR / f"brief_run{run.run_id}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------- main ----------

def main(goal=DEFAULT_GOAL, use_gemini=False, max_parallel=None, with_pack=None, web_search=None):
    max_parallel = max(1, max_parallel or MAX_PARALLEL)
    with_pack = WITH_PACK if with_pack is None else with_pack
    web_search = WEB_SEARCH if web_search is None else web_search
    tags = [name for name, on in (("Pipeline", True), (f"{max_parallel} in parallel", max_parallel > 1),
                                  ("Web search", web_search), ("Gemini second review", use_gemini)) if on]
    run_id = board.start_run(f"{crew._short_title(goal)} · {' + '.join(tags)}", budget_usd=BUDGET_USD)
    run = Run(run_id, goal, use_gemini=use_gemini, web_search=web_search,
              max_parallel=max_parallel, base_ids=[])
    dashboard.render(run_id)
    log(f"Run {run_id} started: up to {max_parallel} agents at once, ${BUDGET_USD:.2f} budget.")
    try:
        if with_pack:
            run.base_ids = workspace.import_pack(run_id)
        plan = coordinator(run)
        log(f"Coordinator made {len(plan)} tasks.")
        for spec in plan:
            run.add_node(spec)
        for node in run.nodes:
            run.sync_deps(node)
        dashboard.render(run_id)
        run_plan(run)
        writers = [n for n in run.nodes if n["spec"]["type"] == "write"]
        if run.stopped:
            log(f"Run {run_id} stopped by the budget cap.")
        else:
            status = "completed" if all(n["status"] == "done" for n in writers) else "needs_human"
            board.finish_run(run_id, status=status)
            log(f"Run {run_id} {status}.")
        path = write_brief(run)
        shown = path.relative_to(HERE) if path.is_relative_to(HERE) else path
        log(f"Brief: {shown}")
    except BudgetExceeded as stop:
        log(f"Run {run_id} stopped by the budget cap. {stop}")
    except Exception:
        board.finish_run(run_id, status="failed")
        raise
    finally:
        dashboard.render(run_id)
        log(f"Total cost: {dashboard._money(board.run_cost(run_id))}")
    return run_id


if __name__ == "__main__":
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("--")}
    parallel = None
    if "--parallel" in args:
        position = args.index("--parallel")
        parallel = int(args[position + 1])
        args = args[:position] + args[position + 2:]
    words = [a for a in args if not a.startswith("--")]
    main(words[0] if words else DEFAULT_GOAL, use_gemini="--gemini" in flags, max_parallel=parallel,
         with_pack=True if "--pack" in flags or not words else None,
         web_search=True if "--web-search" in flags else None)

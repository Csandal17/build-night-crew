"""Task board: a small SQLite store for runs, tasks and model calls.

It survives restarts, so work is never lost if the script stops,
and it records the cost of every model call for the dashboard.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "board.db"

# USD per million tokens: (input, output).
# Source: Anthropic pricing page, October 2026 (Haiku price is for prompts up to 100k tokens).
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    # Gemini, paid-tier list prices (Sept 2026; they double on 1 Jan 2027). A free Google
    # AI Studio key is charged nothing, so these show what the same calls would cost on
    # a paid key. Thinking tokens are counted as output, as Google bills them.
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-2.5-flash": (0.30, 2.50),
    # Coding agents run through the Sparkles CLI use your own Claude Code (or Codex/OpenCode)
    # plan, and Sparkles' result doesn't report tokens. So they're recorded with 0 tokens
    # and $0 here: not free, just not visible to this board.
    "sparkles-claude": (0.0, 0.0),
    "sparkles-codex": (0.0, 0.0),
    "sparkles-opencode": (0.0, 0.0),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    budget_usd REAL NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    title TEXT NOT NULL,
    owner TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    result TEXT,
    review_notes TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    task_id INTEGER REFERENCES tasks(id),
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',
    attempts INTEGER NOT NULL DEFAULT 0,
    run_id INTEGER REFERENCES runs(id),
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source, fingerprint)
);
CREATE TABLE IF NOT EXISTS briefings (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    characters INTEGER NOT NULL,
    audio_path TEXT
);
"""

TASK_FIELDS = {"owner", "status", "result", "review_notes", "attempts"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _execute(sql, params=()):
    """Run one change, save it, and return the new row's id."""
    conn = _connect()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _fetch(sql, params=()):
    """Run one query and return the rows as dictionaries."""
    conn = _connect()
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def cost_of(model, input_tokens, output_tokens):
    if model not in PRICES:
        raise ValueError(f"No price for {model}. Add it to PRICES so costs stay honest.")
    price_in, price_out = PRICES[model]
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def start_run(goal, budget_usd=1.00):
    return _execute(
        "INSERT INTO runs (goal, budget_usd, started_at) VALUES (?, ?, ?)",
        (goal, budget_usd, _now()),
    )


def finish_run(run_id, status="completed"):
    _execute(
        "UPDATE runs SET status = ?, finished_at = ? WHERE id = ?",
        (status, _now(), run_id),
    )


def add_task(run_id, title, owner=None):
    return _execute(
        "INSERT INTO tasks (run_id, title, owner, updated_at) VALUES (?, ?, ?, ?)",
        (run_id, title, owner, _now()),
    )


def update_task(task_id, **fields):
    unknown = set(fields) - TASK_FIELDS
    if unknown:
        raise ValueError(f"Unknown task fields: {unknown}")
    fields["updated_at"] = _now()
    assignments = ", ".join(f"{name} = ?" for name in fields)
    _execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*fields.values(), task_id))


def record_call(run_id, agent, model, input_tokens, output_tokens, task_id=None):
    cost = cost_of(model, input_tokens, output_tokens)
    _execute(
        """INSERT INTO calls (run_id, task_id, agent, model, input_tokens,
                              output_tokens, cost_usd, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, task_id, agent, model, input_tokens, output_tokens, cost, _now()),
    )
    return cost


def run_cost(run_id):
    rows = _fetch(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM calls WHERE run_id = ?",
        (run_id,),
    )
    return rows[0]["total"]


def summary(run_id):
    run = _fetch("SELECT * FROM runs WHERE id = ?", (run_id,))[0]
    tasks = _fetch("SELECT * FROM tasks WHERE run_id = ? ORDER BY id", (run_id,))
    by_agent = _fetch(
        """SELECT agent, SUM(input_tokens) AS input_tokens,
                  SUM(output_tokens) AS output_tokens, SUM(cost_usd) AS cost_usd
           FROM calls WHERE run_id = ?
           GROUP BY agent ORDER BY cost_usd DESC""",
        (run_id,),
    )
    return {"run": run, "tasks": tasks, "by_agent": by_agent, "total_usd": run_cost(run_id)}


def all_runs():
    return _fetch(
        """SELECT runs.*, COALESCE(SUM(calls.cost_usd), 0) AS cost_usd
           FROM runs LEFT JOIN calls ON calls.run_id = runs.id
           GROUP BY runs.id ORDER BY runs.id"""
    )


def spent_today():
    """Total spent on model calls since midnight (local time), across every run."""
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    since = midnight.astimezone(timezone.utc).isoformat(timespec="seconds")
    rows = _fetch(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM calls WHERE created_at >= ?",
        (since,),
    )
    return rows[0]["total"]


def close_interrupted_runs():
    """Runs still marked 'running' when the watcher starts were cut off part way. Say so."""
    stuck = _fetch("SELECT id FROM runs WHERE status = 'running'")
    for row in stuck:
        finish_run(row["id"], status="interrupted")
    return len(stuck)


# ---------- events: work waiting for the always-on crew ----------

EVENT_FIELDS = {"status", "attempts", "run_id", "error"}


def add_event(source, fingerprint, goal):
    """Queue a piece of work. Returns its id, or None if this exact version
    of this file was queued before, so a duplicate never runs twice."""
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO events (source, fingerprint, goal, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (source, fingerprint, goal, _now(), _now()),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount else None
    finally:
        conn.close()


def update_event(event_id, **fields):
    unknown = set(fields) - EVENT_FIELDS
    if unknown:
        raise ValueError(f"Unknown event fields: {unknown}")
    fields["updated_at"] = _now()
    assignments = ", ".join(f"{name} = ?" for name in fields)
    _execute(f"UPDATE events SET {assignments} WHERE id = ?", (*fields.values(), event_id))


def next_event():
    """The oldest piece of work still waiting, or None if the inbox is clear."""
    rows = _fetch("SELECT * FROM events WHERE status = 'waiting' ORDER BY id LIMIT 1")
    return rows[0] if rows else None


def recover_events():
    """After a restart, anything left 'running' was interrupted. Put it back in the queue."""
    stuck = _fetch("SELECT id FROM events WHERE status = 'running'")
    for row in stuck:
        update_event(row["id"], status="waiting")
    return len(stuck)


def recent_events(limit=8):
    return _fetch("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


def events_since(since=None, limit=5):
    """Events that changed after `since` (oldest first). None means the latest few."""
    if since is None:
        rows = _fetch("SELECT * FROM events ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,))
    else:
        rows = _fetch("SELECT * FROM events WHERE updated_at > ? "
                      "ORDER BY updated_at DESC, id DESC LIMIT ?", (since, limit))
    return list(reversed(rows))


# ---------- briefings: the spoken handovers made by brief.py ----------

def last_briefing():
    rows = _fetch("SELECT * FROM briefings ORDER BY id DESC LIMIT 1")
    return rows[0] if rows else None


def add_briefing(characters, audio_path=None):
    return _execute(
        "INSERT INTO briefings (created_at, characters, audio_path) VALUES (?, ?, ?)",
        (_now(), characters, audio_path),
    )


if __name__ == "__main__":
    # Self-test with made-up token counts. No model is called, so it's free.
    run_id = start_run("Self-test: does the board save and add up costs?", budget_usd=1.00)
    t1 = add_task(run_id, "Research the topic", owner="worker-1")
    t2 = add_task(run_id, "Write the summary", owner="worker-2")

    record_call(run_id, "coordinator", "claude-haiku-5-5", 400, 150)
    record_call(run_id, "worker-1", "claude-sonnet-5-5", 2000, 800, task_id=t1)
    record_call(run_id, "worker-2", "claude-sonnet-5-5", 1500, 600, task_id=t2)
    record_call(run_id, "reviewer", "claude-haiku-5-5", 900, 200, task_id=t2)

    update_task(t1, status="done", result="Notes saved", attempts=1)
    update_task(t2, status="done", result="Summary saved", attempts=1)
    finish_run(run_id)

    s = summary(run_id)
    print(f"Run {run_id}: {s['run']['status']}, "
          f"${s['total_usd']:.4f} of ${s['run']['budget_usd']:.2f} budget")
    for a in s["by_agent"]:
        print(f"  {a['agent']:<12} ${a['cost_usd']:.4f}  "
              f"({a['input_tokens']} in, {a['output_tokens']} out)")

    print(f"\nAll runs saved in {DB_PATH.name}:")
    for r in all_runs():
        print(f"  Run {r['id']}: {r['status']}, ${r['cost_usd']:.4f}")
        
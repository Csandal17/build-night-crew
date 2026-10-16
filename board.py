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
        
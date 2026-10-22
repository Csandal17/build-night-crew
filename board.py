"""Task board: a small SQLite store for runs, tasks and model calls.

It survives restarts, so work is never lost if the script stops,
and it records the cost of every model call for the dashboard.
"""

import json
import sqlite3
import threading
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
    # Web search is billed per search, not per token. It is recorded as one "input token"
    # per search at $0.01 each (an assumed price: check Anthropic's pricing page).
    "web-search": (10000.0, 0.0),
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
    updated_at TEXT NOT NULL,
    task_type TEXT NOT NULL DEFAULT 'claim',
    depends_on TEXT NOT NULL DEFAULT '[]'
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
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    task_id INTEGER,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_reads (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    agent TEXT NOT NULL,
    PRIMARY KEY (message_id, agent)
);
"""

TASK_FIELDS = {"owner", "status", "result", "review_notes", "attempts", "task_type", "depends_on"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_ready = set()           # database files whose schema has been set up in this process
_ready_lock = threading.Lock()


def _migrate(conn):
    """Add columns that older boards don't have, so an existing board.db keeps working."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "task_type" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT 'claim'")
    if "depends_on" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN depends_on TEXT NOT NULL DEFAULT '[]'")
    conn.commit()


def _connect():
    """A new connection. Several agents may write at once, so wait up to 30 seconds for a
    lock instead of failing. The schema is set up once per database file, not on every call."""
    key = str(DB_PATH)
    if not DB_PATH.exists():
        _ready.discard(key)  # a deleted board.db starts fresh
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    if key not in _ready:
        with _ready_lock:
            if key not in _ready:
                conn.executescript(SCHEMA)
                _migrate(conn)
                _ready.add(key)
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


def add_task(run_id, title, owner=None, task_type="claim", depends_on=None):
    return _execute(
        "INSERT INTO tasks (run_id, title, owner, updated_at, task_type, depends_on) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, title, owner, _now(), task_type, json.dumps(list(depends_on or []))),
    )


def update_task(task_id, **fields):
    unknown = set(fields) - TASK_FIELDS
    if unknown:
        raise ValueError(f"Unknown task fields: {unknown}")
    if "depends_on" in fields and not isinstance(fields["depends_on"], str):
        fields["depends_on"] = json.dumps(list(fields["depends_on"]))
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


def run_status(run_id):
    rows = _fetch("SELECT status FROM runs WHERE id = ?", (run_id,))
    return rows[0]["status"] if rows else None


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


# ---------- messages: agents talking to each other on the board ----------

MESSAGE_KINDS = {"request", "offer", "challenge", "reply", "escalate", "say"}
MAX_MESSAGES_PER_RUN = 60   # a hard cap, so chatter can't run away
MAX_MESSAGE_CHARS = 600


def post_message(run_id, sender, recipient, kind, text, task_id=None):
    """Post a message to one agent (or "all"). Returns its id, or None if the run has
    reached its message cap. The cap check and the insert happen together, so agents
    posting at the same moment can't go over it."""
    if kind not in MESSAGE_KINDS:
        raise ValueError(f"Unknown message kind {kind!r}. Use one of {sorted(MESSAGE_KINDS)}.")
    text = " ".join(str(text).split())[:MAX_MESSAGE_CHARS]
    if not text:
        return None
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        count = conn.execute("SELECT COUNT(*) FROM messages WHERE run_id = ?", (run_id,)).fetchone()[0]
        if count >= MAX_MESSAGES_PER_RUN:
            conn.rollback()
            return None
        cur = conn.execute(
            "INSERT INTO messages (run_id, task_id, sender, recipient, kind, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, task_id, sender, recipient, kind, text, _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def unread_messages(run_id, agent, limit=6):
    """Messages addressed to this agent (or to "all") that it hasn't read yet, oldest first.
    An agent never sees its own messages."""
    return _fetch(
        """SELECT * FROM messages
           WHERE run_id = ? AND sender != ? AND recipient IN (?, 'all')
             AND id NOT IN (SELECT message_id FROM message_reads WHERE agent = ?)
           ORDER BY id LIMIT ?""",
        (run_id, agent, agent, agent, limit),
    )


def mark_read(agent, message_ids):
    conn = _connect()
    try:
        conn.executemany("INSERT OR IGNORE INTO message_reads (message_id, agent) VALUES (?, ?)",
                         [(mid, agent) for mid in message_ids])
        conn.commit()
    finally:
        conn.close()


def messages_for_run(run_id, limit=30):
    """The latest messages in a run, oldest first, for the dashboard feed."""
    rows = _fetch("SELECT * FROM messages WHERE run_id = ? ORDER BY id DESC LIMIT ?", (run_id, limit))
    return list(reversed(rows))


def message_count(run_id):
    return _fetch("SELECT COUNT(*) AS n FROM messages WHERE run_id = ?", (run_id,))[0]["n"]


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
        
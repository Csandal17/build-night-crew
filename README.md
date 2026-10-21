# build-night-crew

Pre-tested starter for Moonfire Build Night (22 Oct 2026): Claude agents with a shared task board, a per-run budget cap and live cost tracking. Work in progress.

## Requirements

- [uv](https://docs.astral.sh/uv/) (Python 3.12 is installed automatically)
- Your own Anthropic API key

## Setup

```bash
git clone https://github.com/Csandal17/build-night-crew.git
cd build-night-crew
uv sync
```

Create a `.env` file in the project folder:

```
ANTHROPIC_API_KEY=your-key-here
```

## Check your setup

```bash
uv run main.py   # one small Claude call, to check your API key works
```

## Run the crew

```bash
uv run crew.py               # run the evidence crew on the default goal; writes a brief to output/
uv run crew.py --challenge   # plant one known mistake to prove the reviewer catches it
uv run crew.py --challenge --fast-reviewer   # same, with Haiku as the reviewer, to compare cost
uv run dashboard.py          # open the live dashboard (keep it open while the crew runs)
```

For a clean demo, delete `board.db` first so the dashboard starts with no old runs.

The source pack lives in `sources/`: one `.txt` file per source, with header lines, then `---`, then the excerpt.

## Always-on mode

```bash
uv run watch.py   # waits for work in inbox/ and wakes the crew when something arrives (Ctrl+C to stop)
```

Plain code does the waiting, so a quiet inbox costs nothing. Drop a `.txt` file with a claim into `inbox/` and the crew picks it up within a few seconds. The same file is never run twice, an edited file counts as new work, and work cut off by a restart is picked up again. A daily cap (`DAILY_BUDGET_USD` in `watch.py`) stops new runs once today's spending can't cover another one.

A three-step demo, with the dashboard open:

```bash
# 1. Start the watcher in one terminal. The dashboard shows it waiting.
uv run watch.py

# 2. In a second terminal, give it work.
echo "Adults should consider taking a daily vitamin D supplement during autumn and winter." > inbox/vitamin-d.txt

# 3. Stop the watcher mid-run with Ctrl+C, start it again, and watch it pick the work back up.
#    Copy the same file under the same name again and nothing new runs.
```

## What's in here

- `llm.py`: the only way agents call Claude. Timeouts, a per-run budget cap, and cost tracking on every call.
- `board.py`: a SQLite task board recording every run, task and model call, with its cost.
- `dashboard.py`: the live dashboard, which updates while a run is going.
- `crew.py`: the example crew. A coordinator splits the goal, workers check each claim against `sources/`, code checks every quote word for word, and a reviewer approves the work or sends it back once.
- `sources/`: the example source pack (NHS excerpts, checked against the live pages).
- `watch.py`: always-on mode. Watches `inbox/`, queues new work with a fixed ID, runs the crew one piece at a time, retries once, recovers after a restart, and keeps to a daily spending cap.

The first three files work for any crew. `crew.py` and `sources/` are the example to adapt or swap out.

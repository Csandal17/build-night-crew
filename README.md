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

## What's in here

- `llm.py`: the only way agents call Claude. Timeouts, a per-run budget cap, and cost tracking on every call.
- `board.py`: a SQLite task board recording every run, task and model call, with its cost.
- `dashboard.py`: the live dashboard, which updates while a run is going.
- `crew.py`: the example crew. A coordinator splits the goal, workers check each claim against `sources/`, code checks every quote word for word, and a reviewer approves the work or sends it back once.
- `sources/`: the example source pack (NHS excerpts, checked against the live pages).

The first three files work for any crew. `crew.py` and `sources/` are the example to adapt or swap out.

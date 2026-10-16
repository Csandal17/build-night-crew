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

## Run

```bash
uv run main.py
```

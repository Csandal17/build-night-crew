"""A crew worker that writes code: one coding-agent turn through the Sparkles CLI.

`sparkles exec` runs one non-interactive turn of a coding agent (Claude Code, Codex or
OpenCode) inside a repository folder and prints a JSON result. This wraps it so the crew
can use it like any other agent:
- hard limits on turns and time, so a stuck agent can't run forever;
- it refuses to run in a folder that holds a .env file, and the agent never sees the
  crew's own API keys, so a coding agent can't read or leak them;
- the real change is read from git afterwards, so the reviewer judges the actual diff,
  not the agent's description of what it did;
- every turn is recorded on the task board and shows on the dashboard.

The Sparkles CLI is found on your PATH, or at ~/sparkles-cli/sparkles, or wherever
SPARKLES_BIN in .env points.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from dotenv import load_dotenv

import board
import dashboard

load_dotenv()

SPARKLES = (os.getenv("SPARKLES_BIN") or shutil.which("sparkles")
            or str(Path.home() / "sparkles-cli" / "sparkles"))
MAX_DIFF_CHARS = 8000  # how much of the change the reviewer is shown


def _agent_env():
    """The agent's environment: everything except API keys and other secrets the crew holds."""
    secret = ("_API_KEY", "_TOKEN", "_SECRET")
    return {k: v for k, v in os.environ.items() if not k.upper().endswith(secret)}


def _git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return result.stdout


def changes(repo):
    """What the agent actually changed: edits to tracked files, plus any new files."""
    diff = _git(repo, "diff")
    new_files = [f for f in _git(repo, "ls-files", "--others", "--exclude-standard").splitlines() if f]
    for name in new_files:
        try:
            text = (Path(repo) / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            text = "(binary or unreadable file)"
        diff += f"\n--- new file: {name}\n{text}"
    changed = [f for f in _git(repo, "diff", "--name-only").splitlines() if f] + new_files
    return diff.strip(), changed


def run(prompt, repo, *, run_id=None, task_id=None, agent="sparkles-worker", runtime="claude",
        max_turns=6, timeout_minutes=5, approve="edits", mirror=False, allow_env_file=False):
    """Run one Sparkles agent turn in `repo` and return what happened:
    {"status", "message", "session_id", "diff", "files"}. Raises RuntimeError if it fails."""
    repo = Path(repo).expanduser().resolve()
    if not (repo / ".git").exists():
        raise RuntimeError(f"{repo} isn't a git repository, so the change couldn't be checked.")
    if (repo / ".env").exists() and not allow_env_file:
        raise RuntimeError(f"{repo} contains a .env file. Coding agents don't run next to "
                           "live keys; use a folder without one.")
    if not Path(SPARKLES).exists() and not shutil.which(SPARKLES):
        raise RuntimeError(f"Sparkles CLI not found at {SPARKLES}. Set SPARKLES_BIN in .env.")

    command = [SPARKLES, "exec", prompt, "--json", "--runtime", runtime, "--approve", approve,
               "--max-turns", str(max_turns), "--timeout", f"{timeout_minutes}m", "-C", str(repo),
               "--mirror" if mirror else "--local-only"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=_agent_env(),
                                timeout=timeout_minutes * 60 + 60)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"The Sparkles agent was still running after {timeout_minutes} minutes.")

    lines = [line for line in result.stdout.splitlines() if line.strip().startswith("{")]
    if not lines:
        detail = (result.stderr or result.stdout).strip()[:300]
        raise RuntimeError(f"Sparkles gave no result (exit {result.returncode}): {detail}")
    reply = json.loads(lines[-1])

    if run_id is not None:
        board.record_call(run_id, agent, f"sparkles-{runtime}", 0, 0, task_id=task_id)
        dashboard.render(run_id)
    if reply.get("status") != "completed":
        raise RuntimeError(f"Sparkles agent ended with status {reply.get('status')!r}: "
                           f"{reply.get('lastMessage') or result.stderr.strip()[:300]}")

    diff, files = changes(repo)
    return {"status": reply["status"], "message": reply.get("lastMessage", ""),
            "session_id": reply.get("sessionId"), "diff": diff[:MAX_DIFF_CHARS], "files": files}

"""Safe model calls: every agent talks to Claude through ask().

- Timeout and retries, so a stalled connection becomes a visible error, not a silent hang.
- Never starts a call the run can't afford, and stops the run when the budget is used up.
- Records tokens and cost on the task board, and refreshes the dashboard, after every call.
"""

from anthropic import Anthropic
from dotenv import load_dotenv

import board
import dashboard

load_dotenv()

FAST = "claude-haiku-5-5"    # cheap: splitting goals, checking formats, reviews
SMART = "claude-sonnet-5-5"  # the real work

_client = Anthropic(timeout=60.0, max_retries=2)


class BudgetExceeded(Exception):
    """Raised when a call would take the run past its budget."""


def _budget(run_id):
    return board._fetch("SELECT budget_usd FROM runs WHERE id = ?", (run_id,))[0]["budget_usd"]


def _stop(run_id, reason):
    board.finish_run(run_id, status="stopped_budget")
    dashboard.render(run_id)
    raise BudgetExceeded(reason)


def ask(run_id, agent, prompt, *, system=None, model=SMART, max_tokens=1000, task_id=None):
    """Call Claude once on behalf of an agent and return the text of its reply."""
    budget = _budget(run_id)
    spent = board.run_cost(run_id)

    # Worst case for this call: roughly 1 token per 3 characters in, and max_tokens out.
    worst_case = board.cost_of(model, (len(prompt) + len(system or "")) // 3, max_tokens)
    if spent + worst_case > budget:
        money = dashboard._money
        left = money(budget - spent)
        _stop(run_id, f"{agent} needs up to {money(worst_case)}, but only {left} is left.")

    request = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        request["system"] = system
    response = _client.messages.create(**request)

    text = "".join(block.text for block in response.content if block.type == "text")
    board.record_call(run_id, agent, model, response.usage.input_tokens,
                      response.usage.output_tokens, task_id=task_id)
    dashboard.render(run_id)
    return text


if __name__ == "__main__":
    # Test 1: a worker and a reviewer, each making one small real call.
    run_id = board.start_run("Helper test: one worker, one reviewer", budget_usd=1.00)
    task = board.add_task(run_id, "Name three UK seaside towns", owner="worker-1")
    board.update_task(task, status="running")
    answer = ask(run_id, "worker-1",
                 "Name three UK seaside towns, comma separated, nothing else.",
                 model=FAST, max_tokens=50, task_id=task)
    board.update_task(task, status="waiting_review", result=answer, attempts=1)
    verdict = ask(run_id, "reviewer",
                  f"Is this a list of three UK seaside towns? Reply PASS or FAIL only.\n\n{answer}",
                  model=FAST, max_tokens=10, task_id=task)
    board.update_task(task, status="done", review_notes=verdict.strip())
    board.finish_run(run_id)
    dashboard.render(run_id)
    print(f"Test 1: worker said '{answer.strip()}', reviewer said '{verdict.strip()}', "
          f"total ${board.run_cost(run_id):.5f}")

    # Test 2: a tiny budget, so the run has to stop itself partway through.
    run_id = board.start_run("Budget test: should stop itself", budget_usd=0.001)
    try:
        for n in range(1, 6):
            ask(run_id, f"worker-{n}", "Write one short sentence about the sea.",
                model=SMART, max_tokens=60)
            print(f"Test 2: call {n} ok, spent ${board.run_cost(run_id):.5f}")
    except BudgetExceeded as stop:
        print(f"Test 2: stopped as planned. {stop}")
        
"""Safe model calls: every agent talks to Claude through ask().

- Timeout and retries, so a stalled connection becomes a visible error, not a silent hang.
- Never starts a call the run can't afford, and stops the run when the budget is used up.
- Records tokens and cost on the task board, and refreshes the dashboard, after every call.
- ask_json() asks for structured answers through tool use, so replies arrive as valid JSON
  (or as a clear error the crew can retry).
"""

import json

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


def _call(run_id, agent, prompt, system, model, max_tokens, task_id, **extra):
    """One model call with the budget check before it and cost tracking after it."""
    budget = _budget(run_id)
    spent = board.run_cost(run_id)

    # Worst case for this call: roughly 1 token per 3 characters in, and max_tokens out.
    in_chars = len(prompt) + len(system or "") + len(json.dumps(extra.get("tools", "")))
    worst_case = board.cost_of(model, in_chars // 3 + (400 if "tools" in extra else 0), max_tokens)
    if spent + worst_case > budget:
        money = dashboard._money
        left = money(budget - spent)
        _stop(run_id, f"{agent} needs up to {money(worst_case)}, but only {left} is left.")

    request = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        **extra,
    }
    if system:
        request["system"] = system
    response = _client.messages.create(**request)

    board.record_call(run_id, agent, model, response.usage.input_tokens,
                      response.usage.output_tokens, task_id=task_id)
    dashboard.render(run_id)
    return response


def ask(run_id, agent, prompt, *, system=None, model=SMART, max_tokens=1000, task_id=None):
    """Call Claude once on behalf of an agent and return the text of its reply."""
    response = _call(run_id, agent, prompt, system, model, max_tokens, task_id)
    return "".join(block.text for block in response.content if block.type == "text")


def ask_json(run_id, agent, prompt, schema, *, system=None, model=SMART,
             max_tokens=1000, task_id=None):
    """Like ask(), but returns a dict. Claude is asked to answer through a 'submit' tool
    whose input follows `schema`; if it doesn't, a ValueError says so."""
    tool = {"name": "submit", "description": "Submit your answer.", "input_schema": schema}
    prompt += "\n\nAnswer only by calling the submit tool."
    response = _call(run_id, agent, prompt, system, model, max_tokens, task_id,
                     tools=[tool], tool_choice={"type": "auto"})
    if response.stop_reason == "max_tokens":
        raise ValueError("The answer was cut off before it finished.")
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    raise ValueError("No structured answer in the reply.")


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
        
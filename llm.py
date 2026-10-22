"""Safe model calls: every agent talks to Claude through ask().

- Timeout and retries, so a stalled connection becomes a visible error, not a silent hang.
- Never starts a call the run can't afford, and stops the run when the budget is used up.
- Records tokens and cost on the task board, and refreshes the dashboard, after every call.
- ask_json() asks for structured answers through tool use, so replies arrive as valid JSON
  (or as a clear error the crew can retry).
"""

import json
import threading

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


# Money set aside for calls that are in flight. Agents can run in parallel, so every call
# reserves its worst case before it starts and gives it back once its real cost is on the
# board. Without this, several calls could each pass the budget check and together overshoot.
_reserved = {}
_reserve_lock = threading.Lock()


def reserve(run_id, agent, worst_case):
    """Set aside `worst_case` dollars for one call, or stop the run if they aren't there."""
    refusal = None
    with _reserve_lock:
        if board.run_status(run_id) == "stopped_budget":
            refusal = "The run has already been stopped by its budget."
        else:
            budget = _budget(run_id)
            spent = board.run_cost(run_id)
            in_flight = _reserved.get(run_id, 0.0)
            if spent + in_flight + worst_case > budget:
                money = dashboard._money
                left = money(max(0.0, budget - spent - in_flight))
                refusal = f"{agent} needs up to {money(worst_case)}, but only {left} is left."
            else:
                _reserved[run_id] = in_flight + worst_case
    if refusal:
        _stop(run_id, refusal)  # raises BudgetExceeded


def release(run_id, worst_case):
    with _reserve_lock:
        _reserved[run_id] = max(0.0, _reserved.get(run_id, 0.0) - worst_case)


def _call(run_id, agent, prompt, system, model, max_tokens, task_id, reserve_extra=0.0, **extra):
    """One model call with the budget check before it and cost tracking after it."""
    # Worst case for this call: roughly 1 token per 3 characters in, and max_tokens out.
    in_chars = len(prompt) + len(system or "") + len(json.dumps(extra.get("tools", "")))
    worst_case = board.cost_of(model, in_chars // 3 + (400 if "tools" in extra else 0), max_tokens)
    worst_case += reserve_extra
    reserve(run_id, agent, worst_case)

    request = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        **extra,
    }
    if system:
        request["system"] = system
    try:
        response = _client.messages.create(**request)
        # Record the real cost before giving the reservation back, so the money is always
        # counted either as reserved or as spent.
        board.record_call(run_id, agent, model, response.usage.input_tokens,
                          response.usage.output_tokens, task_id=task_id)
    finally:
        release(run_id, worst_case)
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


SEARCH_COST_USD = 0.01   # assumed price per web search: check Anthropic's pricing page
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}


def _field(item, name):
    """Read a field from an SDK object or a plain dict."""
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def search_results(response):
    """The pages a web-search reply found, as [{"url", "title"}] in order, without repeats."""
    found, seen = [], set()
    for block in response.content:
        if _field(block, "type") != "web_search_tool_result":
            continue
        content = _field(block, "content")
        if not isinstance(content, list):
            continue  # an error from the search tool, not a list of pages
        for item in content:
            url = _field(item, "url")
            if url and url not in seen:
                seen.add(url)
                found.append({"url": url, "title": _field(item, "title") or ""})
    return found


def ask_search(run_id, agent, prompt, *, system=None, model=FAST, max_tokens=800,
               task_id=None, max_uses=3):
    """Let Claude search the web (Anthropic's web search tool). Returns
    {"text": the reply, "pages": [{"url", "title"}]}. Each search is recorded as a cost on
    the board. Raises whatever the API raises if the tool isn't available on this key."""
    tool = {**WEB_SEARCH_TOOL, "max_uses": max_uses}
    response = _call(run_id, agent, prompt, system, model, max_tokens, task_id,
                     reserve_extra=SEARCH_COST_USD * max_uses, tools=[tool])
    used = _field(_field(response.usage, "server_tool_use"), "web_search_requests") or 0
    if used:
        board.record_call(run_id, agent, "web-search", int(used), 0, task_id=task_id)
        dashboard.render(run_id)
    text = "".join(b.text for b in response.content if _field(b, "type") == "text")
    return {"text": text, "pages": search_results(response)}


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
        
"""Gemini as a second, independent reviewer.

Works like llm.ask_json(), but calls Google's Gemini API instead of Claude:
- the same budget check before every call, so Gemini can never take a run over budget;
- the same cost tracking on the task board and dashboard after every call;
- answers arrive as JSON that follows a schema (Gemini's structured output).

Needs GEMINI_API_KEY in .env (a free key from Google AI Studio works).
Set GEMINI_MODEL in .env to use a different model.
"""

import json
import os
import time

import httpx
from dotenv import load_dotenv

import board
import dashboard
import llm

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Used only when MODEL is overloaded (Google's 503 "high demand") after the retries below.
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")
RETRY_WAITS = (5, 15)  # seconds to wait before each retry while Google is busy
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiBusy(RuntimeError):
    """Google kept saying the model is overloaded or rate-limited."""


def _api_key():
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("No GEMINI_API_KEY in .env. Get a free key from Google AI Studio.")
    return key


def _thinking(model):
    """Keep Gemini's hidden thinking short: it's billed as output and slows the reply.
    Gemini 2.5 models take a token budget (0 = off); newer models take a level."""
    if model.startswith("gemini-2.5"):
        return {"thinkingBudget": 0}
    return {"thinkingLevel": "low"}


def _message(response):
    try:
        return response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return response.text[:300]


def _post(model, body):
    """Send one request. While Google is busy or rate-limiting (the free tier allows
    roughly 10 requests a minute), wait and retry: after 5 seconds, then 15."""
    url = API_URL.format(model=model)
    headers = {"x-goog-api-key": _api_key(), "Content-Type": "application/json"}
    for wait in (*RETRY_WAITS, None):
        response = httpx.post(url, headers=headers, json=body, timeout=90.0)
        if response.status_code == 200:
            return response.json()
        if response.status_code not in (429, 500, 503):
            raise RuntimeError(f"Gemini returned {response.status_code}: {_message(response)}")
        if wait is None:
            raise GeminiBusy(f"Gemini returned {response.status_code}: {_message(response)}")
        time.sleep(wait)


def ask_json(run_id, agent, prompt, schema, *, system=None, model=MODEL,
             max_tokens=2000, task_id=None):
    """Ask Gemini once on behalf of an agent and return its answer as a dict.
    If the model stays overloaded through the retries, ask the fallback model instead."""
    try:
        return _ask_json(run_id, agent, prompt, schema, system, model, max_tokens, task_id)
    except GeminiBusy as busy:
        if not FALLBACK_MODEL or FALLBACK_MODEL == model:
            raise
        print(f"  {model} is busy ({busy}). Asking {FALLBACK_MODEL} instead.")
        return _ask_json(run_id, agent, prompt, schema, system, FALLBACK_MODEL, max_tokens, task_id)


def _ask_json(run_id, agent, prompt, schema, system, model, max_tokens, task_id):
    budget = llm._budget(run_id)
    spent = board.run_cost(run_id)
    in_chars = len(prompt) + len(system or "") + len(json.dumps(schema))
    worst_case = board.cost_of(model, in_chars // 3, max_tokens)  # thinking counts as output
    if spent + worst_case > budget:
        money = dashboard._money
        llm._stop(run_id, f"{agent} needs up to {money(worst_case)}, "
                          f"but only {money(budget - spent)} is left.")

    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
            "maxOutputTokens": max_tokens,
            "thinkingConfig": _thinking(model),
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    data = _post(model, body)

    usage = data.get("usageMetadata", {})
    output_tokens = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
    board.record_call(run_id, agent, model, usage.get("promptTokenCount", 0),
                      output_tokens, task_id=task_id)
    dashboard.render(run_id)

    candidate = (data.get("candidates") or [{}])[0]
    if candidate.get("finishReason") == "MAX_TOKENS":
        raise ValueError("Gemini's answer was cut off before it finished.")
    text = "".join(part.get("text", "") for part in candidate.get("content", {}).get("parts", [])
                   if not part.get("thought"))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ValueError(f"Gemini's reply was not valid JSON: {text[:200]!r}")

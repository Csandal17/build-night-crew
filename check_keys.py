"""Check every API key before you rely on it. Run this first on the night.

- Anthropic: one tiny Claude call (a fraction of a cent).
- Gemini: confirms the model in gemini.py exists for your key, then one tiny
  structured-JSON call in exactly the format the second reviewer uses (free on a free key).
- ElevenLabs: reads your character allowance and checks both briefing voices exist.
  No audio is made, so nothing is charged.

Run: uv run check_keys.py
"""

import os

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv

import gemini
import llm
from brief import VOICES

load_dotenv()

results = []


def report(name, ok, detail):
    results.append((name, ok))
    mark = {True: "OK  ", False: "FAIL", None: "SKIP"}[ok]
    print(f"{mark}  {name}: {detail}")


def check_anthropic():
    if not os.getenv("ANTHROPIC_API_KEY"):
        return report("Anthropic", None, "no ANTHROPIC_API_KEY in .env")
    reply = Anthropic(timeout=30.0).messages.create(
        model=llm.FAST, max_tokens=5, messages=[{"role": "user", "content": "Say OK."}])
    report("Anthropic", True, f"{llm.FAST} replied ({reply.usage.input_tokens} tokens in, "
                              f"{reply.usage.output_tokens} out)")


def check_gemini():
    try:
        key = gemini._api_key()
    except RuntimeError as missing:
        return report("Gemini", None, str(missing))
    listing = httpx.get("https://generativelanguage.googleapis.com/v1beta/models",
                        headers={"x-goog-api-key": key}, params={"pageSize": 1000}, timeout=30.0)
    if listing.status_code != 200:
        return report("Gemini", False, f"listing models returned {listing.status_code}: "
                                       f"{listing.text[:200]}")
    names = [m["name"].removeprefix("models/") for m in listing.json().get("models", [])]
    if gemini.MODEL not in names:
        flash = ", ".join(n for n in names if "flash" in n) or "none"
        return report("Gemini", False, f"{gemini.MODEL} isn't available to this key. "
                                       f"Flash models it can use: {flash}. "
                                       "Set GEMINI_MODEL in .env to one of them.")
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    data = gemini._post(gemini.MODEL, {
        "contents": [{"role": "user", "parts": [{"text": "Reply with ok set to true."}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema,
                             "maxOutputTokens": 500, "thinkingConfig": gemini._thinking(gemini.MODEL)},
    })
    parts = data["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    usage = data.get("usageMetadata", {})
    fallback = (f"fallback {gemini.FALLBACK_MODEL} available" if gemini.FALLBACK_MODEL in names
                else f"fallback {gemini.FALLBACK_MODEL} NOT available, so no backup if Google is busy")
    report("Gemini", True, f"{gemini.MODEL} answered {text.strip()} in JSON "
                           f"({usage.get('promptTokenCount', 0)} tokens in, "
                           f"{usage.get('candidatesTokenCount', 0)} out, "
                           f"{usage.get('thoughtsTokenCount', 0)} thinking); {fallback}")


def check_elevenlabs():
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        return report("ElevenLabs", None, "no ELEVENLABS_API_KEY in .env")
    headers = {"xi-api-key": key}
    voices = []
    for role, voice_id in VOICES.items():
        r = httpx.get(f"https://api.elevenlabs.io/v1/voices/{voice_id}", headers=headers, timeout=30.0)
        if r.status_code != 200:
            return report("ElevenLabs", False, f"{role} voice {voice_id} returned {r.status_code}: "
                                               f"{r.text[:200]}")
        voices.append(f"{role} = {r.json().get('name', voice_id)}")
    allowance = ""
    sub = httpx.get("https://api.elevenlabs.io/v1/user/subscription", headers=headers, timeout=30.0)
    if sub.status_code == 200:
        s = sub.json()
        used, limit = s.get("character_count"), s.get("character_limit")
        if isinstance(used, int) and isinstance(limit, int):
            allowance = f", {used:,} of {limit:,} characters used this period"
    report("ElevenLabs", True, ", ".join(voices) + allowance)


if __name__ == "__main__":
    for name, check in (("Anthropic", check_anthropic), ("Gemini", check_gemini),
                        ("ElevenLabs", check_elevenlabs)):
        try:
            check()
        except Exception as error:
            report(name, False, str(error)[:300])
    failed = [name for name, ok in results if ok is False]
    print("\nAll set." if not failed else f"\nFix before the night: {', '.join(failed)}.")

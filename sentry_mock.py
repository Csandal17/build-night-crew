"""Mock Sentry: makes the demo service fail for real, then sends the alert a real Sentry would.

What is real: the exception. The demo service's code is run, it raises, and the stack
trace, source lines, release and times all come from that failure.
What is simulated: Sentry itself. This script builds a Sentry-shaped alert from the real
exception, signs it the way Sentry signs webhooks (HMAC-SHA256 of the body), and posts it to
sentry_webhook.py. The request carries a header saying it is simulated, and the receiver
repeats that in the crew's inbox, so the demo never passes it off as a real Sentry event.

The payload shape follows Sentry's published webhook formats as summarised in their docs and
integration guides; it has NOT been checked against a delivery from a real Sentry project.
To use the real thing later, point a Sentry internal integration (or a service hook) at the
receiver: it understands the same shapes.

Run:  uv run sentry_mock.py                      (fail, build the alert, post it)
      uv run sentry_mock.py --dry-run            (print the alert, send nothing)
      uv run sentry_mock.py --save alert.json    (also keep the alert on disk)
      uv run sentry_mock.py --url http://localhost:8787/sentry --repo ../demo-service
Set SENTRY_CLIENT_SECRET (the same value on both sides) to sign the request.
"""

import hashlib
import hmac
import json
import linecache
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_REPO = HERE.parent / "demo-service"
DEFAULT_URL = "http://127.0.0.1:8787/sentry"
CONTEXT_LINES = 5
ISSUE_ID = "4821"


def sign(body, secret):
    """The signature Sentry sends: hex HMAC-SHA256 of the raw request body."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _facts(repo):
    return json.loads((Path(repo) / ".demo" / "facts.json").read_text(encoding="utf-8"))


def _import_service(repo):
    """Import the demo service fresh from its folder."""
    for name in [m for m in sys.modules if m == "service" or m.startswith("service.")]:
        del sys.modules[name]
    sys.path.insert(0, str(repo))
    try:
        from service import app  # noqa: PLC0415
        return app
    finally:
        sys.path.remove(str(repo))


def _frames(tb, repo):
    """Sentry-style stack frames, oldest call first, with the lines of source around each."""
    frames = []
    for frame, lineno in traceback.walk_tb(tb):
        filename = Path(frame.f_code.co_filename)
        try:
            relative = filename.resolve().relative_to(Path(repo).resolve()).as_posix()
        except ValueError:
            continue                                   # skip anything outside the service
        lines = [linecache.getline(str(filename), n).rstrip("\n")
                 for n in range(max(1, lineno - CONTEXT_LINES), lineno + CONTEXT_LINES + 1)]
        before = lineno - max(1, lineno - CONTEXT_LINES)
        frames.append({
            "filename": relative, "abs_path": f"/srv/app/{relative}",
            "module": relative[:-3].replace("/", "."), "function": frame.f_code.co_name,
            "lineno": lineno, "in_app": True, "context_line": lines[before],
            "pre_context": lines[:before], "post_context": lines[before + 1:],
        })
    return frames


def capture(repo):
    """Run the failing request against the real demo service. Returns (event, issue)."""
    repo = Path(repo).resolve()
    if not (repo / ".demo" / "facts.json").exists():
        raise SystemExit(f"{repo} isn't a built demo service. Run: uv run make_demo_service.py")
    facts = _facts(repo)
    app = _import_service(repo)
    order = {"id": facts["first_failing_order"], "currency": "GBP",
             "items": [{"price": 24.5, "qty": 2}, {"price": 9.99, "qty": 1}]}
    try:
        app.handle_request(order)
    except Exception as error:                          # the real failure
        frames = _frames(error.__traceback__, repo)
        kind, value = type(error).__name__, repr(error.args[0]) if error.args else ""
    else:
        raise SystemExit("The demo service did not fail. Rebuild it with make_demo_service.py --force.")

    crash = frames[-1]
    when = datetime.fromisoformat(facts["first_failure_at"].rstrip("Z")).replace(tzinfo=timezone.utc)
    culprit = f"{crash['module']} in {crash['function']}"
    title = f"{kind}: {value}"
    base = "https://sentry.example.com"
    event = {
        "event_id": uuid.UUID(int=int(ISSUE_ID) * 7919).hex, "project": 4507,
        "release": facts["release"], "environment": "production", "platform": "python",
        "level": "error", "logger": "checkout", "culprit": culprit, "title": title,
        "datetime": when.isoformat().replace("+00:00", "Z"), "timestamp": when.timestamp(),
        "tags": [["environment", "production"], ["handled", "no"], ["level", "error"],
                 ["release", facts["release"]], ["currency", "GBP"]],
        "exception": {"values": [{"type": kind, "value": value, "module": "builtins",
                                  "mechanism": {"type": "generic", "handled": False},
                                  "stacktrace": {"frames": frames}}]},
        "request": {"url": "https://shop.example.com/api/orders", "method": "POST"},
        "issue_id": ISSUE_ID, "issue_url": f"{base}/api/0/issues/{ISSUE_ID}/",
        "web_url": f"{base}/organizations/demo/issues/{ISSUE_ID}/events/",
    }
    issue = {"id": ISSUE_ID, "shortId": "CHECKOUT-1A", "title": title, "culprit": culprit,
             "level": "error", "status": "unresolved", "count": str(facts["failed_gbp_orders"]),
             "firstSeen": event["datetime"], "project": {"id": "4507", "slug": "checkout-service"}}
    return event, issue


def build_alert(event, issue):
    """The webhook body: an issue alert that fired, in the shape Sentry's integrations receive."""
    return {
        "action": "triggered",
        "installation": {"uuid": "00000000-0000-4000-8000-00000000d3m0"},
        "data": {"event": event, "triggered_rule": "Production errors in checkout-service",
                 "issue": issue},
        "actor": {"type": "application", "id": "sentry", "name": "Sentry (simulated)"},
    }


def post(url, alert, secret=None):
    body = json.dumps(alert).encode("utf-8")
    headers = {"Content-Type": "application/json", "Request-ID": uuid.uuid4().hex,
               "Sentry-Hook-Resource": "event_alert", "Sentry-Hook-Timestamp": str(int(time.time())),
               "X-Simulated": "mock-sentry"}
    if secret:
        headers["Sentry-Hook-Signature"] = sign(body, secret)
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, response.read().decode("utf-8")


def main(argv):
    def option(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    repo = Path(option("--repo", DEFAULT_REPO)).expanduser()
    event, issue = capture(repo)
    alert = build_alert(event, issue)
    if option("--save"):
        Path(option("--save")).write_text(json.dumps(alert, indent=2), encoding="utf-8")
    crash = event["exception"]["values"][0]["stacktrace"]["frames"][-1]
    print(f"The demo service failed for real: {issue['title']} at "
          f"{crash['filename']}:{crash['lineno']} in {crash['function']} (release {event['release']}).")
    if "--dry-run" in argv:
        print(json.dumps(alert, indent=2))
        return 0
    url = option("--url", DEFAULT_URL)
    try:
        status, text = post(url, alert, os.getenv("SENTRY_CLIENT_SECRET"))
    except (urllib.error.URLError, OSError) as problem:
        print(f"Couldn't reach {url} ({problem}). Is sentry_webhook.py running?")
        return 1
    print(f"Alert sent to {url}: {status} {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

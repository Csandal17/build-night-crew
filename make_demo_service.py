"""Build the demo "production" service that the Sentry mock will make fail.

It is a tiny checkout service with a real git history, and a bug that really raises:

  c1  Initial checkout service
  c2  Add disk usage warning to health check        <- harmless, but noisy in the logs
  c3  Add bulk-discount pricing                      <- harmless
  c4  Speed up pricing: cache exchange rates         <- the culprit: GBP is left out of the cache

After c4 is deployed, every order in GBP raises KeyError: 'GBP'. USD and EUR orders are fine.
A log file with the incident in it is written next to the code (untracked, so the history
stays clean). Everything is deterministic, so the demo behaves the same every time.

This is a SIMULATION for the hackathon demo: invented service, invented orders.

Run:  uv run make_demo_service.py                  (creates ../demo-service)
      uv run make_demo_service.py /some/folder     (somewhere else)
      uv run make_demo_service.py --force          (rebuild a folder this script made before)
"""

import json
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

MARKER = ".demo-service"   # proves a folder was made by this script, so --force can't delete anything else
INCIDENT_DAY = "2026-10-22"
DEPLOY_TIME = "14:02:00"

FILES = {}

FILES["service/__init__.py"] = ""
FILES["service/config.py"] = '''"""Service settings."""

SUPPORTED_CURRENCIES = ["USD", "EUR", "GBP"]
DB_POOL_SIZE = 10
'''
FILES["service/pricing.py:v1"] = '''"""Currency conversion for checkout totals."""

RATES = {"USD": 1.0, "EUR": 0.92, "GBP": 0.79}   # units of currency per US dollar


def fetch_rates():
    return dict(RATES)


def convert(amount_usd, currency):
    rates = fetch_rates()
    return round(amount_usd * rates.get(currency, 1.0), 2)
'''
FILES["service/pricing.py:v4"] = '''"""Currency conversion for checkout totals."""

RATES = {"USD": 1.0, "EUR": 0.92, "GBP": 0.79}   # units of currency per US dollar

_RATE_CACHE = {}


def _warm_cache():
    """Load the rates once at start-up instead of on every order."""
    for code in ("USD", "EUR"):          # TODO: add the rest
        _RATE_CACHE[code] = RATES[code]


_warm_cache()


def convert(amount_usd, currency):
    return round(amount_usd * _RATE_CACHE[currency], 2)
'''
FILES["service/checkout.py:v1"] = '''"""Work out what an order costs."""

from service import pricing


def total(order):
    subtotal = sum(item["price"] * item["qty"] for item in order["items"])
    return pricing.convert(subtotal, order["currency"])
'''
FILES["service/checkout.py:v3"] = '''"""Work out what an order costs."""

from service import pricing

BULK_THRESHOLD = 10       # items
BULK_DISCOUNT = 0.05


def apply_bulk_discount(subtotal, items):
    if sum(item["qty"] for item in items) >= BULK_THRESHOLD:
        return subtotal * (1 - BULK_DISCOUNT)
    return subtotal


def total(order):
    subtotal = sum(item["price"] * item["qty"] for item in order["items"])
    subtotal = apply_bulk_discount(subtotal, order["items"])
    return pricing.convert(subtotal, order["currency"])
'''
FILES["service/app.py:v1"] = '''"""The order endpoint."""

import logging

from service import checkout

log = logging.getLogger("checkout")


def handle_request(order):
    log.info("order received id=%s currency=%s", order["id"], order["currency"])
    amount = checkout.total(order)
    log.info("order charged id=%s amount=%s %s", order["id"], amount, order["currency"])
    return {"order_id": order["id"], "charged": amount, "currency": order["currency"]}
'''
FILES["service/app.py:v2"] = '''"""The order endpoint."""

import logging
import shutil

from service import checkout

log = logging.getLogger("checkout")


def health_check(path="/var/data"):
    used = shutil.disk_usage(path)
    percent = round(100 * used.used / used.total)
    if percent > 75:
        log.warning("health disk usage %s%% on %s", percent, path)
    return {"ok": True, "disk_percent": percent}


def handle_request(order):
    log.info("order received id=%s currency=%s", order["id"], order["currency"])
    amount = checkout.total(order)
    log.info("order charged id=%s amount=%s %s", order["id"], amount, order["currency"])
    return {"order_id": order["id"], "charged": amount, "currency": order["currency"]}
'''

COMMITS = [
    ("Initial checkout service", "2026-10-19T10:00:00",
     {"service/__init__.py": "service/__init__.py", "service/config.py": "service/config.py",
      "service/pricing.py": "service/pricing.py:v1", "service/checkout.py": "service/checkout.py:v1",
      "service/app.py": "service/app.py:v1"}),
    ("Add disk usage warning to health check", "2026-10-20T16:30:00",
     {"service/app.py": "service/app.py:v2"}),
    ("Add bulk-discount pricing", "2026-10-21T11:15:00",
     {"service/checkout.py": "service/checkout.py:v3"}),
    ("Speed up pricing: cache exchange rates", f"{INCIDENT_DAY}T13:58:00",
     {"service/pricing.py": "service/pricing.py:v4"}),
]


def _git(folder, *args, when=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Demo Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
           "GIT_COMMITTER_NAME": "Demo Dev", "GIT_COMMITTER_EMAIL": "dev@example.com"}
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{when}+00:00"
    result = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def make_logs(folder, release):
    """A deterministic production log from 13:50 to 14:20 with the incident in it.
    Returns the facts the demo is built on (also saved to .demo/facts.json)."""
    rng = random.Random(42)
    start = datetime.fromisoformat(f"{INCIDENT_DAY}T13:50:00")
    deploy = datetime.fromisoformat(f"{INCIDENT_DAY}T{DEPLOY_TIME}")
    end = datetime.fromisoformat(f"{INCIDENT_DAY}T14:20:00")
    lines, orders = [], {"ok": {"USD": 0, "EUR": 0, "GBP": 0}, "failed": {"GBP": 0}}
    first_failure = None

    def stamp(moment):
        return f"{moment:%Y-%m-%d %H:%M:%S},{rng.randint(0, 999):03d}"

    events = []
    minute = start
    while minute <= end:               # the disk warning has been repeating every minute for days
        events.append((minute + timedelta(seconds=5), "WARNING health disk usage 81% on /var/data"))
        minute += timedelta(minutes=1)
    events.append((deploy - timedelta(seconds=5), f"INFO deploy release {release} rolled out to production"))
    events.append((deploy, "INFO pricing cache warm-up finished in 412ms"))
    for offset in (190, 214, 247):     # unrelated slow database queries
        events.append((deploy + timedelta(seconds=offset),
                       "WARNING slow query 1.8s on orders_by_user (customer lookup)"))

    moment, order_number = start, 1000
    while moment < end:
        moment += timedelta(seconds=rng.randint(8, 32))
        order_number += 1
        currency = rng.choices(["USD", "EUR", "GBP"], weights=[50, 30, 20])[0]
        order_id = f"ord-{order_number}"
        events.append((moment, f"INFO order received id={order_id} currency={currency}"))
        if moment >= deploy and currency == "GBP":
            orders["failed"]["GBP"] += 1
            first_failure = first_failure or (order_id, moment)
            events.append((moment + timedelta(milliseconds=40),
                           f"ERROR unhandled exception processing order id={order_id} "
                           f"currency={currency}: KeyError: 'GBP' (service/pricing.py, convert)"))
        else:
            orders["ok"][currency] += 1
            events.append((moment + timedelta(milliseconds=120),
                           f"INFO order charged id={order_id} currency={currency}"))
    events.sort(key=lambda item: item[0])
    for when, text in events:
        level, _, rest = text.partition(" ")
        lines.append(f"{stamp(when)} {level:<7} checkout {rest}")

    (folder / "logs").mkdir(exist_ok=True)
    (folder / "logs" / "app.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    facts = {
        "release": release, "deployed_at": f"{INCIDENT_DAY}T{DEPLOY_TIME}Z",
        "culprit_commit_subject": COMMITS[-1][0],
        "first_failing_order": first_failure[0], "first_failure_at": first_failure[1].isoformat() + "Z",
        "failed_gbp_orders": orders["failed"]["GBP"], "ok_orders_by_currency": orders["ok"],
        "red_herrings": ["disk usage warning repeats every minute and predates the incident",
                         "three slow queries after the deploy are unrelated to the error"],
    }
    (folder / ".demo").mkdir(exist_ok=True)
    (folder / ".demo" / "facts.json").write_text(json.dumps(facts, indent=2), encoding="utf-8")
    return facts


def build(folder, force=False):
    folder = Path(folder).expanduser().resolve()
    if folder.exists():
        if not force:
            raise SystemExit(f"{folder} already exists. Use --force to rebuild a demo-service folder.")
        if not (folder / MARKER).exists():
            raise SystemExit(f"{folder} wasn't made by this script, so --force won't delete it.")
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    (folder / MARKER).write_text("made by make_demo_service.py\n", encoding="utf-8")
    (folder / ".gitignore").write_text(".demo-service\n__pycache__/\n", encoding="utf-8")
    (folder / "README.md").write_text(
        "# demo-service\n\nA simulated checkout service for the build-night-crew demo. "
        "Invented code and data.\n", encoding="utf-8")
    _git(folder, "init", "-q", "-b", "main")
    _git(folder, "add", ".gitignore", "README.md")
    for subject, when, changes in COMMITS:
        for target, source in changes.items():
            path = folder / target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(FILES[source], encoding="utf-8")
            _git(folder, "add", target)
        _git(folder, "commit", "-q", "-m", subject, when=when)
    release = _git(folder, "rev-parse", "--short", "HEAD")
    facts = make_logs(folder, release)
    return folder, facts


if __name__ == "__main__":
    args = sys.argv[1:]
    force = "--force" in args
    targets = [a for a in args if not a.startswith("--")]
    default = Path(__file__).resolve().parent.parent / "demo-service"
    folder, facts = build(targets[0] if targets else default, force=force)
    print(f"Demo service built in {folder}")
    print(f"  release {facts['release']} deployed at {facts['deployed_at']}")
    print(f"  {facts['failed_gbp_orders']} GBP orders fail with KeyError: 'GBP' after the deploy")
    print("Next: uv run sentry_webhook.py   then   uv run sentry_mock.py")

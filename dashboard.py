"""Live cost dashboard for the crew.

Reads the task board and writes dashboard.html plus dashboard_data.js.
The open page checks dashboard_data.js every few seconds and swaps in the
new content without reloading, so it updates smoothly while the crew runs.
"""

import json
import sys
import webbrowser
from datetime import datetime
from html import escape
from pathlib import Path

import board

OUT_PATH = Path(__file__).parent / "dashboard.html"
DATA_PATH = Path(__file__).parent / "dashboard_data.js"
REFRESH_SECONDS = 3

STATUS_COLOURS = {
    "done": "#7FB77E",
    "completed": "#7FB77E",
    "running": "#F2B33D",
    "queued": "#C5D1DE",
    "waiting_review": "#F2B33D",
    "revising": "#F2B33D",
    "needs_human": "#E98A4E",
    "failed": "#E98A4E",
    "stopped_budget": "#E98A4E",
}

CSS = """
:root {
  --navy: #1B2A4A; --ink: #14213D; --text: #2E3A52; --muted: #5A6F96;
  --page: #EEF3F7; --hero-text: #F4F7FB; --track: #D8E2EC; --bar: #8FB3D3;
  --sun: #F2B33D; --card: #FFFFFF;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font-family: 'Source Serif 4', Georgia, 'Times New Roman', serif;
  font-size: 18px; line-height: 1.6; }
h1, h2, h3, .num, .label { font-family: 'Bricolage Grotesque', 'Avenir Next', 'Segoe UI', sans-serif; }
.wrap { max-width: 1040px; margin: 0 auto; padding: 0 clamp(16px, 5vw, 48px); }
.hero { background: var(--navy); color: var(--hero-text); padding: 48px 0 56px; }
.eyebrow { color: #B9C6DA; font-size: 16px; margin: 0 0 8px; }
.hero h1 { font-size: clamp(28px, 4vw, 40px); font-weight: 700; margin: 0 0 32px; line-height: 1.2; }
.big { display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }
.big .num { font-size: clamp(64px, 12vw, 120px); font-weight: 800; line-height: 1; letter-spacing: -0.02em; }
.big .of { font-size: 20px; color: #DCE4EF; }
.budget { margin-top: 24px; height: 14px; background: #2E3A52; border-radius: 8px; overflow: hidden; }
.budget span { display: block; height: 100%; background: var(--sun); border-radius: 8px; }
.pill { display: inline-block; font-family: 'Bricolage Grotesque', sans-serif; font-size: 14px;
  font-weight: 700; padding: 2px 12px; border-radius: 22px; color: var(--ink); }
section { padding: 48px 0 0; }
h2 { font-size: 32px; font-weight: 800; margin: 0 0 24px; }
.agent { display: grid; grid-template-columns: 1fr auto; gap: 6px 16px; margin-bottom: 20px; }
.agent .label { font-weight: 700; font-size: 18px; }
.agent .num { font-weight: 700; font-size: 18px; }
.agent .bar { grid-column: 1 / -1; height: 18px; background: var(--track); border-radius: 8px; overflow: hidden; }
.agent .bar span { display: block; height: 100%; background: var(--bar); border-radius: 8px; }
.agent .bar span.top { background: var(--sun); }
.agent .tokens { grid-column: 1 / -1; color: var(--muted); font-size: 15px; margin-top: -2px; }
.tasks { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 24px; }
.task { border-top: 3px solid var(--bar); padding-top: 14px; }
.task h3 { font-size: 20px; margin: 0 0 6px; }
.task p { margin: 6px 0 0; color: var(--text); font-size: 16px; }
.meta { color: var(--muted); font-size: 15px; }
.runs { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 16px; }
.run { background: var(--card); border-radius: 8px; padding: 16px 18px; }
.run.current { outline: 3px solid var(--sun); }
.run .num { font-size: 32px; font-weight: 800; display: block; }
footer { margin-top: 64px; background: var(--navy); color: #DCE4EF; padding: 24px 0; font-size: 15px; }
"""


def _money(value):
    if value >= 1:
        return f"${value:.2f}"
    if 0 < value < 0.0001:
        return f"${value:.5f}"  # tiny costs stay visible instead of rounding to $0.0000
    return f"${value:.4f}"

def _pill(status):
    colour = STATUS_COLOURS.get(status, "#C5D1DE")
    return f'<span class="pill" style="background:{colour}">{escape(status.replace("_", " "))}</span>'


def _task_costs(run_id):
    rows = board._fetch(
        "SELECT task_id, SUM(cost_usd) AS cost FROM calls WHERE run_id = ? GROUP BY task_id",
        (run_id,),
    )
    return {row["task_id"]: row["cost"] for row in rows}


def _body(run_id=None):
    """Build the page content (everything inside the body) for one run."""
    runs = board.all_runs()
    if not runs:
        return "<div class='wrap'><p>No runs yet.</p></div>"
    run_id = run_id or runs[-1]["id"]
    s = board.summary(run_id)
    run, total = s["run"], s["total_usd"]
    budget = run["budget_usd"]
    used_pct = min(100, total / budget * 100) if budget else 0
    task_costs = _task_costs(run_id)

    top_cost = max((a["cost_usd"] for a in s["by_agent"]), default=0)
    agents_html = "".join(
        f"""<div class="agent">
          <span class="label">{escape(a['agent'])}</span><span class="num">{_money(a['cost_usd'])}</span>
          <div class="bar"><span class="{'top' if a['cost_usd'] == top_cost else ''}"
               style="width:{(a['cost_usd'] / top_cost * 100) if top_cost else 0:.1f}%"></span></div>
          <span class="tokens">{a['input_tokens']:,} tokens in, {a['output_tokens']:,} out</span>
        </div>"""
        for a in s["by_agent"]
    ) or "<p class='meta'>No model calls yet.</p>"

    tasks_html = "".join(
        f"""<div class="task">
          <h3>{escape(t['title'])}</h3>
          {_pill(t['status'])}
          <p class="meta">{escape(t['owner'] or 'unassigned')} · attempts {t['attempts']} · {_money(task_costs.get(t['id'], 0))}</p>
          {f"<p>{escape(t['result'][:220])}</p>" if t['result'] else ''}
          {f"<p class='meta'>Review: {escape(t['review_notes'][:160])}</p>" if t['review_notes'] else ''}
        </div>"""
        for t in s["tasks"]
    ) or "<p class='meta'>No tasks yet.</p>"

    runs_html = "".join(
        f"""<div class="run {'current' if r['id'] == run_id else ''}">
            <span class="meta">Run {r['id']} · {escape(r['status'].replace('_', ' '))}</span>
          <span class="num">{_money(r['cost_usd'])}</span>
        </div>"""
        for r in reversed(runs[-8:])
    )

    return f"""<div class="hero"><div class="wrap">
  <p class="eyebrow">Run {run['id']} · {_pill(run['status'])}</p>
  <h1>{escape(run['goal'])}</h1>
  <div class="big"><span class="num">{_money(total)}</span>
    <span class="of">of {_money(budget)} budget · {used_pct:.0f}% used</span></div>
  <div class="budget"><span style="width:{used_pct:.1f}%"></span></div>
</div></div>
<div class="wrap">
  <section><h2>Cost by agent</h2>{agents_html}</section>
  <section><h2>Task board</h2><div class="tasks">{tasks_html}</div></section>
  <section><h2>Every run</h2><div class="runs">{runs_html}</div></section>
</div>
<footer><div class="wrap">build-night-crew · updated {datetime.now():%H:%M:%S} · live</div></footer>"""


def render(run_id=None):
    """Write the latest content to both files. Call this after every step."""
    body = _body(run_id)
    DATA_PATH.write_text(f"window.renderDash({json.dumps(body)});", encoding="utf-8")
    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crew Cost Board</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap">
<style>{CSS}</style>
<style>
  #live {{ position: fixed; right: 20px; bottom: 20px; z-index: 10; display: flex; align-items: center;
    gap: 10px; background: #FFFFFF; color: #14213D; border-radius: 22px; padding: 8px 16px;
    font-family: 'Bricolage Grotesque', sans-serif; font-size: 15px; font-weight: 700;
    box-shadow: 0 8px 24px rgba(5, 12, 30, 0.25); }}
  #live .dot {{ width: 10px; height: 10px; border-radius: 50%; background: #7FB77E; }}
  #live.ok .dot {{ animation: pulse 2s ease-out infinite; }}
  #live.down .dot {{ background: #E98A4E; }}
  @keyframes pulse {{ 0% {{ box-shadow: 0 0 0 0 rgba(127, 183, 126, 0.7); }}
    100% {{ box-shadow: 0 0 0 10px rgba(127, 183, 126, 0); }} }}
  .changed .big .num {{ animation: glow 1.4s ease-out; }}
  @keyframes glow {{ 0% {{ color: #F2B33D; }} 100% {{ color: #F4F7FB; }} }}
  @media (prefers-reduced-motion: reduce) {{
    #live.ok .dot, .changed .big .num {{ animation: none; }} }}
</style>
</head><body>
<div id="app">{body}</div>
<div id="live" class="ok"><span class="dot"></span><span id="live-text">Live</span></div>
<script>
  // Check for new content every few seconds and swap it in without reloading,
  // so the page never flashes and keeps its scroll position.
  var app = document.getElementById("app");
  var badge = document.getElementById("live");
  var badgeText = document.getElementById("live-text");
  var current = null;
  var lastOk = Date.now();
  var failing = false;

  window.renderDash = function (html) {{
    if (html === current) return;
    var firstLoad = current === null;
    current = html;
    app.innerHTML = html;
    if (!firstLoad) {{
      app.classList.remove("changed");
      void app.offsetWidth;  // restart the glow animation
      app.classList.add("changed");
    }}
  }};

  setInterval(function () {{
    var s = document.createElement("script");
    s.src = "{DATA_PATH.name}?t=" + Date.now();
    s.onload = function () {{ lastOk = Date.now(); failing = false; s.remove(); }};
    s.onerror = function () {{ failing = true; s.remove(); }};
    document.body.appendChild(s);
  }}, {REFRESH_SECONDS * 1000});

  // Update the badge every second so it visibly ticks, even when nothing changed.
  setInterval(function () {{
    var secs = Math.round((Date.now() - lastOk) / 1000);
    badge.className = failing ? "down" : "ok";
    badgeText.textContent = failing
      ? "Offline · last data " + secs + "s ago"
      : "Live · checked " + secs + "s ago";
  }}, 1000);
</script>
</body></html>"""
    OUT_PATH.write_text(page, encoding="utf-8")
    return OUT_PATH

if __name__ == "__main__":
    path = render(int(sys.argv[1]) if len(sys.argv) > 1 else None)
    print(f"Dashboard written to {path.name}")
    webbrowser.open(path.resolve().as_uri())

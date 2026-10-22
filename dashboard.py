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
LEDE = ("A coordinator splits the goal, workers do the tasks, and a reviewer "
        "checks every one before it counts.")
LONG_GOAL_CHARS = 80  # longer titles get a smaller headline clamped to 3 lines

STATUS_COLOURS = {
    "done": "#7FB77E",
    "completed": "#7FB77E",
    "running": "#F2B33D",
    "queued": "#C5D1DE",
    "waiting": "#C5D1DE",
    "interrupted": "#E98A4E",
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
.hero { background: var(--navy); color: var(--hero-text); padding: clamp(40px, 6vw, 72px) 0 clamp(48px, 7vw, 88px); }
.hero-grid { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 0.85fr);
  gap: clamp(32px, 5vw, 64px); align-items: center; }
@media (max-width: 860px) { .hero-grid { grid-template-columns: 1fr; } }
.eyebrow { color: #B9C6DA; font-size: 16px; margin: 0 0 12px; }
.hero h1 { font-size: clamp(32px, 4.6vw, 52px); font-weight: 800; margin: 0 0 16px;
  line-height: 1.02; letter-spacing: -0.02em; }
.hero h1.long { font-size: clamp(24px, 3vw, 32px); display: -webkit-box; -webkit-box-orient: vertical;
  -webkit-line-clamp: 3; line-clamp: 3; overflow: hidden; }
.lede { color: #DCE4EF; font-size: clamp(17px, 1.8vw, 19px); margin: 0 0 20px; max-width: 30em; }
.tags { display: flex; flex-wrap: wrap; gap: 10px; }
.tag { display: inline-flex; align-items: center; min-height: 36px; padding: 0 16px; border: 1px solid #5A6F96;
  border-radius: 22px; color: #F4F7FB; font-size: 15px; }
.receipt { justify-self: center; width: 100%; max-width: 440px; background: var(--card); color: var(--ink);
  border-radius: 6px; padding: 26px 28px 22px; transform: rotate(-1.5deg);
  box-shadow: 0 24px 48px rgba(5, 12, 30, 0.45); }
.receipt .head { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 4px 16px; }
.receipt .head b { font-family: 'Bricolage Grotesque', sans-serif; font-size: 20px; }
.receipt .head span, .receipt .small { font-size: 15px; color: #45526B; }
.receipt hr { border: 0; border-top: 2px dashed #C5D1DE; margin: 16px 0 14px; }
.receipt .spent { font-family: 'Bricolage Grotesque', sans-serif; font-weight: 800; letter-spacing: -0.02em;
  font-size: clamp(40px, 5vw, 56px); line-height: 1; margin: 6px 0 12px; display: block; }
.budget { height: 10px; background: var(--track); border-radius: 8px; overflow: hidden; margin-bottom: 8px; }
.budget span { display: block; height: 100%; background: var(--sun); border-radius: 8px; }
.stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.stats b { font-family: 'Bricolage Grotesque', sans-serif; font-size: clamp(22px, 3vw, 28px); line-height: 1.1; display: block; }
.stats span { display: block; font-size: 14px; line-height: 1.35; color: #45526B; margin-top: 4px; }
.receipt .foot { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 6px 16px; font-size: 15px; }
.receipt .foot strong { font-weight: 600; }
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
.watcher { font-family: 'Bricolage Grotesque', sans-serif; font-weight: 700; color: var(--muted); margin: -12px 0 20px; }
.inbox { display: grid; gap: 12px; }
.event { background: var(--card); border-radius: 8px; padding: 14px 18px;
  display: grid; grid-template-columns: 1fr auto; gap: 4px 16px; align-items: center; }
.event .label { font-weight: 700; }
.event p { grid-column: 1 / -1; margin: 0; color: var(--text); font-size: 16px; }
footer { margin-top: 64px; background: var(--navy); color: #DCE4EF; padding: 24px 0; font-size: 15px; }
"""


def _money(value):
    if value >= 1:
        return f"${value:.2f}"
    if 0 < value < 0.0001:
        return f"${value:.5f}"  # tiny costs stay visible instead of rounding to $0.0000
    return f"${value:.4f}"

def _clip(text, limit):
    """Shorten long text at a word boundary, so cards never end mid-word."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",.:;") + "\u2026"


def _cents(value):
    """Friendly receipt wording: 4.2¢ for small amounts, $1.50 for larger ones."""
    return f"{value * 100:.1f}\u00a2" if value < 1 else f"${value:.2f}"


def _local_time(iso):
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%a %d %b, %H:%M")
    except (TypeError, ValueError):
        return ""


def _pill(status):
    colour = STATUS_COLOURS.get(status, "#C5D1DE")
    return f'<span class="pill" style="background:{colour}">{escape(status.replace("_", " "))}</span>'


def _task_costs(run_id):
    rows = board._fetch(
        "SELECT task_id, SUM(cost_usd) AS cost FROM calls WHERE run_id = ? GROUP BY task_id",
        (run_id,),
    )
    return {row["task_id"]: row["cost"] for row in rows}


def _inbox_html():
    """The always-on inbox (watch.py). Empty when the watcher has never been used."""
    events = board.recent_events()
    if not events:
        return ""
    running = [e for e in events if e["status"] == "running"]
    waiting = sum(1 for e in events if e["status"] == "waiting")
    if running:
        state = f"Working on {running[0]['source']}"
    elif waiting:
        state = f"{waiting} waiting"
    else:
        state = "Waiting for work"
    state += f" · {_money(board.spent_today())} spent today"
    cards = "".join(
        f"""<div class="event">
          <span class="label">{escape(e['source'])}</span>{_pill(e['status'])}
          <p class="meta">attempts {e['attempts']}{f" · run {e['run_id']}" if e['run_id'] else ''}</p>
          <p>{escape(_clip(e['goal'], 200))}</p>
          {f"<p class='meta'>Error: {escape(_clip(e['error'], 200))}</p>" if e['error'] else ''}
        </div>"""
        for e in events
    )
    return f"""<section><h2>Inbox</h2><p class="watcher">{escape(state)}</p>
  <div class="inbox">{cards}</div></section>"""


def _body(run_id=None):
    """Build the page content (everything inside the body) for one run."""
    runs = board.all_runs()
    inbox_html = _inbox_html()
    if not runs:
        return f"<div class='wrap'>{inbox_html}<section><p>No runs yet.</p></section></div>"
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

    def _result_html(t):
        """A held task's last answer never passed review, so it's labelled as unverified."""
        if not t["result"]:
            return ""
        text = escape(_clip(t["result"], 320))
        if t["status"] == "needs_human":
            return f"<p><strong>Not verified.</strong> Last answer, which did not pass review: {text}</p>"
        return f"<p>{text}</p>"

    tasks_html = "".join(
        f"""<div class="task">
          <h3>{escape(t['title'])}</h3>
          {_pill(t['status'])}
          <p class="meta">{escape(t['owner'] or 'unassigned')} · attempts {t['attempts']} · {_money(task_costs.get(t['id'], 0))}</p>
          {_result_html(t)}
          {f"<p class='meta'>Review: {escape(_clip(t['review_notes'], 280))}</p>" if t['review_notes'] else ''}
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

    title, _, tag_text = run["goal"].partition(" \u00b7 ")
    tags_html = "".join(f'<span class="tag">{escape(t)}</span>' for t in tag_text.split(" + ") if t)
    tasks = s["tasks"]
    passed = sum(t["status"] == "done" for t in tasks)
    fixed = sum(t["status"] == "done" and t["attempts"] > 1 for t in tasks)
    held = sum(t["status"] == "needs_human" for t in tasks)
    calls = board._fetch("SELECT COUNT(*) AS n FROM calls WHERE run_id = ?", (run_id,))[0]["n"]
    budget_text = f"${budget:.2f}" if budget >= 0.01 else _money(budget)
    long_title = len(title) > LONG_GOAL_CHARS

    return f"""<div class="hero"><div class="wrap"><div class="hero-grid">
  <div>
    <p class="eyebrow">Run {run['id']} · {_pill(run['status'])}</p>
    <h1{f' class="long" title="{escape(title)}"' if long_title else ''}>{escape(title)}</h1>
    <p class="lede">{escape(LEDE)}</p>
    <div class="tags">{tags_html}</div>
  </div>
  <div class="receipt">
    <div class="head"><b>Run receipt</b><span>{_local_time(run['started_at'])}</span></div>
    <hr>
    <span class="small">Spent so far</span>
    <span class="spent">{_money(total)}</span>
    <div class="budget"><span style="width:{used_pct:.1f}%"></span></div>
    <span class="small">of a {budget_text} budget \u00b7 {used_pct:.0f}% used</span>
    <hr>
    <div class="stats">
      <div><b>{passed}/{len(tasks)}</b><span>Passed review</span></div>
      <div><b>{fixed}</b><span>Caught and fixed</span></div>
      <div><b>{held}</b><span>Held for a human</span></div>
    </div>
    <hr>
    <div class="foot"><span class="small">{calls} model calls</span>
      <strong>{passed} of {len(tasks)} passed for {_cents(total)}</strong></div>
  </div>
</div></div></div>
<div class="wrap">
  {inbox_html}
  <section><h2>Cost by agent</h2>{agents_html}</section>
  <section><h2>Task board</h2><div class="tasks">{tasks_html}</div></section>
  <section><h2>Every run</h2><div class="runs">{runs_html}</div></section>
</div>
<footer><div class="wrap">build-night-crew · updated {datetime.now():%H:%M:%S} · live</div></footer>"""


def _briefing():
    """The newest spoken briefing (brief.py) for the player: its audio file and its script."""
    latest = board.last_briefing()
    if not latest or not latest["audio_path"]:
        return None
    lines = []
    try:
        script = (OUT_PATH.parent / latest["audio_path"]).with_suffix(".json")
        lines = json.loads(script.read_text(encoding="utf-8")).get("lines", [])
    except (OSError, ValueError):
        pass  # briefings made before scripts were saved still get a player
    return {"id": latest["id"], "audio": latest["audio_path"],
            "when": _local_time(latest["created_at"]), "lines": lines}


def _js(value):
    """JSON that's safe to put inside a <script> tag."""
    return json.dumps(value).replace("</", "<\\/")


def render(run_id=None):
    """Write the latest content to both files. Call this after every step."""
    body = _body(run_id)
    briefing = _briefing()
    DATA_PATH.write_text(f"window.renderDash({json.dumps(body)}, {json.dumps(briefing)});",
                         encoding="utf-8")
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
  .changed .spent {{ animation: glow 1.4s ease-out; }}
  @keyframes glow {{ 0% {{ color: #F2B33D; }} 100% {{ color: #14213D; }} }}
  @media (prefers-reduced-motion: reduce) {{
    #live.ok .dot, .changed .spent {{ animation: none; }} }}
  /* The latest spoken briefing. It sits outside #app, so refreshes never cut the audio off. */
  #brief {{ position: fixed; left: 20px; bottom: 20px; z-index: 10; width: min(400px, calc(100vw - 40px));
    background: #FFFFFF; color: #14213D; border-radius: 16px; padding: 14px 16px 12px;
    box-shadow: 0 8px 24px rgba(5, 12, 30, 0.25); }}
  #brief[hidden] {{ display: none; }}
  #brief .head {{ display: flex; align-items: baseline; gap: 10px; font-family: 'Bricolage Grotesque', sans-serif; }}
  #brief .head b {{ font-size: 16px; }}
  #brief-when {{ font-size: 14px; color: #45526B; }}
  #brief-new {{ display: none; margin-left: auto; background: #F2B33D; font-size: 13px; font-weight: 700;
    border-radius: 22px; padding: 1px 10px; }}
  #brief.fresh #brief-new {{ display: inline-block; }}
  #brief audio {{ display: block; width: 100%; margin: 10px 0 6px; }}
  #brief summary {{ cursor: pointer; font-family: 'Bricolage Grotesque', sans-serif; font-size: 14px;
    font-weight: 700; color: #45526B; }}
  #brief ol {{ list-style: none; padding: 0; margin: 8px 0 0; max-height: 40vh; overflow-y: auto;
    font-size: 15px; line-height: 1.45; }}
  #brief li {{ margin: 0 0 8px; }}
  #brief li .who {{ display: block; font-family: 'Bricolage Grotesque', sans-serif; font-size: 13px;
    font-weight: 700; text-transform: capitalize; color: #45526B; }}
  #brief li.reviewer .who {{ color: #8A5A00; }}
  body.has-brief {{ padding-bottom: 170px; }}
  @media (max-width: 640px) {{ #brief {{ left: 12px; right: 12px; width: auto; bottom: 68px; }} }}
</style>
</head><body>
<div id="app">{body}</div>
<div id="live" class="ok"><span class="dot"></span><span id="live-text">Live</span></div>
<section id="brief" aria-label="Latest spoken briefing" hidden>
  <div class="head"><b>Latest briefing</b><span id="brief-when"></span><span id="brief-new">New</span></div>
  <audio id="brief-audio" controls preload="none"></audio>
  <details id="brief-script"><summary>Show script</summary><ol id="brief-lines"></ol></details>
</section>
<script>
  // Check for new content every few seconds and swap it in without reloading,
  // so the page never flashes and keeps its scroll position.
  var app = document.getElementById("app");
  var badge = document.getElementById("live");
  var badgeText = document.getElementById("live-text");
  var current = {_js(body)};  // what's on screen now
  var lastOk = Date.now();
  var failing = false;

  // The briefing player only changes when a new briefing arrives, so playback isn't interrupted.
  var briefBox = document.getElementById("brief");
  var briefAudio = document.getElementById("brief-audio");
  var briefScript = document.getElementById("brief-script");
  var briefId = null;
  var briefReady = false;  // after the first load, a new briefing gets a "New" tag

  function showBrief(brief) {{
    if (!brief) {{ briefBox.hidden = true; document.body.classList.remove("has-brief"); return; }}
    if (brief.id === briefId) return;
    briefId = brief.id;
    briefAudio.src = brief.audio + "?v=" + brief.id;
    document.getElementById("brief-when").textContent = brief.when;
    var list = document.getElementById("brief-lines");
    list.textContent = "";
    brief.lines.forEach(function (line) {{
      var item = document.createElement("li");
      item.className = line[0];
      var who = document.createElement("span");
      who.className = "who";
      who.textContent = line[0];
      item.appendChild(who);
      item.appendChild(document.createTextNode(line[1]));
      list.appendChild(item);
    }});
    briefScript.hidden = brief.lines.length === 0;
    briefBox.hidden = false;
    document.body.classList.add("has-brief");
    if (briefReady) briefBox.classList.add("fresh");
  }}
  briefAudio.addEventListener("play", function () {{ briefBox.classList.remove("fresh"); }});
  showBrief({_js(briefing)});
  briefReady = true;

  window.renderDash = function (html, brief) {{
    showBrief(brief);
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

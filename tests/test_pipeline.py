"""Offline tests: no API key, no network. Models and web pages are replaced by fakes."""

import re
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

import board
import crew
import dashboard
import gates
import gemini
import llm
import pipeline
import workspace

PAGE = ("<html><head><title>Fake page</title><script>var x=1;</script></head><body><p>"
        + "Adults should consider a daily vitamin D supplement in autumn and winter. " * 6
        + "</p></body></html>")
QUOTE = "Adults should consider a daily vitamin D supplement in autumn and winter."


# ---------- board ----------

def test_old_board_gets_new_columns(tmp_path):
    conn = sqlite3.connect(board.DB_PATH)
    conn.executescript("""CREATE TABLE runs (id INTEGER PRIMARY KEY, goal TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running', budget_usd REAL NOT NULL, started_at TEXT NOT NULL,
        finished_at TEXT);
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, title TEXT NOT NULL,
        owner TEXT, status TEXT NOT NULL DEFAULT 'queued', result TEXT, review_notes TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);""")
    conn.commit(); conn.close()
    run = board.start_run("old board", 1.0)
    task = board.add_task(run, "t", task_type="research", depends_on=[1, 2])
    row = board.summary(run)["tasks"][0]
    assert row["task_type"] == "research" and row["depends_on"] == "[1, 2]" and row["id"] == task


def test_messages_inbox_read_and_cap(monkeypatch):
    run = board.start_run("g", 1.0)
    a = board.post_message(run, "writer-1", "researcher-1", "request", "need a dosage source")
    board.post_message(run, "researcher-1", "all", "offer", "filed S1")
    assert [m["id"] for m in board.unread_messages(run, "researcher-1")] == [a, a + 1][:1]  # not its own
    assert len(board.unread_messages(run, "writer-1")) == 1          # the "all" offer
    board.mark_read("researcher-1", [a])
    assert board.unread_messages(run, "researcher-1") == []
    with pytest.raises(ValueError):
        board.post_message(run, "x", "y", "shout", "hi")
    monkeypatch.setattr(board, "MAX_MESSAGES_PER_RUN", 3)
    assert board.post_message(run, "x", "y", "say", "third") is not None
    assert board.post_message(run, "x", "y", "say", "fourth") is None


def test_message_cap_holds_when_agents_post_at_once(monkeypatch):
    monkeypatch.setattr(board, "MAX_MESSAGES_PER_RUN", 5)
    run = board.start_run("g", 1.0)
    threads = [threading.Thread(target=board.post_message, args=(run, f"a{i}", "all", "say", "hi"))
               for i in range(20)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert board.message_count(run) == 5


# ---------- library and fetching ----------

def test_is_public_url(monkeypatch):
    assert not workspace.is_public_url("ftp://example.com/x")
    assert not workspace.is_public_url("http://example.com:8080/x")
    monkeypatch.setattr(workspace.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert workspace.is_public_url("https://example.com/page")
    for private in ("127.0.0.1", "10.0.0.5", "192.168.1.9", "169.254.169.254", "::1"):
        monkeypatch.setattr(workspace.socket, "getaddrinfo",
                            lambda *a, _p=private, **k: [(2, 1, 6, "", (_p, 443))])
        assert not workspace.is_public_url("https://sneaky.example.com/")


def test_extract_text_drops_scripts_and_keeps_title():
    title, text = workspace.extract_text(PAGE)
    assert title == "Fake page" and "var x" not in text and QUOTE in text


def test_fetch_page_rules(monkeypatch):
    monkeypatch.setattr(workspace, "_get", lambda url: (url, "text/html", PAGE.encode()))
    page = workspace.fetch_page("https://example.com/a")
    assert page["title"] == "Fake page" and QUOTE in page["text"]
    monkeypatch.setattr(workspace, "_get", lambda url: (url, "text/html", b"<p>tiny</p>"))
    with pytest.raises(workspace.FetchError):
        workspace.fetch_page("https://example.com/b")
    monkeypatch.setattr(workspace, "_get", lambda url: (url, "application/pdf", b"%PDF"))
    with pytest.raises(workspace.FetchError):
        workspace.fetch_page("https://example.com/c")


def test_library_ids_are_stable_and_urls_dedupe():
    page = lambda n: {"url": f"https://a.org/{n}", "title": f"T{n}", "text": QUOTE * 5, "retrieved": "now"}
    assert workspace.save_source(1, page(1)) == "S1"
    assert workspace.save_source(1, page(2)) == "S2"
    assert workspace.save_source(1, page(1)) == "S1"            # same URL, same id
    assert sorted(workspace.load_library(1)) == ["S1", "S2"]
    assert workspace.load_library(2) == {}                       # runs don't share libraries


def test_import_pack_and_quote_check():
    ids = workspace.import_pack(7)
    library = workspace.load_library(7)
    assert ids == ["S1", "S2", "S3"] and len(library) == 3
    vit_d = next(s for s in library.values() if s["title"] == "Vitamin D")
    assert workspace.quote_in_source("everyone should consider taking a daily vitamin D supplement", vit_d)
    assert not workspace.quote_in_source("vitamin D cures colds", vit_d)


# ---------- gates ----------

def test_gates():
    assert gates.research_gate(["S1"]) == []
    assert "No page" in gates.research_gate([], ["404"])[0]
    src = {"S1": {"text": "The sky is blue on clear days."}}
    good = {"text": "Sky.", "evidence": [{"source": "S1", "quote": "the sky is BLUE"}]}
    assert gates.section_gate(good, src) == []
    assert "word for word" in gates.section_gate(
        {"text": "x", "evidence": [{"source": "S1", "quote": "the sky is green"}]}, src)[0]
    assert "not one of the sources" in gates.section_gate(
        {"text": "x", "evidence": [{"source": "S9", "quote": "a"}]}, src)[0]
    assert gates.section_gate({"text": "", "evidence": []}, src)


# ---------- plan ----------

def test_normalise_plan():
    raw = [
        {"key": "r1", "type": "research", "title": "R", "instructions": "i", "acceptance": [], "depends_on": ["w1"]},
        {"key": "w1", "type": "write", "title": "W", "instructions": "i", "acceptance": [],
         "depends_on": ["r1", "ghost", "w1", "r1"]},
    ]
    plan = pipeline.normalise_plan(raw)
    assert plan[0]["depends_on"] == [] and plan[1]["depends_on"] == ["r1"]
    loop = [
        {"key": "a", "type": "write", "title": "A", "instructions": "", "acceptance": [], "depends_on": ["b"]},
        {"key": "b", "type": "write", "title": "B", "instructions": "", "acceptance": [], "depends_on": ["a"]},
    ]
    assert all(t["depends_on"] == [] for t in pipeline.normalise_plan(loop))
    with pytest.raises(ValueError):
        pipeline.normalise_plan([raw[0]])
    with pytest.raises(ValueError):
        pipeline.normalise_plan([])


# ---------- budget reservation ----------

def _fake_response(text="ok"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=10, output_tokens=10), stop_reason="end_turn")


def test_parallel_calls_cannot_overshoot_the_budget(monkeypatch):
    in_flight, peak, lock = [0], [0], threading.Lock()

    def slow_create(**request):
        with lock:
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
        time.sleep(0.15)
        with lock:
            in_flight[0] -= 1
        return _fake_response()

    monkeypatch.setattr(llm._client.messages, "create", slow_create)
    one_call = board.cost_of("claude-haiku-5-5", 10, 1000)       # worst case of one tiny call
    run = board.start_run("budget", budget_usd=one_call * 3.5)   # room for three in flight, not four
    results = []

    def go():
        try:
            llm.ask(run, "worker", "hi", model=llm.FAST, max_tokens=1000)
            results.append("ok")
        except llm.BudgetExceeded:
            results.append("stopped")

    threads = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert peak[0] <= 3, f"{peak[0]} calls were in flight at once"
    assert "ok" in results and "stopped" in results
    assert board.run_cost(run) <= board.summary(run)["run"]["budget_usd"]
    assert board.run_status(run) == "stopped_budget"
    assert llm._reserved.get(run, 0) == pytest.approx(0)          # every reservation was released


def test_reservation_is_released_when_a_call_fails(monkeypatch):
    def boom(**request):
        raise RuntimeError("network down")
    monkeypatch.setattr(llm._client.messages, "create", boom)
    run = board.start_run("fail", budget_usd=1.0)
    with pytest.raises(RuntimeError):
        llm.ask(run, "worker", "hi", model=llm.FAST, max_tokens=100)
    assert llm._reserved.get(run, 0) == pytest.approx(0)


def test_ask_search_records_search_cost_and_returns_pages(monkeypatch):
    blocks = [
        SimpleNamespace(type="text", text="Found some pages."),
        SimpleNamespace(type="web_search_tool_result", content=[
            SimpleNamespace(url="https://nhs.uk/a", title="A"), {"url": "https://nhs.uk/b", "title": "B"},
            SimpleNamespace(url="https://nhs.uk/a", title="A again")]),
        SimpleNamespace(type="web_search_tool_result", content=SimpleNamespace(error_code="unavailable")),
    ]
    response = SimpleNamespace(content=blocks, stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=50, output_tokens=20,
                                                     server_tool_use=SimpleNamespace(web_search_requests=2)))
    monkeypatch.setattr(llm._client.messages, "create", lambda **r: response)
    run = board.start_run("search", 1.0)
    found = llm.ask_search(run, "researcher-1", "find it")
    assert [p["url"] for p in found["pages"]] == ["https://nhs.uk/a", "https://nhs.uk/b"]
    agents = {a["agent"]: a["cost_usd"] for a in board.summary(run)["by_agent"]}
    assert agents["researcher-1"] >= 0.02                         # two searches at $0.01
    assert llm._reserved.get(run, 0) == pytest.approx(0)


# ---------- the whole pipeline, with fake models ----------

class Fake:
    """Replaces the models. `plan` is the coordinator's output; the other agents behave
    according to the hooks, so each test can make the crew succeed, fail or disagree."""

    def __init__(self, plan, **hooks):
        self.plan, self.hooks = plan, hooks
        self.log, self.lock = [], threading.Lock()
        self.in_flight = self.peak = 0

    def _enter(self, agent):
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.log.append(("start", agent, time.time()))

    def _exit(self, agent):
        with self.lock:
            self.in_flight -= 1
            self.log.append(("end", agent, time.time()))

    def span(self, agent):
        start = next(t for k, a, t in self.log if k == "start" and a == agent)
        end = next(t for k, a, t in self.log if k == "end" and a == agent)
        return start, end

    def calls(self, agent):
        return sum(1 for k, a, _ in self.log if k == "start" and a == agent)

    def ask_json(self, run_id, agent, prompt, schema, *, system=None, model=None, max_tokens=None, task_id=None):
        self._enter(agent)
        try:
            board.record_call(run_id, agent, "claude-haiku-5-5", 100, 50, task_id=task_id)
            hook = self.hooks.get(agent) or self.hooks.get(agent.split("-")[0])
            if hook:
                out = hook(self, agent, prompt, schema)
                if out is not None:
                    return out
            if schema is pipeline.PLAN_SCHEMA:
                return {"tasks": self.plan}
            if schema is pipeline.RESEARCH_SCHEMA:
                time.sleep(0.25)
                return {"urls": [{"url": f"https://example.org/{agent}", "why": "relevant"}]}
            if schema is pipeline.SECTION_SCHEMA:
                return section_from_prompt(prompt)
            if schema is crew.REVIEW_SCHEMA:
                return {"verdict": "pass", "notes": "Supported by the quote."}
            raise AssertionError("unexpected schema")
        finally:
            self._exit(agent)


def section_from_prompt(prompt, text="A short section."):
    """A writer answer quoting the first source it was given."""
    match = re.search(r"\[(S\d+)\][^\n]*\n([^\n]+)", prompt)
    return {"status": "ready", "text": text,
            "evidence": [{"source": match.group(1), "quote": match.group(2)[:60]}]}


def task(key, kind, deps=(), title=None):
    return {"key": key, "type": kind, "title": title or f"Task {key}", "instructions": "do it",
            "acceptance": ["checkable"], "depends_on": list(deps)}


@pytest.fixture
def install(monkeypatch):
    def _install(fake, gemini_fake=None):
        monkeypatch.setattr(pipeline, "ask_json", fake.ask_json)
        monkeypatch.setattr(workspace, "_get", lambda url: (url, "text/html", PAGE.encode()))
        if gemini_fake:
            monkeypatch.setattr(gemini, "ask_json", gemini_fake)
        return fake
    return _install


def statuses(run_id):
    return {t["owner"]: t["status"] for t in board.summary(run_id)["tasks"]}


def test_parallel_research_then_writers(install, tmp_path):
    plan = [task("r1", "research"), task("r2", "research"),
            task("w1", "write", ["r1"]), task("w2", "write", ["r2"]), task("w3", "write", ["r1", "r2"])]
    fake = install(Fake(plan))
    run_id = pipeline.main("goal", max_parallel=3)
    assert set(statuses(run_id).values()) == {"done"}
    assert board.summary(run_id)["run"]["status"] == "completed"
    # both researchers ran at the same time
    (s1, e1), (s2, e2) = fake.span("researcher-1"), fake.span("researcher-2")
    assert s2 < e1 and s1 < e2
    # a writer only started after the research it depends on had finished
    assert fake.span("writer-1")[0] >= e1 and fake.span("writer-3")[0] >= max(e1, e2)
    assert fake.peak <= 3
    # sources were saved by code, writers cited them, offers were posted
    assert len(workspace.load_library(run_id)) == 2
    assert {m["kind"] for m in board.messages_for_run(run_id)} == {"offer"}
    brief = (tmp_path / "output" / f"brief_run{run_id}.md").read_text()
    assert "Fake page" in brief and "A short section." in brief and "## Sources" in brief
    body = dashboard._body(run_id)
    assert "Message feed" in body and "researcher-1" in body


def test_sequential_mode_runs_one_at_a_time(install):
    plan = [task("r1", "research"), task("r2", "research"), task("w1", "write", ["r1", "r2"])]
    fake = install(Fake(plan))
    pipeline.main("goal", max_parallel=1)
    assert fake.peak == 1


def test_writer_asks_for_a_missing_source(install):
    def writer(fake, agent, prompt, schema):
        if "Sources:\n(none)" in prompt:
            return {"status": "need_source", "text": "", "evidence": [], "missing": "the safe upper dose"}
    fake = install(Fake([task("w1", "write")], writer=writer))
    run_id = pipeline.main("goal")
    tasks = {t["owner"]: t for t in board.summary(run_id)["tasks"]}
    assert tasks["writer-1"]["status"] == "done" and tasks["writer-1"]["attempts"] == 1
    assert tasks["researcher-1"]["status"] == "done" and "Find a source" in tasks["researcher-1"]["title"]
    request = [m for m in board.messages_for_run(run_id) if m["kind"] == "request"][0]
    assert (request["sender"], request["recipient"]) == ("writer-1", "researcher-1")
    assert "upper dose" in request["text"]
    assert fake.calls("writer-1") == 2                      # asked, then wrote once it had a source


def test_source_requests_are_limited(install, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_SOURCE_REQUESTS", 1)
    def writer(fake, agent, prompt, schema):
        return {"status": "need_source", "text": "", "evidence": [], "missing": "something"}
    install(Fake([task("w1", "write")], writer=writer))
    run_id = pipeline.main("goal")
    assert statuses(run_id)["writer-1"] == "needs_human"
    assert board.summary(run_id)["run"]["status"] == "needs_human"
    assert any(m["kind"] == "escalate" for m in board.messages_for_run(run_id))


def test_reviewer_challenge_and_reply(install, tmp_path):
    reviews = []
    def reviewer(fake, agent, prompt, schema):
        if schema is crew.REVIEW_SCHEMA:
            reviews.append(1)
            return ({"verdict": "revise", "notes": "The source is a blog; cite an official one."}
                    if len(reviews) == 1 else {"verdict": "pass", "notes": "Better."})
    fake = install(Fake([task("r1", "research"), task("w1", "write", ["r1"])], reviewer=reviewer))
    run_id = pipeline.main("goal")
    writer = next(t for t in board.summary(run_id)["tasks"] if t["owner"] == "writer-1")
    assert writer["status"] == "done" and writer["attempts"] == 2
    kinds = [m["kind"] for m in board.messages_for_run(run_id)]
    assert "challenge" in kinds and "reply" in kinds
    assert "Caught in review" in (tmp_path / "output" / f"brief_run{run_id}.md").read_text()


def test_invented_quote_is_stopped_by_the_gate_before_review(install):
    def writer(fake, agent, prompt, schema):
        if "Code check" not in prompt:
            return {"status": "ready", "text": "x", "evidence": [{"source": "S1", "quote": "made up words"}]}
    fake = install(Fake([task("r1", "research"), task("w1", "write", ["r1"])], writer=writer))
    run_id = pipeline.main("goal")
    writer_task = next(t for t in board.summary(run_id)["tasks"] if t["owner"] == "writer-1")
    assert writer_task["status"] == "done" and writer_task["attempts"] == 2
    assert fake.calls("reviewer") == 1                      # the reviewer never saw the invented quote


def test_failed_research_blocks_its_dependents_without_crashing(monkeypatch, install):
    fake = install(Fake([task("r1", "research"), task("r2", "research"),
                         task("w1", "write", ["r1"]), task("w2", "write", ["w1"]),
                         task("w3", "write", ["r2"])]))
    def get(url):
        if "researcher-1" in url:
            raise workspace.FetchError("404")
        return url, "text/html", PAGE.encode()
    monkeypatch.setattr(workspace, "_get", get)
    run_id = pipeline.main("goal")
    s = statuses(run_id)
    assert s["researcher-1"] == "needs_human" and s["writer-1"] == "needs_human"
    assert s["writer-2"] == "needs_human"                   # a failure ripples down a chain
    assert s["researcher-2"] == "done" and s["writer-3"] == "done"
    assert board.summary(run_id)["run"]["status"] == "needs_human"


def test_budget_stop_ends_the_run_cleanly(install, tmp_path):
    def researcher(fake, agent, prompt, schema):
        if agent == "researcher-2":
            board.finish_run(fake.run_id_for_test, status="stopped_budget")
            raise llm.BudgetExceeded("out of money")
    fake = Fake([task("r1", "research"), task("r2", "research"), task("w1", "write", ["r1", "r2"])],
                researcher=researcher)
    original = board.start_run
    def start(*a, **k):
        fake.run_id_for_test = original(*a, **k)
        return fake.run_id_for_test
    board.start_run = start
    try:
        install(fake)
        run_id = pipeline.main("goal")
    finally:
        board.start_run = original
    s = statuses(run_id)
    assert s["researcher-2"] == "stopped" and s["writer-1"] == "stopped"
    assert board.summary(run_id)["run"]["status"] == "stopped_budget"
    assert (tmp_path / "output" / f"brief_run{run_id}.md").exists()


def test_gemini_must_agree(install):
    def gemini_fake(run_id, agent, prompt, schema, **kw):
        board.record_call(run_id, agent, "gemini-2.5-flash", 100, 50, task_id=kw.get("task_id"))
        calls.append(1)
        return ({"verdict": "revise", "notes": "Not convinced."} if len(calls) == 1
                else {"verdict": "pass", "notes": "Fine now."})
    calls = []
    fake = install(Fake([task("r1", "research"), task("w1", "write", ["r1"])]), gemini_fake)
    run_id = pipeline.main("goal", use_gemini=True)
    writer = next(t for t in board.summary(run_id)["tasks"] if t["owner"] == "writer-1")
    assert writer["status"] == "done" and writer["attempts"] == 2 and len(calls) == 2
    assert fake.calls("reviewer") == 2                       # Claude re-reviewed the revision


def test_gemini_outage_holds_the_work_instead_of_passing_it(install):
    def down(*a, **k):
        raise RuntimeError("503")
    install(Fake([task("r1", "research"), task("w1", "write", ["r1"])]), down)
    run_id = pipeline.main("goal", use_gemini=True)
    assert statuses(run_id)["writer-1"] == "needs_human"


def test_web_search_path_and_fallback(install, monkeypatch):
    install(Fake([task("r1", "research"), task("w1", "write", ["r1"])]))
    monkeypatch.setattr(pipeline, "ask_search", lambda *a, **k: {"text": "", "pages": [
        {"url": "https://nhs.uk/vitamin-d", "title": "Vitamin D"}]})
    run_id = pipeline.main("goal", web_search=True)
    assert [s["url"] for s in workspace.load_library(run_id).values()] == ["https://nhs.uk/vitamin-d"]

    def unavailable(*a, **k):
        raise RuntimeError("tool not enabled")
    monkeypatch.setattr(pipeline, "ask_search", unavailable)
    run_id = pipeline.main("goal", web_search=True)          # falls back to proposing URLs
    assert statuses(run_id)["writer-1"] == "done"
    assert any("Web search isn't available" in m["text"] for m in board.messages_for_run(run_id))


def test_pack_gives_writers_sources_with_no_research(install):
    fake = install(Fake([task("w1", "write")]))
    run_id = pipeline.main("goal", with_pack=True)
    assert statuses(run_id) == {"writer-1": "done"} and fake.calls("researcher-1") == 0


def test_dashboard_renders_waiting_tasks_and_feed(install):
    install(Fake([task("r1", "research"), task("w1", "write", ["r1"])]))
    run_id = pipeline.main("goal")
    html = dashboard._body(run_id)
    assert "Message feed" in html and "offer" in html and "research" in html
    assert dashboard.render(run_id).exists()


# ---------- the old paths still work ----------

def test_ask_json_still_returns_the_submitted_answer(monkeypatch):
    block = SimpleNamespace(type="tool_use", input={"verdict": "pass", "notes": "ok"})
    response = SimpleNamespace(content=[block], stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=30, output_tokens=12))
    monkeypatch.setattr(llm._client.messages, "create", lambda **r: response)
    run = board.start_run("old path", 1.0)
    assert llm.ask_json(run, "reviewer", "review it", crew.REVIEW_SCHEMA) == {"verdict": "pass", "notes": "ok"}
    assert board.run_cost(run) > 0 and llm._reserved.get(run, 0) == pytest.approx(0)


def test_watcher_can_run_the_pipeline(install, monkeypatch):
    import watch
    install(Fake([task("r1", "research"), task("w1", "write", ["r1"])]))
    monkeypatch.setattr(watch, "RUNNER", pipeline)
    monkeypatch.setattr(watch, "BRIEF_AFTER_WORK", False)
    event_id = board.add_event("vitamin-d.txt", "abc123", "Is vitamin D worth taking?")
    watch.work_on(board.next_event())
    event = board.recent_events()[0]
    assert event["id"] == event_id and event["status"] == "done" and event["run_id"]
    assert watch.can_afford_a_run()


def test_spoken_briefing_handles_pipeline_runs(install):
    import brief
    install(Fake([task("r1", "research"), task("w1", "write", ["r1"], title="Who should take it")]))
    run_id = pipeline.main("Is vitamin D worth taking?")
    lines, _ = brief.build_script(show_all=True)
    spoken = " ".join(text for _, text in lines)
    assert "Who should take it" in spoken or "Is vitamin D worth taking" in spoken
    assert "Spent today" in spoken and "Task r1" not in spoken     # finished research isn't narrated

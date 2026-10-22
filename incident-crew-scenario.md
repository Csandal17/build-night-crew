# Incident crew: the library-removal scenario

One goal: a production error arrives. The crew finds the cause from separate evidence sources, splits the fix into parts, reviews each part with a different agent, and ships a ready-to-merge package or escalates to a human. It never touches production.

Everything is simulated and labelled as such: the site, the data, and Sentry. The exception is real.

## 1. The scenario

**The site: ProteinPick.** Type a shop or restaurant and it returns the highest-protein item per £. Restaurants list protein per portion. Shops (supermarkets, delis) list protein per 100 g, so ranking them converts weights, and that conversion code calls `np.float(...)`.

**What happens.** The site has worked for weeks. At 14:02 a redeploy rebuilds the environment from an unpinned `numpy>=1.20`. The rebuild picks up a NumPy where the old `np.float` alias no longer exists (to be confirmed by the researcher from the release notes; my understanding is that NumPy 1.24 removed it). Every shop search now raises `AttributeError: module 'numpy' has no attribute 'float'`. Restaurant searches still work, so only part of the site is down.

**Why it's a good test for a crew**
- No application code changed. The newest code commit is days old, so "what changed" is not in git.
- The logs are quiet. A `DeprecationWarning` was logged once at start-up for weeks, and there are two decoys: a repeating disk warning and a few slow queries.
- The cause lives in four different places (logs, dependency records, code, the library's own release notes), so one agent reading everything would be guessing.

**Trigger.** A simulated Sentry alert (real stack trace and source lines, signed, marked simulated) lands in `inbox/`. Nothing polls for the error.

## 2. Evidence the demo service holds (all deterministic)

| File | What it is |
|---|---|
| `service/` + git history | 5 commits, none touching dependencies in the last 3 days. Decoy commits: "Add dark mode", "Update README". |
| `logs/app.log` | Start-up `DeprecationWarning` for `np.float`, requests, 200s until 14:02, then 500s on shop searches only. |
| `deploys/previous.freeze`, `deploys/current.freeze` | Package versions before and after the 14:02 redeploy (NumPy moves up a few minor versions). |
| `requirements.txt` | Unpinned `numpy>=1.20`. |
| `searches.db` | Every search: time, query, place type, status. Gives impact numbers. |
| `.demo/facts.json` | Ground truth used only by tests, never shown to agents. |

## 3. Stages, agents and roles

### Stage 1: Collect (parallel, one agent per source, read-only)

Each agent picks from a fixed menu of read-only queries and code runs them. No agent runs free-form commands. Each returns evidence items (E1, E2, ...) pointing at an exact line, commit, row or package.

| Agent | Source | Hands back |
|---|---|---|
| Log analyst | `logs/app.log` | First error time and line, error counts by place type, the early deprecation warning, and which warnings are noise |
| Git historian | git | Commits near the first error, files they touched, and whether any touched dependencies |
| Dependency collector | freeze files, `requirements.txt` | Which packages changed between deploys, and which are unpinned |
| Code reader | source at the alert's release | The failing line from the stack frames, plus every other use of the same call |
| Impact analyst | `searches.db` | Failed searches, which place types, the time window, and the share of traffic affected |

**Gate (plain code):** every cited line, commit and row exists, and every count recomputes. Evidence that fails is dropped before anyone reasons from it.

### Stage 2: Research (the web, in parallel with Stage 1 once the error text is known)

| Agent | Job |
|---|---|
| Library researcher | Finds the library's release notes and migration guidance for the removed call. Proposes URLs; code fetches and saves them. |

**Gate:** every claim the diagnostician makes about the library must be a quote that appears word for word on a saved page.

### Stage 3: Diagnose (loop, max 2 rounds)

| Agent | Job |
|---|---|
| Diagnostician | Reads only the evidence library and writes a root-cause hypothesis citing evidence IDs. It names which version removed the call. |
| Skeptic | Must rule out the decoys (disk warning, slow queries, the dark-mode commit) and ask what else fits. If it can't, the hypothesis goes back. |

The challenge and the reply post to the message feed so the audience sees the agents disagree.

### Stage 4: Fix (parallel, each on its own git branch)

| Agent | Output |
|---|---|
| Planner | Splits the fix into the parts below, from the diagnosis |
| Code fixer | Replaces `np.float` with builtin `float` at every use |
| Test writer | A regression test that calls the shop-ranking path |
| Pinning agent | A version constraint for `requirements.txt` |
| Remediation agent | The failed-search list from the database and a short status note |

### Stage 5: Review (a different agent for each part; loops back if wrong)

| Reviewer | Checks |
|---|---|
| Fix reviewer | The diff is minimal and complete, and no other use of the alias is left |
| Test reviewer | The test would have caught the bug |
| Pin reviewer | The constraint actually blocks the breaking versions |
| Remediation reviewer | The failed-search list matches the database |

**Gates run first and are plain code**
- The new test fails on the old code and passes on the fix.
- The recorded failing shop searches replay with zero failures.
- The diff only touches allowed files.

A rejected part goes back to its author with the reason, up to two times.

### Stage 6: Integrate and ship

| Agent | Job |
|---|---|
| Integrator | Merges the branches, runs the full test suite, writes the incident report with a timeline |

**Ship rule, fixed in code:** the crew only outputs a ready-to-merge package if every gate passed. Otherwise it escalates to a human with what it found and what blocked it.

## 4. Message feed (what the audience sees)

Sample messages: Dependency collector to Diagnostician: "numpy 1.23.5 to 1.26.4 between deploys (E4)". Skeptic to Diagnostician: "The disk warning predates the incident by days; rule it out with E2." Test reviewer to Test writer: "This test passes on the old code too. Rewrite."

## 5. What's real and what's simulated

| Real | Simulated |
|---|---|
| The exception and stack trace | Sentry itself |
| The library removal and its release notes | The site, shops and traffic |
| The git history, logs and database queries | The production deployment |
| The test and replay gates | The deploy records |

The Sentry payload shape and signature header are unverified against a real Sentry delivery.

## 6. Demo script (3 minutes)

1. Open ProteinPick. A restaurant search works. A shop search works.
2. Redeploy. A shop search now fails.
3. Sentry alert arrives; the dashboard wakes up.
4. Collectors work in parallel. Evidence appears with IDs.
5. The skeptic challenges the decoys on the feed.
6. A reviewer rejects the first test; the author redoes it.
7. Gates pass. Incident report and ready-to-merge branch appear. Shop search works again on the fixed branch.

## 7. Risks

- Venue wifi: pre-save the release-notes pages as a source pack.
- Library version: pin the demo environment so it fails the same way every run.
- Unverified claim: the exact NumPy version that removed the alias is checked by the researcher, not assumed.
- Sandboxes: collectors run as separate processes unless there's time to move them into separate Sparkles sandboxes. We say which one we did.

## 8. Build order

1. Rebuild `make_demo_service.py` for ProteinPick: code, history, logs, freeze files, `searches.db`. Update the Sentry mock to read the real failure.
2. Collectors with the evidence gate.
3. Researcher and quote gate.
4. Diagnostician and skeptic loop with the message feed.
5. Fix stage with the replay and test gates.
6. Review loop, integrator, incident report page.
7. If time: separate sandboxes.

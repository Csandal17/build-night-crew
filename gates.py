"""Deterministic gates: checks done by plain code before any model reviews the work.

Each task type has one. A gate returns a list of problems (empty means it passed), and the
reviewers only ever see work that has already got through its gate. The agent that did the
work can't edit, argue with or skip these checks.

  research  -> were real pages fetched and saved, with enough text to be a source?
  write     -> is there text, and does every quote appear word for word in a source
               the writer was actually given?

To add a task type (say, `code`), add a function here and register it in GATES.
"""

import workspace

MIN_QUOTES = 1


def research_gate(saved, errors=()):
    """`saved` is the list of source ids filed by this task; `errors` are fetch failures."""
    if saved:
        return []
    detail = f" Fetch problems: {'; '.join(errors)}" if errors else ""
    return ["No page could be fetched and saved as a source." + detail]


def section_gate(answer, visible_sources):
    """`visible_sources` is {"S1": source, ...}: only what this writer was given."""
    problems = []
    if not (answer.get("text") or "").strip():
        problems.append("The section has no text.")
    evidence = answer.get("evidence") or []
    if len(evidence) < MIN_QUOTES:
        problems.append("A section needs at least one quote from a source.")
    for item in evidence:
        sid, quote = item.get("source"), item.get("quote", "")
        if sid not in visible_sources:
            problems.append(f"{sid} is not one of the sources you were given.")
        elif not workspace.quote_in_source(quote, visible_sources[sid]):
            problems.append(f"The quote attributed to {sid} does not appear in it word for word.")
    return problems


GATES = {"research": research_gate, "write": section_gate}

"""Prompts: a human/agent-readable brief, and a strict JSON extraction prompt for providers."""

from __future__ import annotations

from pathlib import Path

from ..memory.layout import find_ticket_dir
from ..store import read_text

RULES = """\
RULES (non-negotiable)
1. Use ONLY facts present in the sources. Never invent people, roles, tickets, dates, links or decisions.
2. Every entry has a confidence tag:
   verified    = stated directly in a source (a source reference is REQUIRED)
   inferred    = your reasonable deduction from sources (say so)
   unconfirmed = mentioned but not confirmed (e.g. "Priya will test it")
   unknown     = needed information that the sources do not contain
   conflict    = two sources disagree (state both, with sources)
3. Cite the source for each entry: ticket, Confluence page, MR number/comment, commit hash.
4. Roles (QA, frontend, backend, reviewer) only when a source states them; otherwise role = unknown.
5. Do not copy source code, credentials or secrets. File paths and short summaries only.
6. Add; never delete or rewrite history. Skip anything already recorded.
7. Discussions come from WRITTEN comments only (ticket comments, MR review comments). Say who said what.
"""

SECTION_GUIDE = """\
SECTIONS
implementation  what was built and why
decisions       technical decision + reason (+ alternatives if stated)
discussions     who said what in ticket/MR comments
testing         test cases, results, who tested, defects
open-items      pending work, follow-ups, unanswered questions
docs            documentation links and what each page covers
summary         2-3 sentence overview (write once)
"""


def build_brief(memory_dir: Path, ticket: str, saved: list[Path]) -> str:
    tdir = find_ticket_dir(memory_dir, ticket)
    rel = tdir.relative_to(memory_dir) if tdir else Path("?")
    srcs = "\n".join(f"  - {p}" for p in saved) or "  (none)"
    return f"""\
Engineering Memory — extraction brief for {ticket}
Memory directory: {memory_dir}
Ticket memory:    {rel}/

1. Read these sanitised source files (paths relative to the memory directory):
{srcs}
2. Read {rel}/MEMORY.md first so you do not repeat what is recorded.
3. Record what you learn with `memory update {ticket}`, e.g.

   memory update {ticket} --section implementation --confidence verified \\
       --source "MR !482 description" --text "Retries use exponential backoff capped at 5 attempts"
   memory update {ticket} --section decisions --confidence verified --source "Confluence: Payment retry design" \\
       --text "Chose backoff over fixed delay to avoid thundering herd"
   memory update {ticket} --person "Priya Sharma" --role QA --source "Ticket comment 2026-10-30"
   memory update {ticket} --section open-items --confidence unknown --text "Reviewer of the MR is not stated in any source"
   memory update {ticket} --set status=done

   Or pipe a JSON batch:  memory update {ticket} --json answer.json
4. Run `memory validate` and `memory index` when finished.

{RULES}
{SECTION_GUIDE}"""


JSON_SCHEMA = """\
Return ONLY one JSON object (no prose):
{
  "entries": [{"section": "implementation|decisions|discussions|testing|open-items|docs|summary",
               "text": "...", "confidence": "verified|inferred|unconfirmed|unknown|conflict",
               "source": "where this came from (required for verified)"}],
  "people":  [{"name": "...", "role": "stated role or unknown", "source": "..."}],
  "links":   [{"url": "...", "label": "...", "kind": "docs|ticket|mr|other"}],
  "set":     {"title": "only if currently unknown", "status": "planned|in-progress|in-review|testing|done|blocked|abandoned"}
}
"""

MAX_SOURCE_CHARS = 60_000


def build_extraction_prompt(memory_dir: Path, ticket: str) -> tuple[str, list[str]]:
    """Self-contained prompt (current memory + stored sources) for ``memory update --ai``."""
    tdir = find_ticket_dir(memory_dir, ticket)
    if not tdir:
        raise ValueError(f"no memory for {ticket}")
    warnings: list[str] = []
    current = read_text(tdir / "MEMORY.md")
    chunks: list[str] = []
    budget = MAX_SOURCE_CHARS
    for f in sorted((memory_dir / "attachments" / ticket).glob("*.md")):
        text = read_text(f)
        if len(text) > budget:
            text = text[:budget]
            warnings.append(f"source {f.name} truncated to fit the prompt")
        budget -= len(text)
        chunks.append(f"=== SOURCE FILE: attachments/{ticket}/{f.name} ===\n{text}")
        if budget <= 0:
            warnings.append("remaining sources omitted (prompt size limit)")
            break
    prompt = (
        f"You maintain an engineering memory for ticket {ticket}.\n\n{RULES}\n{SECTION_GUIDE}\n"
        f"CURRENT MEMORY (do not repeat):\n{current}\n\nSOURCES:\n" + "\n\n".join(chunks) + f"\n\n{JSON_SCHEMA}"
    )
    return prompt, warnings

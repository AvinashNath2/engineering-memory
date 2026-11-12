"""Entry format and section-level Markdown editing.

An entry is one bullet in a ``## Section``::

    - **2026-11-07** [verified] Retries use exponential backoff. _(source: MR !482)_ <!-- em:3fa9c1d2e4 -->

Multi-line text continues on indented lines. The trailing HTML comment is an invisible,
stable id used for duplicate detection. Entries are only ever *added*.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from ..store import sha1

PLACEHOLDER = "_Nothing recorded yet._"
CONFIDENCE = ("verified", "inferred", "unconfirmed", "unknown", "conflict")
ID_RE = re.compile(r"<!--\s*em:([A-Za-z0-9:._\-]+)\s*-->")
ENTRY_START = re.compile(r"^- \*\*([^*]+)\*\* \[([a-z]+)\] ")
NEAR_DUP_RATIO = 0.93


class EntryError(ValueError):
    pass


@dataclass
class Entry:
    text: str
    confidence: str = "unconfirmed"
    source: str | None = None
    when: str = ""  # date or timestamp displayed in bold
    key: str | None = None  # explicit identity (e.g. "commit:<hash>"); default = text

    def validate(self) -> None:
        if self.confidence not in CONFIDENCE:
            raise EntryError(f"confidence must be one of {', '.join(CONFIDENCE)}")
        if not self.text or not self.text.strip():
            raise EntryError("entry text is empty")
        if self.confidence == "verified" and not (self.source and self.source.strip()):
            raise EntryError("a [verified] entry requires a source (e.g. --source 'MR !482')")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[`*_]", "", text.lower())).strip(" .;:-")


def entry_id(section: str, entry: Entry) -> str:
    basis = entry.key if entry.key else normalize(entry.text)
    return sha1(f"{section}|{basis}")[:10]


def render(section: str, entry: Entry) -> str:
    entry.validate()
    lines = [ln.rstrip() for ln in entry.text.strip().splitlines()]
    first, rest = lines[0], lines[1:]
    tail = ""
    if entry.source:
        tail += f" _(source: {entry.source.strip()})_"
    tail += f" <!-- em:{entry_id(section, entry)} -->"
    head = f"- **{entry.when}** [{entry.confidence}] {first}"
    if rest:
        rest[-1] = rest[-1] + tail
        return "\n".join([head] + ["  " + r if r else "" for r in rest])
    return head + tail


# ---- section editing ----------------------------------------------------
def _section_bounds(lines: list[str], heading: str) -> tuple[int, int] | None:
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == f"## {heading}":
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return start, end


def section_entries(body: str, heading: str) -> list[str]:
    """Return the raw entry blocks (strings) of a section."""
    lines = body.split("\n")
    bounds = _section_bounds(lines, heading)
    if not bounds:
        return []
    s, e = bounds
    blocks: list[list[str]] = []
    for ln in lines[s + 1:e]:
        if ln.startswith("- "):
            blocks.append([ln])
        elif blocks and (ln.startswith("  ") or ln == ""):
            if ln.strip():
                blocks[-1].append(ln)
    return ["\n".join(b) for b in blocks]


def existing_ids(body: str) -> set[str]:
    return set(ID_RE.findall(body))


def find_duplicate(body: str, heading: str, section: str, entry: Entry) -> str | None:
    """Return a reason string if the entry duplicates an existing one, else None."""
    if entry_id(section, entry) in existing_ids(body):
        return "duplicate (same id)"
    if entry.key:  # explicit keys are exact identities, no fuzzy matching
        return None
    new_norm = normalize(entry.text)
    for block in section_entries(body, heading):
        clean = ID_RE.sub("", block)
        clean = re.sub(r"_\(source:.*?\)_", "", clean)
        clean = ENTRY_START.sub("", clean, count=1)
        old_norm = normalize(clean)
        if old_norm and difflib.SequenceMatcher(None, old_norm, new_norm).ratio() >= NEAR_DUP_RATIO:
            return "near-duplicate of an existing entry"
    return None


def append_entry(body: str, heading: str, rendered: str, sort_timeline: bool = False) -> str:
    """Insert ``rendered`` into ``## heading``. Creates the section if it is missing."""
    lines = body.rstrip("\n").split("\n")
    bounds = _section_bounds(lines, heading)
    if bounds is None:
        lines += ["", f"## {heading}", "", rendered]
        return "\n".join(lines) + "\n"
    s, e = bounds
    content = lines[s + 1:e]
    content = [ln for ln in content if ln.strip() != PLACEHOLDER]
    # strip trailing blanks, then re-add one blank line before next section
    while content and not content[-1].strip():
        content.pop()
    while content and not content[0].strip():
        content.pop(0)

    if sort_timeline:
        blocks: list[list[str]] = []
        preamble: list[str] = []
        for ln in content:
            if ln.startswith("- "):
                blocks.append([ln])
            elif blocks:
                blocks[-1].append(ln)
            else:
                preamble.append(ln)
        new_when = ENTRY_START.match(rendered)
        key = new_when.group(1) if new_when else "~"
        insert_at = len(blocks)
        for idx, b in enumerate(blocks):
            m = ENTRY_START.match(b[0])
            if m and m.group(1) > key:  # ISO timestamps sort lexicographically
                insert_at = idx
                break
        blocks.insert(insert_at, rendered.split("\n"))
        content = preamble + [ln for b in blocks for ln in b]
    else:
        content.append(rendered)

    new_lines = lines[:s + 1] + [""] + content + ([""] if e < len(lines) else []) + lines[e:]
    return "\n".join(new_lines).rstrip("\n") + "\n"

"""Structured retrieval of a ticket memory (used by show / timeline / start / people)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..memory.entries import ENTRY_START, ID_RE
from ..memory.layout import MemoryError_, TICKET_FILES, find_ticket_dir, normalize_ticket
from ..memory.indexer import collect_tickets
from ..store import parse_frontmatter, read_text


@dataclass
class TicketMemory:
    ticket: str
    path: Path  # ticket directory
    meta: dict[str, Any]
    files: dict[str, str] = field(default_factory=dict)  # file name -> full text

    def rel(self, memory_dir: Path) -> str:
        return self.path.relative_to(memory_dir).as_posix()


def load_ticket(memory_dir: Path, ticket: str) -> TicketMemory | None:
    ticket = normalize_ticket(ticket)
    tdir = find_ticket_dir(memory_dir, ticket)
    if not tdir:
        return None
    files = {f: read_text(tdir / f) for f in TICKET_FILES if (tdir / f).exists()}
    meta, _ = parse_frontmatter(files.get("MEMORY.md", ""))
    return TicketMemory(ticket, tdir, meta, files)


def timeline_entries(tm: TicketMemory) -> list[tuple[str, str]]:
    """[(timestamp, text)] oldest first, with HTML id comments stripped."""
    text = tm.files.get("timeline.md", "")
    _, body = parse_frontmatter(text)
    out: list[tuple[str, str]] = []
    for line in body.splitlines():
        m = ENTRY_START.match(line)
        if m:
            clean = ID_RE.sub("", line[m.end():]).strip()
            out.append((m.group(1), f"[{m.group(2)}] {clean}"))
    return sorted(out, key=lambda t: t[0])


def filled_sections(tm: TicketMemory) -> dict[str, bool]:
    """Which sections already contain entries (used by `memory start`)."""
    _, body = parse_frontmatter(tm.files.get("MEMORY.md", ""))
    result: dict[str, bool] = {}
    current = None
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            result[current] = False
        elif current and line.startswith("- "):
            result[current] = True
    return result


def find_people(memory_dir: Path, query: str | None = None) -> list[dict[str, Any]]:
    """Aggregate people from ticket frontmatter. Optional case-insensitive name/role filter."""
    people: dict[str, dict[str, Any]] = {}
    for row in collect_tickets(memory_dir):
        for m in row.team:
            name = str(m.get("name", "")).strip()
            if not name:
                continue
            rec = people.setdefault(name.lower(), {"name": name, "entries": []})
            rec["entries"].append(
                {
                    "ticket": row.ticket,
                    "project": row.project,
                    "role": m.get("role", "unknown"),
                    "confidence": m.get("confidence", "unconfirmed"),
                    "source": m.get("source", ""),
                    "path": row.rel,
                }
            )
    out = sorted(people.values(), key=lambda r: r["name"].lower())
    if query:
        q = query.lower()
        out = [
            r for r in out
            if q in r["name"].lower() or any(q in str(e["role"]).lower() or q == e["ticket"].lower() for e in r["entries"])
        ]
    return out


def require_ticket(memory_dir: Path, ticket: str) -> TicketMemory:
    tm = load_ticket(memory_dir, ticket)
    if not tm:
        raise MemoryError_(f"No memory for {normalize_ticket(ticket)}. Run `memory start {normalize_ticket(ticket)}`.")
    return tm

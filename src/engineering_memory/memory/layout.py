"""Directory layout of a memory repository and lookup helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..store import parse_frontmatter, read_text

MARKER = ".engineering-memory.yaml"
SCHEMA_VERSION = 1
TOP_DIRS = ["projects", "people", "decisions", "discussions", "daily-logs", "attachments"]
TICKET_FILES = ["MEMORY.md", "timeline.md", "technical-decisions.md", "discussions.md", "testing.md"]
STATUSES = ("planned", "in-progress", "in-review", "testing", "done", "blocked", "abandoned")
TYPES = ("feature", "bug", "task", "spike", "chore")
TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


class MemoryError_(Exception):
    """Domain error with a message safe to show to the user."""


def normalize_ticket(ticket: str) -> str:
    t = ticket.strip().upper()
    if not TICKET_RE.match(t):
        raise MemoryError_(f"'{ticket}' is not a valid ticket id (expected e.g. PAY-1234)")
    return t


def require_memory_dir(memory_dir: Path) -> Path:
    if not (memory_dir / MARKER).exists():
        raise MemoryError_(
            f"{memory_dir} is not an initialised memory directory. Run `memory init {memory_dir}` first."
        )
    return memory_dir


def project_dir(memory_dir: Path, project: str) -> Path:
    return memory_dir / "projects" / project


def ticket_dir_for(memory_dir: Path, project: str, ticket: str) -> Path:
    return project_dir(memory_dir, project) / "tickets" / ticket


def find_ticket_dir(memory_dir: Path, ticket: str) -> Path | None:
    ticket = normalize_ticket(ticket)
    matches = sorted((memory_dir / "projects").glob(f"*/tickets/{ticket}"))
    matches = [m for m in matches if m.is_dir()]
    if len(matches) > 1:
        raise MemoryError_(
            f"Ticket {ticket} exists in several projects: " + ", ".join(str(m.relative_to(memory_dir)) for m in matches)
        )
    return matches[0] if matches else None


def iter_ticket_dirs(memory_dir: Path):
    yield from sorted(p for p in (memory_dir / "projects").glob("*/tickets/*") if p.is_dir())


def read_meta(path: Path) -> dict:
    meta, _ = parse_frontmatter(read_text(path))
    return meta


@dataclass
class ProjectInfo:
    slug: str
    name: str
    path: Path
    repos: list[dict]
    ticket_prefixes: list[str]
    description: str = ""


def list_projects(memory_dir: Path) -> list[ProjectInfo]:
    out: list[ProjectInfo] = []
    for pf in sorted((memory_dir / "projects").glob("*/PROJECT.md")):
        try:
            meta = read_meta(pf)
        except Exception:
            continue
        out.append(
            ProjectInfo(
                slug=pf.parent.name,
                name=str(meta.get("name", pf.parent.name)),
                path=pf.parent,
                repos=list(meta.get("repos") or []),
                ticket_prefixes=[str(p).upper() for p in (meta.get("ticket_prefixes") or [])],
                description=str(meta.get("description") or ""),
            )
        )
    return out


def resolve_project(
    memory_dir: Path,
    *,
    explicit: str | None = None,
    repo_name: str | None = None,
    remote: str | None = None,
    ticket: str | None = None,
) -> ProjectInfo | None:
    projects = list_projects(memory_dir)
    if explicit:
        for p in projects:
            if explicit in (p.slug, p.name):
                return p
        raise MemoryError_(f"Unknown project '{explicit}'. Register it with `memory project add`.")
    for p in projects:  # 1) repository
        for r in p.repos:
            if (repo_name and r.get("name") == repo_name) or (remote and r.get("remote") and r.get("remote") == remote):
                return p
    if ticket:  # 2) ticket prefix
        prefix = ticket.split("-")[0].upper()
        for p in projects:
            if prefix in p.ticket_prefixes:
                return p
    return None

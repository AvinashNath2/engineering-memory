"""Creating the memory repository, projects and ticket memories."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..store import (
    atomic_write,
    dump_frontmatter,
    instructions_dir,
    memory_lock,
    now_iso,
    slugify,
    template_dir,
)
from .layout import (
    MARKER,
    SCHEMA_VERSION,
    TICKET_FILES,
    TOP_DIRS,
    MemoryError_,
    find_ticket_dir,
    normalize_ticket,
    require_memory_dir,
    project_dir,
    read_meta,
    ticket_dir_for,
    TYPES,
)

GITIGNORE = """# Engineering Memory - local, rebuildable or transient files
.em.lock
.engineering-memory.sqlite
.engineering-memory.sqlite-journal
*.tmp
.*.tmp
.DS_Store
"""


def render_template(template: str, **values: str) -> str:
    text = (template_dir() / template).read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


# ---- repository ---------------------------------------------------------
def init_memory_dir(path: Path, git: bool = True) -> tuple[Path, bool]:
    """Create (or complete) a memory directory. Idempotent. Returns (path, created_new)."""
    path = path.expanduser().resolve()
    created = not (path / MARKER).exists()
    path.mkdir(parents=True, exist_ok=True)
    if created:
        atomic_write(
            path / MARKER,
            f"schema_version: {SCHEMA_VERSION}\ncreated: '{now_iso()}'\ngenerator: engineering-memory\n",
        )
    for d in TOP_DIRS:
        (path / d).mkdir(exist_ok=True)
        keep = path / d / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")
    gi = path / ".gitignore"
    if not gi.exists():
        atomic_write(gi, GITIGNORE)
    # Agent instructions live in the memory dir so any agent opened here knows the rules.
    for target, src in (("CLAUDE.md", "CLAUDE.md"), ("AGENTS.md", "CODEX.md")):
        if not (path / target).exists():
            atomic_write(path / target, (instructions_dir() / src).read_text(encoding="utf-8"))
    from .indexer import rebuild_index

    rebuild_index(path)
    if git and not (path / ".git").exists():
        try:
            subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            pass  # git is optional for the memory directory itself
    return path, created


# ---- projects -----------------------------------------------------------
def add_project(
    memory_dir: Path,
    name: str,
    *,
    repo_name: str | None = None,
    remote: str | None = None,
    ticket_prefixes: list[str] | None = None,
    description: str = "",
) -> Path:
    require_memory_dir(memory_dir)
    slug = slugify(name)
    pdir = project_dir(memory_dir, slug)
    pfile = pdir / "PROJECT.md"
    with memory_lock(memory_dir):
        if pfile.exists():
            meta = read_meta(pfile)
            body_text = pfile.read_text(encoding="utf-8").split("---", 2)[-1]
        else:
            meta = {
                "schema_version": SCHEMA_VERSION,
                "name": name,
                "slug": slug,
                "created": now_iso(),
                "description": description,
                "repos": [],
                "ticket_prefixes": [],
            }
            body_text = "\n" + render_template("project.md", name=name, description=description or "")
        repos = list(meta.get("repos") or [])
        if repo_name or remote:
            entry = {"name": repo_name or Path(remote or "").stem, "remote": remote}
            if not any(r.get("name") == entry["name"] and r.get("remote") == entry["remote"] for r in repos):
                repos.append(entry)
        meta["repos"] = repos
        prefixes = [str(p).upper() for p in (meta.get("ticket_prefixes") or [])]
        for p in ticket_prefixes or []:
            if p.upper() not in prefixes:
                prefixes.append(p.upper())
        meta["ticket_prefixes"] = prefixes
        if description:
            meta["description"] = description
        meta["updated"] = now_iso()
        atomic_write(pfile, dump_frontmatter(meta, body_text))
        (pdir / "tickets").mkdir(exist_ok=True)
        keep = pdir / "tickets" / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")
    return pdir


# ---- tickets ------------------------------------------------------------
def create_ticket(
    memory_dir: Path,
    project_slug: str,
    ticket: str,
    *,
    title: str | None = None,
    ticket_type: str = "feature",
    repository: dict | None = None,
) -> tuple[Path, bool]:
    """Create a ticket memory unless one already exists (duplicate protection)."""
    require_memory_dir(memory_dir)
    ticket = normalize_ticket(ticket)
    if ticket_type not in TYPES:
        raise MemoryError_(f"type must be one of {', '.join(TYPES)}")
    existing = find_ticket_dir(memory_dir, ticket)
    if existing:
        return existing, False
    if not (project_dir(memory_dir, project_slug) / "PROJECT.md").exists():
        raise MemoryError_(f"Project '{project_slug}' is not registered. Run `memory project add`.")
    tdir = ticket_dir_for(memory_dir, project_slug, ticket)
    ts = now_iso()
    title = title or "unknown"
    with memory_lock(memory_dir):
        if find_ticket_dir(memory_dir, ticket):  # re-check under lock
            return find_ticket_dir(memory_dir, ticket), False  # type: ignore[return-value]
        tdir.mkdir(parents=True, exist_ok=True)
        meta = {
            "schema_version": SCHEMA_VERSION,
            "ticket": ticket,
            "title": title,
            "project": project_slug,
            "type": ticket_type,
            "status": "in-progress",
            "created": ts,
            "updated": ts,
            "repository": repository or {},
            "branches": [],
            "commits": [],
            "pull_requests": [],
            "links": [],
            "team": [],
            "tags": [],
        }
        vals = {"ticket": ticket, "title": title}
        atomic_write(tdir / "MEMORY.md", dump_frontmatter(meta, render_template("feature.md", **vals)))
        side = {
            "timeline.md": ("timeline", "timeline.md"),
            "technical-decisions.md": ("technical-decisions", "decision.md"),
            "discussions.md": ("discussions", "discussion.md"),
            "testing.md": ("testing", "testing.md"),
        }
        for fname, (kind, tpl) in side.items():
            smeta = {"schema_version": SCHEMA_VERSION, "ticket": ticket, "kind": kind, "created": ts, "updated": ts}
            atomic_write(tdir / fname, dump_frontmatter(smeta, render_template(tpl, **vals)))
    assert all((tdir / f).exists() for f in TICKET_FILES)
    return tdir, True

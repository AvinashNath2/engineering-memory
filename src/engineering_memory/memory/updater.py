"""Incremental, history-preserving updates of a ticket memory."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..security import redact_secrets, sanitize_url
from ..store import (
    atomic_write,
    dump_frontmatter,
    memory_lock,
    now_iso,
    parse_frontmatter,
    parse_ts,
    iso,
    read_text,
    sha1,
    write_if_unchanged,
)
from .entries import Entry, EntryError, append_entry, find_duplicate, render
from .layout import STATUSES, TYPES, MemoryError_, find_ticket_dir, normalize_ticket

# section key -> (file, heading)
SECTIONS: dict[str, tuple[str, str]] = {
    "summary": ("MEMORY.md", "Summary"),
    "git": ("MEMORY.md", "Git"),
    "implementation": ("MEMORY.md", "Implementation"),
    "code-changes": ("MEMORY.md", "Code changes"),
    "team": ("MEMORY.md", "Team"),
    "docs": ("MEMORY.md", "Documentation"),
    "sources": ("MEMORY.md", "Sources"),
    "open-items": ("MEMORY.md", "Open items"),
    "decisions": ("technical-decisions.md", "Decisions"),
    "discussions": ("discussions.md", "Discussions"),
    "testing": ("testing.md", "Testing"),
    "timeline": ("timeline.md", "Events"),
}
SETTABLE_META = ("title", "status", "type")


@dataclass
class EntryRequest:
    section: str
    text: str
    confidence: str = "unconfirmed"
    source: str | None = None
    when: str | None = None
    key: str | None = None


@dataclass
class PersonRequest:
    name: str
    role: str = "unknown"
    source: str | None = None
    confidence: str | None = None  # default: verified if source else unconfirmed


@dataclass
class LinkRequest:
    url: str
    label: str = ""
    kind: str = "docs"  # docs | ticket | mr | other


@dataclass
class UpdateResult:
    ticket: str
    path: Path
    added: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    redactions: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added)


@dataclass
class _Doc:
    path: Path
    meta: dict[str, Any]
    body: str
    original_hash: str | None
    dirty: bool = False


class TicketSession:
    """Loads a ticket's five files, applies additive edits, and saves them with conflict checks."""

    def __init__(self, tdir: Path):
        self.tdir = tdir
        self.docs: dict[str, _Doc] = {}
        for name in ("MEMORY.md", "timeline.md", "technical-decisions.md", "discussions.md", "testing.md"):
            p = tdir / name
            if not p.exists():
                raise MemoryError_(f"{p} is missing; run `memory validate` to diagnose")
            text = read_text(p)
            meta, body = parse_frontmatter(text)
            self.docs[name] = _Doc(p, meta, body, sha1(text))
        self.redactions = 0
        self.touched = False

    @property
    def meta(self) -> dict[str, Any]:
        return self.docs["MEMORY.md"].meta

    @property
    def ticket(self) -> str:
        return str(self.meta.get("ticket", self.tdir.name))

    def clean(self, text: str) -> str:
        cleaned, n = redact_secrets(text)
        self.redactions += n
        return cleaned

    def add(self, section_key: str, entry: Entry) -> str | None:
        """Add an entry. Returns None on success or a reason string when skipped."""
        if section_key not in SECTIONS:
            raise MemoryError_(f"Unknown section '{section_key}'. Choose from: {', '.join(SECTIONS)}")
        fname, heading = SECTIONS[section_key]
        entry.text = self.clean(entry.text)
        if entry.source:
            entry.source = self.clean(entry.source)
        if not entry.when:
            entry.when = now_iso() if section_key == "timeline" else now_iso()[:10]
        entry.validate()
        doc = self.docs[fname]
        reason = find_duplicate(doc.body, heading, section_key, entry)
        if reason:
            return reason
        doc.body = append_entry(doc.body, heading, render(section_key, entry), sort_timeline=(section_key == "timeline"))
        doc.dirty = True
        self.touched = True
        return None

    def set_meta(self, key: str, value: Any) -> None:
        self.meta[key] = value
        self.docs["MEMORY.md"].dirty = True
        self.touched = True

    def add_to_list(self, key: str, value: Any) -> bool:
        lst = self.meta.setdefault(key, [])
        if value in lst:
            return False
        lst.append(value)
        self.docs["MEMORY.md"].dirty = True
        self.touched = True
        return True

    def save(self) -> list[Path]:
        if not self.touched:
            return []
        ts = now_iso()
        self.docs["MEMORY.md"].dirty = True
        written: list[Path] = []
        for name, doc in self.docs.items():
            if not doc.dirty:
                continue
            doc.meta["updated"] = ts
            write_if_unchanged(doc.path, dump_frontmatter(doc.meta, doc.body), doc.original_hash)
            written.append(doc.path)
        return written


def _open_ticket(memory_dir: Path, ticket: str) -> tuple[str, Path]:
    ticket = normalize_ticket(ticket)
    tdir = find_ticket_dir(memory_dir, ticket)
    if not tdir:
        raise MemoryError_(f"No memory for {ticket}. Run `memory start {ticket}` first.")
    return ticket, tdir


def daily_log(memory_dir: Path, ticket: str, message: str) -> None:
    now = datetime.now(timezone.utc)
    path = memory_dir / "daily-logs" / f"{now:%Y-%m-%d}.md"
    existing = read_text(path) if path.exists() else f"# Daily log {now:%Y-%m-%d}\n\n"
    line = f"- {now:%H:%M:%S}Z **{ticket}** {message}\n"
    atomic_write(path, existing.rstrip("\n") + "\n" + line)


def update_ticket(
    memory_dir: Path,
    ticket: str,
    *,
    entries: list[EntryRequest] | None = None,
    people: list[PersonRequest] | None = None,
    links: list[LinkRequest] | None = None,
    set_meta: dict[str, str] | None = None,
    add_tags: list[str] | None = None,
    log: bool = True,
) -> UpdateResult:
    """Apply additive changes to a ticket memory. Nothing is ever deleted or overwritten."""
    ticket, tdir = _open_ticket(memory_dir, ticket)
    result = UpdateResult(ticket=ticket, path=tdir)
    with memory_lock(memory_dir):
        s = TicketSession(tdir)
        new_ids: list[str] = []

        def note(section: str, e: Entry) -> None:
            reason = s.add(section, e)
            label = f"{section}: {e.text.splitlines()[0][:80]}"
            if reason:
                result.skipped.append(f"{label} ({reason})")
            else:
                result.added.append(label)
                new_ids.append(f"{section}|{e.key or e.text}")

        for req in entries or []:
            try:
                note(req.section, Entry(req.text, req.confidence, req.source, req.when or "", req.key))
            except EntryError as exc:
                raise MemoryError_(f"{req.section}: {exc}") from exc

        for p in people or []:
            conf = p.confidence or ("verified" if p.source else "unconfirmed")
            role = (p.role or "unknown").strip() or "unknown"
            name = s.clean(p.name.strip())
            member = {"name": name, "role": role, "confidence": conf}
            if p.source:
                member["source"] = s.clean(p.source)
            try:
                text = f"**{name}** — {role}"
                reason = s.add(
                    "team",
                    Entry(text, conf, p.source, key=f"person:{name.lower()}|{role.lower()}"),
                )
            except EntryError as exc:
                raise MemoryError_(f"team: {exc}") from exc
            if reason:
                result.skipped.append(f"team: {name} ({reason})")
            else:
                s.add_to_list("team", member)
                result.added.append(f"team: {name} — {role}")
                new_ids.append(f"person:{name.lower()}|{role.lower()}")

        for ln in links or []:
            url = sanitize_url(ln.url.strip())
            label = ln.label.strip() or url
            if not re.match(r"^(https?://|mailto:)", url):
                result.warnings.append(f"ignored link without http(s) scheme: {url}")
                continue
            if ln.kind == "mr":
                section, store_key = "git", "pull_requests"
            elif ln.kind == "docs":
                section, store_key = "docs", "links"
            else:
                section, store_key = "docs", "links"
            if s.add_to_list(store_key, {"label": label, "url": url, "kind": ln.kind}):
                reason = s.add(section, Entry(f"[{label}]({url})", "verified", "provided link", key=f"link:{url}"))
                if not reason:
                    result.added.append(f"{section}: link {label}")
                    new_ids.append(f"link:{url}")
            else:
                result.skipped.append(f"link {url} (already recorded)")

        for key, value in (set_meta or {}).items():
            if key not in SETTABLE_META:
                raise MemoryError_(f"Cannot set '{key}'. Allowed: {', '.join(SETTABLE_META)}")
            value = s.clean(value.strip())
            if key == "status" and value not in STATUSES:
                raise MemoryError_(f"status must be one of {', '.join(STATUSES)}")
            if key == "type" and value not in TYPES:
                raise MemoryError_(f"type must be one of {', '.join(TYPES)}")
            old = s.meta.get(key)
            if old == value:
                result.skipped.append(f"{key} (unchanged)")
                continue
            s.set_meta(key, value)
            if key == "title":
                mem = s.docs["MEMORY.md"]
                mem.body = re.sub(r"^# .*$", f"# {ticket} — {value}", mem.body, count=1, flags=re.M)
            msg = f"{key.capitalize()} changed: {old} → {value}"
            note("timeline", Entry(msg, "verified", "memory update --set", key=f"meta:{key}:{old}->{value}"))
            result.added.append(f"{key}: {old} → {value}")

        for tag in add_tags or []:
            if s.add_to_list("tags", tag.strip().lower()):
                result.added.append(f"tag: {tag}")

        counts: dict[str, int] = {}
        for a in result.added:
            sec = a.split(":")[0]
            if sec in SECTIONS and sec != "timeline":
                counts[sec] = counts.get(sec, 0) + 1
        if counts:
            digest = sha1("|".join(sorted(new_ids)))[:10]
            summary = ", ".join(f"{k} (+{v})" for k, v in counts.items())
            s.add("timeline", Entry(f"Memory updated: {summary}", "verified", "memory update", key=f"update:{digest}"))
        result.redactions = s.redactions
        s.save()
    if result.changed and log:
        daily_log(memory_dir, ticket, f"updated {len(result.added)} item(s)")
    return result


# ---- Git activity -------------------------------------------------------
def record_activity(memory_dir: Path, ticket: str, activity, *, pr_links: list[str] | None = None) -> UpdateResult:
    """Record branch, commits, changed file paths and PR references from a Git Activity."""
    ticket, tdir = _open_ticket(memory_dir, ticket)
    result = UpdateResult(ticket=ticket, path=tdir)
    repo = activity.repo
    with memory_lock(memory_dir):
        s = TicketSession(tdir)
        if repo.name and not s.meta.get("repository"):
            s.set_meta("repository", {"name": repo.name, "remote": repo.remote})

        def add(section: str, e: Entry, label: str) -> None:
            reason = s.add(section, e)
            (result.skipped if reason else result.added).append(f"{section}: {label}" + (f" ({reason})" if reason else ""))

        if repo.branch:
            if s.add_to_list("branches", repo.branch):
                when = repo.branch_created or None
                created = f" (created {parse_ts(when).strftime('%Y-%m-%d')})" if when else ""
                add("git", Entry(f"Branch `{repo.branch}` in `{repo.name}`{created}", "verified",
                                 "git", key=f"branch:{repo.name}:{repo.branch}"), f"branch {repo.branch}")
        for c in activity.commits:
            if s.add_to_list("commits", c.hash):
                when_dt = parse_ts(c.date)
                text = f"`{c.short}` {c.subject} — {c.author}"
                add("git", Entry(text, "verified", f"commit {c.short}", when=iso(when_dt)[:10], key=f"commit:{c.hash}"),
                    f"commit {c.short}")
                add("timeline", Entry(f"Commit `{c.short}`: {c.subject}", "verified", f"commit {c.short}",
                                      when=iso(when_dt), key=f"commit-tl:{c.hash}"), f"commit {c.short}")
            for f in c.files:
                status, path = f[0], f[2:]
                label = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied"}.get(status, status)
                reason = s.add(
                    "code-changes",
                    Entry(f"`{path}` ({label})", "verified", f"commit {c.short}", key=f"file:{path}"),
                )
                if not reason:
                    result.added.append(f"code-changes: {path}")
        for ref in activity.pr_refs:
            if s.add_to_list("pull_requests", {"ref": ref}):
                add("git", Entry(f"Pull/merge request reference `{ref}` found in commit messages", "verified",
                                 "commit message", key=f"prref:{ref}"), f"PR {ref}")
        for url in pr_links or []:
            url = sanitize_url(url)
            if s.add_to_list("pull_requests", {"url": url}):
                add("git", Entry(f"Pull/merge request: {url}", "verified", "provided link", key=f"prurl:{url}"), f"PR {url}")
        if activity.excluded_files:
            result.warnings.append(f"{activity.excluded_files} sensitive file path(s) were excluded from capture")
        if not activity.commits:
            result.warnings.append("no commits found to capture")
        result.redactions = s.redactions
        s.save()
    if result.changed:
        daily_log(memory_dir, ticket, f"captured git activity ({len(result.added)} new item(s))")
    return result

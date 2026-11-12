"""Ingest ticket / Confluence / merge-request material into a ticket memory.

Deterministic part (no AI): store sanitised copies of the sources, extract links and
explicitly-labelled people (``Assignee: …``), and register everything in the memory.
The AI agent then reads the stored sources and writes the narrative sections.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..security import is_sensitive_path, redact_secrets
from ..store import atomic_write, memory_lock, sha1, slugify
from .layout import MemoryError_, find_ticket_dir, normalize_ticket
from .updater import EntryRequest, LinkRequest, PersonRequest, UpdateResult, update_ticket

KINDS = ("ticket", "docs", "mr", "note")
KIND_LABEL = {"ticket": "Ticket", "docs": "Confluence/documentation page", "mr": "Merge request", "note": "Note"}

_ROLE_FIELDS = {
    "assignee": "assignee", "reporter": "reporter", "owner": "owner",
    "reviewer": "reviewer", "reviewers": "reviewer", "approved by": "approver", "approver": "approver",
    "qa": "QA", "tester": "QA", "tested by": "QA", "qa engineer": "QA",
    "backend": "backend", "frontend": "frontend", "developer": "developer",
    "author": "author", "created by": "author",
}
_FIELD_RE = re.compile(
    r"^\s*(?:[-*>]\s*)?(?:\*\*)?(" + "|".join(sorted((re.escape(k) for k in _ROLE_FIELDS), key=len, reverse=True)) +
    r")(?:\*\*)?\s*[:：]\s*(?:\*\*)?\s*(.+?)\s*$",
    re.I | re.M,
)
_NO_NAME = {"", "-", "none", "n/a", "na", "unassigned", "unknown", "tbd", "nobody", "pending"}
_URL_RE = re.compile(r"https?://[^\s<>\")\]]+")
_DIFF_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


@dataclass
class SourceFile:
    kind: str
    path: Path


@dataclass
class IngestResult:
    ticket: str
    saved: list[Path] = field(default_factory=list)  # relative to memory dir
    update: UpdateResult | None = None
    people_found: list[tuple[str, str]] = field(default_factory=list)
    links_found: list[tuple[str, str]] = field(default_factory=list)
    title_found: str | None = None
    warnings: list[str] = field(default_factory=list)
    redactions: int = 0


# ---- diff handling ------------------------------------------------------
def looks_like_diff(text: str) -> bool:
    return bool(re.search(r"^diff --git ", text, re.M)) or bool(re.search(r"^@@ .+ @@", text, re.M) and re.search(r"^\+\+\+ ", text, re.M))


def split_diff(text: str) -> tuple[str, list[tuple[str, int, int]]]:
    """Separate prose from a unified diff. Returns (prose, [(path, added, removed)]); code is dropped."""
    prose: list[str] = []
    stats: list[tuple[str, int, int]] = []
    current: list | None = None
    in_diff = False
    for ln in text.splitlines():
        m = _DIFF_FILE.match(ln)
        if m:
            in_diff = True
            current = [m.group(2), 0, 0]
            stats.append(current)  # type: ignore[arg-type]
            continue
        if in_diff and current is not None:
            if ln.startswith("+") and not ln.startswith("+++"):
                current[1] += 1
            elif ln.startswith("-") and not ln.startswith("---"):
                current[2] += 1
            continue
        prose.append(ln)
    return "\n".join(prose).strip(), [(p, a, r) for p, a, r in stats]


# ---- extraction ---------------------------------------------------------
def _clean_names(raw: str) -> list[str]:
    raw = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", raw)  # markdown links
    names: list[str] = []
    for part in re.split(r"\s*(?:,|;|\band\b|&)\s*", raw):
        part = re.sub(r"\(.*?\)", "", part).replace("@", "").replace("*", "").strip(" .`_")
        if part.lower() in _NO_NAME or len(part) > 60 or len(part) < 2:
            continue
        names.append(part)
    return names


def extract_people(text: str, kind: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for m in _FIELD_RE.finditer(text):
        role = _ROLE_FIELDS[m.group(1).lower()]
        if role == "author" and kind == "mr":
            role = "MR author"
        for name in _clean_names(m.group(2)):
            if (name, role) not in found:
                found.append((name, role))
    return found


def classify_url(url: str) -> str | None:
    u = url.lower()
    if re.search(r"/(merge_requests|pull|pull-requests|-/merge_requests)/\d+", u):
        return "mr"
    if "/browse/" in u or "/jira/" in u or "/issues/" in u:
        return "ticket"
    if "atlassian.net/wiki" in u or "/confluence" in u or "/wiki/" in u or "/display/" in u or "notion.so" in u:
        return "docs"
    return None


def extract_links(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?'")
        kind = classify_url(url)
        if kind and (url, kind) not in out:
            out.append((url, kind))
    return out[:20]


def extract_title(text: str, ticket: str) -> str | None:
    m = re.search(r"^\s*(?:\*\*)?(?:title|summary)(?:\*\*)?\s*[:：]\s*(.+?)\s*$", text, re.I | re.M)
    if not m:
        m = re.search(r"^#\s+(.+?)\s*$", text, re.M)
    if not m:
        return None
    title = re.sub(rf"^\W*{re.escape(ticket)}\W*", "", m.group(1), flags=re.I).strip(" -—:*")
    return title[:200] or None


# ---- main entry ---------------------------------------------------------
def ingest_sources(
    memory_dir: Path,
    ticket: str,
    sources: list[SourceFile],
    extra_sensitive: list[str] | None = None,
) -> IngestResult:
    ticket = normalize_ticket(ticket)
    tdir = find_ticket_dir(memory_dir, ticket)
    if not tdir:
        raise MemoryError_(f"No memory for {ticket}. Run `memory start {ticket}` first.")
    if not sources:
        raise MemoryError_("Nothing to ingest: pass --ticket, --docs, --mr or --note files.")
    result = IngestResult(ticket=ticket)
    attach_dir = memory_dir / "attachments" / ticket
    entries: list[EntryRequest] = []
    people: list[PersonRequest] = []
    links: list[LinkRequest] = []
    title: str | None = None

    for src in sources:
        if src.kind not in KINDS:
            raise MemoryError_(f"Unknown source kind '{src.kind}'")
        path = src.path.expanduser()
        if not path.is_file():
            raise MemoryError_(f"Source file not found: {path}")
        if is_sensitive_path(path.name, extra_sensitive):
            result.warnings.append(f"skipped sensitive file name: {path.name}")
            continue
        raw = path.read_bytes()
        if b"\x00" in raw[:4096]:
            result.warnings.append(f"skipped binary file: {path.name}")
            continue
        text = raw.decode("utf-8", errors="replace")
        text, n = redact_secrets(text)
        result.redactions += n

        stored = text
        note_suffix = ""
        if src.kind == "mr" and looks_like_diff(text):
            prose, stats = split_diff(text)
            lines = [prose, "", "## Diff summary (file paths and line counts only; code omitted)", ""]
            lines += [f"- `{p}` (+{a} / -{r})" for p, a, r in stats] or ["- (no files parsed)"]
            stored = "\n".join(lines).strip()
            note_suffix = f"; diff reduced to {len(stats)} file path(s)"

        digest = sha1(stored)[:8]
        fname = f"{src.kind}-{digest}-{slugify(path.stem)[:40]}.md"
        dest = attach_dir / fname
        header = (
            f"<!-- imported by engineering-memory from '{path.name}' as {src.kind}; "
            f"secrets redacted: {n}{note_suffix} -->\n\n"
        )
        with memory_lock(memory_dir):
            if not dest.exists():
                atomic_write(dest, header + stored)
        rel = os.path.relpath(dest, tdir).replace(os.sep, "/")
        result.saved.append(dest.relative_to(memory_dir))
        label = f"{KIND_LABEL[src.kind]}: [{fname}]({rel})"
        entries.append(EntryRequest("sources", label, "verified", f"ingest of {path.name}", key=f"source:{digest}"))

        # deterministic extraction (explicit fields only, never guesses)
        origin = f"{src.kind} {path.name}"
        for name, role in extract_people(stored, src.kind):
            people.append(PersonRequest(name, role, origin, "verified"))
            result.people_found.append((name, role))
        for url, kind in extract_links(stored):
            links.append(LinkRequest(url, label=f"{kind} link from {path.name}", kind=kind))
            result.links_found.append((url, kind))
        if src.kind == "ticket" and not title:
            title = extract_title(stored, ticket)
        if ticket not in stored.upper():
            result.warnings.append(
                f"{path.name} never mentions {ticket}; check that it belongs to this ticket"
            )

    meta_set: dict[str, str] = {}
    if title:
        from .layout import read_meta

        if str(read_meta(tdir / "MEMORY.md").get("title", "unknown")) == "unknown":
            meta_set["title"] = title
            result.title_found = title
    result.update = update_ticket(
        memory_dir, ticket, entries=entries, people=people, links=links, set_meta=meta_set
    )
    result.redactions += result.update.redactions
    return result

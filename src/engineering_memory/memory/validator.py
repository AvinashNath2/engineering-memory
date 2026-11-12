"""Validate memory files: schema, links, timestamps, duplicates, secrets, evidence rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote

from ..security import find_secrets
from ..store import FrontmatterError, now_utc, parse_frontmatter, parse_ts, read_text
from .entries import CONFIDENCE, ENTRY_START, ID_RE
from .indexer import _walk_md
from .layout import MARKER, STATUSES, TICKET_FILES, TYPES, iter_ticket_dirs

REQUIRED_META = ("schema_version", "ticket", "project", "status", "created", "updated")
_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


@dataclass(frozen=True)
class Issue:
    level: str  # "error" | "warning"
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.level.upper():7} {self.path}: {self.message}"


def _entry_blocks(body: str):
    block: list[str] = []
    for ln in body.split("\n"):
        if ln.startswith("- "):
            if block:
                yield "\n".join(block)
            block = [ln]
        elif block and ln.startswith("  "):
            block.append(ln)
        elif block:
            yield "\n".join(block)
            block = []
    if block:
        yield "\n".join(block)


def validate(memory_dir: Path) -> list[Issue]:
    issues: list[Issue] = []
    rel = lambda p: p.relative_to(memory_dir).as_posix()  # noqa: E731

    def err(p: Path, m: str) -> None:
        issues.append(Issue("error", rel(p) if p.is_absolute() else str(p), m))

    def warn(p: Path, m: str) -> None:
        issues.append(Issue("warning", rel(p) if p.is_absolute() else str(p), m))

    if not (memory_dir / MARKER).exists():
        issues.append(Issue("error", ".", f"missing {MARKER}; not an initialised memory directory"))
        return issues
    for f in ("INDEX.md", "PROJECTS.md", "PEOPLE.md"):
        if not (memory_dir / f).exists():
            warn(memory_dir / f, "missing; run `memory index`")

    seen_tickets: dict[str, Path] = {}
    now = now_utc() + timedelta(days=1)

    # ---- per-ticket checks
    for tdir in iter_ticket_dirs(memory_dir):
        ticket = tdir.name
        if ticket in seen_tickets:
            err(tdir, f"duplicate memory for {ticket} (also {rel(seen_tickets[ticket])})")
        seen_tickets[ticket] = tdir
        project = tdir.parent.parent.name
        if not (tdir.parent.parent / "PROJECT.md").exists():
            err(tdir, f"project '{project}' has no PROJECT.md")
        for fname in TICKET_FILES:
            if not (tdir / fname).exists():
                err(tdir / fname, "required file is missing")
        mem = tdir / "MEMORY.md"
        if not mem.exists():
            continue
        try:
            meta, _ = parse_frontmatter(read_text(mem))
        except FrontmatterError as exc:
            err(mem, str(exc))
            continue
        for k in REQUIRED_META:
            if k not in meta:
                err(mem, f"frontmatter is missing '{k}'")
        if meta.get("ticket") and str(meta["ticket"]) != ticket:
            err(mem, f"frontmatter ticket '{meta['ticket']}' does not match folder '{ticket}'")
        if meta.get("project") and str(meta["project"]) != project:
            err(mem, f"frontmatter project '{meta['project']}' does not match folder '{project}'")
        if meta.get("status") and str(meta["status"]) not in STATUSES:
            err(mem, f"invalid status '{meta['status']}'")
        if meta.get("type") and str(meta["type"]) not in TYPES:
            err(mem, f"invalid type '{meta['type']}'")
        try:
            created, updated = parse_ts(meta["created"]), parse_ts(meta["updated"])
            if created > updated:
                err(mem, "created is later than updated")
            if updated > now:
                err(mem, "updated is in the future")
        except (KeyError, ValueError, TypeError):
            err(mem, "created/updated are not valid ISO timestamps")
        for m in meta.get("team") or []:
            if isinstance(m, dict) and m.get("confidence") == "verified" and not m.get("source"):
                err(mem, f"team member '{m.get('name')}' is verified without a source")

        # timeline chronology
        tl = tdir / "timeline.md"
        if tl.exists():
            _, body = parse_frontmatter(read_text(tl))
            last = None
            for ln in body.splitlines():
                m = ENTRY_START.match(ln)
                if not m:
                    continue
                try:
                    ts = parse_ts(m.group(1))
                except ValueError:
                    err(tl, f"invalid timestamp '{m.group(1)}'")
                    continue
                if ts > now:
                    err(tl, f"timeline entry in the future: {m.group(1)}")
                if last and ts < last:
                    err(tl, f"timeline is not chronological at {m.group(1)}")
                last = ts if not last or ts > last else last

    # ---- per-file checks
    for p in _walk_md(memory_dir):
        text = read_text(p)
        r = rel(p)
        is_generated = r in ("INDEX.md", "PEOPLE.md", "PROJECTS.md") or r.startswith("people/")
        try:
            meta, body = parse_frontmatter(text)
        except FrontmatterError as exc:
            err(p, str(exc))
            continue
        if r.startswith("attachments/"):
            body = ""  # raw sources: only secret scan below
        # secrets
        for f in find_secrets(text):
            (err if f.severity == "error" else warn)(p, f"possible secret ({f.kind}) at line {f.line}")
        # entries
        ids: dict[str, int] = {}
        for block in _entry_blocks(body):
            if not block.startswith("- **"):
                continue
            m = ENTRY_START.match(block)
            if not m:
                warn(p, f"entry without confidence tag: {block.splitlines()[0][:70]}")
                continue
            conf = m.group(2)
            if conf not in CONFIDENCE:
                err(p, f"unknown confidence '{conf}'")
            if conf == "verified" and "(source:" not in block:
                err(p, f"[verified] entry has no source: {block.splitlines()[0][:70]}")
            idm = ID_RE.search(block)
            if idm:
                ids[idm.group(1)] = ids.get(idm.group(1), 0) + 1
        for i, c in ids.items():
            if c > 1:
                err(p, f"duplicate entry id {i} appears {c} times")
        # links (broken links in generated views are warnings: `memory index` repairs them)
        report = warn if is_generated and not r.startswith("people/") else err
        for target in _LINK.findall(body):
            if re.match(r"^[a-z][a-z0-9+.\-]*:", target, re.I) or target.startswith("#"):
                continue
            path_part = unquote(target.split("#", 1)[0])
            if path_part and not (p.parent / path_part).resolve().exists():
                report(p, f"broken link: {target}")
    return issues

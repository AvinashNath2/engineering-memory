"""Search Markdown memories without any AI.

Two interchangeable strategies:
* plain text scan (always available, no index needed)
* SQLite FTS5 (optional, rebuildable, used when present and fresh)
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..memory.indexer import FTS_FILE, _walk_md, fts_is_fresh
from ..store import parse_frontmatter, parse_ts, read_text

STOPWORDS = {
    "a", "an", "the", "of", "for", "to", "in", "on", "at", "by", "and", "or", "is", "was", "were", "are",
    "what", "who", "which", "when", "where", "why", "how", "did", "do", "does", "i", "my", "me", "we", "our",
    "with", "about", "from", "that", "this", "it", "any", "all",
}
AGENT_FILES = {"CLAUDE.md", "AGENTS.md"}
_TICKET = re.compile(r"^[a-z][a-z0-9]+-\d+$")


@dataclass
class Hit:
    path: str  # relative to memory dir
    score: float
    matched: int
    ticket: str | None = None
    project: str | None = None
    title: str | None = None
    snippets: list[tuple[int, str]] = field(default_factory=list)  # (line number, text)
    ticket_matched: int = 0  # distinct query terms matched anywhere in this ticket's files


@dataclass
class _Doc:
    rel: str
    text: str
    ticket: str | None
    project: str | None
    title: str | None
    updated: datetime | None


def parse_query(query: str) -> list[str]:
    """Quoted phrases stay intact; other words are lower-cased and stop-words dropped."""
    terms: list[str] = []
    for phrase in re.findall(r'"([^"]+)"', query):
        terms.append(phrase.strip().lower())
    rest = re.sub(r'"[^"]+"', " ", query)
    for tok in re.findall(r"[\w][\w\-.]*", rest.lower()):
        tok = tok.strip(".-")
        if len(tok) >= 2 and tok not in STOPWORDS:
            terms.append(tok)
    seen: dict[str, None] = {}
    for t in terms:
        seen.setdefault(t, None)
    return list(seen)


def _load_docs(memory_dir: Path) -> list[_Doc]:
    ticket_meta: dict[str, tuple[str, datetime | None, str]] = {}
    docs: list[_Doc] = []
    for p in _walk_md(memory_dir):
        rel = p.relative_to(memory_dir).as_posix()
        if rel in AGENT_FILES:  # instruction files are not memories
            continue
        text = read_text(p)
        parts = rel.split("/")
        ticket = project = title = None
        updated = None
        if len(parts) >= 4 and parts[0] == "projects" and parts[2] == "tickets":
            project, ticket = parts[1], parts[3]
        elif len(parts) >= 3 and parts[0] == "attachments":
            ticket = parts[1]
        elif len(parts) >= 2 and parts[0] == "projects":
            project = parts[1]
        if rel.endswith("/MEMORY.md") and ticket:
            try:
                meta, _ = parse_frontmatter(text)
                title = str(meta.get("title", ""))
                updated = parse_ts(meta["updated"]) if meta.get("updated") else None
                ticket_meta[ticket] = (title, updated, project or "")
            except Exception:
                pass
        docs.append(_Doc(rel, text, ticket, project, title, updated))
    for d in docs:  # share ticket-level title/updated with sibling files
        if d.ticket and d.ticket in ticket_meta:
            t, u, proj = ticket_meta[d.ticket]
            d.title = d.title or t
            d.updated = d.updated or u
            d.project = d.project or proj or None
        if d.updated is None:
            d.updated = datetime.fromtimestamp((memory_dir / d.rel).stat().st_mtime, tz=timezone.utc)
    return docs


def _score(doc: _Doc, terms: list[str]) -> tuple[float, set[str]]:
    low = doc.text.lower()
    head = (doc.rel + " " + (doc.title or "") + " " + (doc.ticket or "")).lower()
    headings = "\n".join(ln for ln in low.splitlines() if ln.startswith("#"))
    score, matched = 0.0, set()
    for t in terms:
        c = low.count(t)
        if c == 0 and t not in head:
            continue
        matched.add(t)
        score += min(c, 10)
        if t in head:
            score += 5
        if t in headings:
            score += 2
        if _TICKET.match(t) and t in low:
            score += 3
    # reward breadth: documents that match more distinct terms win
    return score * (1 + 0.5 * (len(matched) - 1)), matched


def _snippets(doc: _Doc, terms: list[str], limit: int = 3) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for i, ln in enumerate(doc.text.splitlines(), start=1):
        low = ln.lower()
        if any(t in low for t in terms):
            clean = re.sub(r"<!--.*?-->", "", ln).strip()
            if clean and not clean.startswith(("---", "schema_version")):
                out.append((i, clean[:180]))
                if len(out) >= limit:
                    break
    return out


def _fts_candidates(memory_dir: Path, terms: list[str]) -> dict[str, float] | None:
    try:
        con = sqlite3.connect(memory_dir / FTS_FILE)
        expr = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
        rows = con.execute("select path, bm25(docs) from docs where docs match ? order by bm25(docs)", (expr,)).fetchall()
        con.close()
    except sqlite3.Error:
        return None
    return {p: -float(r) for p, r in rows}  # bm25 is negative-better


def search(
    memory_dir: Path,
    query: str,
    *,
    project: str | None = None,
    person: str | None = None,
    since: str | None = None,
    limit: int = 10,
    use_fts: bool = True,
) -> tuple[list[Hit], str]:
    """Return (hits, strategy) where strategy is 'fts' or 'text'."""
    terms = parse_query(query)
    if not terms:
        return [], "text"
    docs = _load_docs(memory_dir)
    strategy = "text"
    candidates: set[str] | None = None
    if use_fts and fts_is_fresh(memory_dir):
        cand = _fts_candidates(memory_dir, terms)
        if cand is not None:
            candidates, strategy = set(cand), "fts"

    if project:
        docs = [d for d in docs if d.project == project]
    if since:
        cutoff = parse_ts(since)
        docs = [d for d in docs if d.updated and d.updated >= cutoff]
    if person:
        pl = person.lower()
        tickets = {d.ticket for d in docs if d.ticket and pl in d.text.lower()}
        docs = [d for d in docs if (d.ticket in tickets) or (not d.ticket and pl in d.text.lower())]

    scored: list[tuple[_Doc, float, set[str]]] = []
    per_ticket: dict[str, set[str]] = {}
    for d in docs:
        if candidates is not None and d.rel not in candidates:
            continue
        score, matched = _score(d, terms)
        if not matched:
            continue
        if d.rel.startswith("attachments/"):
            score *= 0.6  # raw sources rank below curated memory
        scored.append((d, score, matched))
        if d.ticket:
            per_ticket.setdefault(d.ticket, set()).update(matched)
    hits: list[Hit] = []
    for d, score, matched in scored:
        tm = len(per_ticket.get(d.ticket or "", matched)) if d.ticket else len(matched)
        hits.append(Hit(d.rel, round(score, 2), len(matched), d.ticket, d.project, d.title,
                        _snippets(d, terms), ticket_matched=max(tm, len(matched))))
    # tickets whose files together cover more of the query rank first, then the best single file
    hits.sort(key=lambda h: (h.ticket_matched, h.matched, h.score), reverse=True)
    return hits[:limit], strategy

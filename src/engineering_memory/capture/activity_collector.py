"""Combine Git data with ticket detection into one Activity snapshot."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..store import now_iso
from .git_collector import CommitInfo, RepoInfo, collect_commits, repo_info


def detect_ticket(text: str | None, pattern: str) -> str | None:
    """First ticket ID in ``text`` (e.g. a branch name), upper-cased. None if absent."""
    if not text:
        return None
    m = re.search(pattern, text)
    return m.group(0).upper() if m else None


def detect_all_tickets(text: str | None, pattern: str) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    for m in re.finditer(pattern, text):
        t = m.group(0).upper()
        if t not in seen:
            seen.append(t)
    return seen


@dataclass
class Activity:
    repo: RepoInfo
    ticket: str | None
    base_ref: str | None
    commits: list[CommitInfo] = field(default_factory=list)
    captured_at: str = field(default_factory=now_iso)

    @property
    def files(self) -> list[str]:
        """Unique changed paths (status stripped), first-seen order."""
        seen: dict[str, None] = {}
        for c in self.commits:
            for f in c.files:
                seen.setdefault(f[2:], None)
        return list(seen)

    @property
    def excluded_files(self) -> int:
        return sum(c.excluded_files for c in self.commits)

    @property
    def pr_refs(self) -> list[str]:
        seen: dict[str, None] = {}
        for c in self.commits:
            for r in c.pr_refs:
                seen.setdefault(r, None)
        return list(seen)


def collect_activity(
    cwd: Path,
    cfg: Config,
    *,
    ticket: str | None = None,
    limit: int | None = None,
    since: str | None = None,
) -> Activity:
    repo = repo_info(cwd)
    resolved = ticket.upper() if ticket else detect_ticket(repo.branch, cfg.ticket_pattern)
    commits, base = collect_commits(
        repo.root,
        branch=repo.branch,
        base_branches=cfg.base_branches,
        limit=limit or cfg.max_commits,
        since=since,
        extra_sensitive=cfg.extra_sensitive_patterns,
    )
    return Activity(repo=repo, ticket=resolved, base_ref=base, commits=commits)


def restrict_to_ticket(activity: Activity) -> tuple[Activity, bool]:
    """Without a base branch we cannot know which commits belong to the ticket.

    In that case keep only commits whose subject mentions the ticket. Returns (activity, restricted).
    """
    if activity.base_ref is not None or not activity.ticket:
        return activity, False
    t = activity.ticket.lower()
    activity.commits = [c for c in activity.commits if t in c.subject.lower()]
    return activity, True

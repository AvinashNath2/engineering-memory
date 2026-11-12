"""Collect Git activity with the Git CLI. Never reads file contents, only metadata."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..security import is_sensitive_path, redact_secrets, sanitize_url


class GitError(Exception):
    pass


@dataclass
class CommitInfo:
    hash: str
    author: str
    date: str  # ISO 8601 as reported by git
    subject: str
    files: list[str] = field(default_factory=list)  # sensitive paths already removed
    excluded_files: int = 0
    pr_refs: list[str] = field(default_factory=list)

    @property
    def short(self) -> str:
        return self.hash[:8]


@dataclass
class RepoInfo:
    root: Path
    name: str
    remote: str | None
    branch: str | None  # None when detached
    head: str | None
    branch_created: str | None = None


def run_git(args: list[str], cwd: Path, check: bool = True) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout if proc.returncode == 0 else ""


def find_repo_root(cwd: Path) -> Path:
    out = run_git(["rev-parse", "--show-toplevel"], cwd, check=False).strip()
    if not out:
        raise GitError(f"{cwd} is not inside a Git repository")
    return Path(out)


def repo_name_from(remote: str | None, root: Path) -> str:
    if remote:
        tail = re.split(r"[/:]", remote.rstrip("/"))[-1]
        return re.sub(r"\.git$", "", tail) or root.name
    return root.name


def repo_info(cwd: Path) -> RepoInfo:
    root = find_repo_root(cwd)
    remote = run_git(["config", "--get", "remote.origin.url"], root, check=False).strip() or None
    remote = sanitize_url(remote) if remote else None
    branch = run_git(["symbolic-ref", "--short", "-q", "HEAD"], root, check=False).strip() or None
    head = run_git(["rev-parse", "-q", "--verify", "HEAD"], root, check=False).strip() or None
    created = branch_creation_date(root, branch) if branch else None
    return RepoInfo(root, repo_name_from(remote, root), remote, branch, head, created)


def branch_creation_date(root: Path, branch: str) -> str | None:
    """Best effort: the oldest reflog entry for the branch (only exists on the machine that created it)."""
    out = run_git(["reflog", "show", "--date=iso-strict", "--format=%gd|%gs", branch], root, check=False)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    for ln in reversed(lines):
        sel, _, subject = ln.partition("|")
        if subject.startswith("branch: Created") or subject.startswith("checkout: moving"):
            m = re.search(r"\{(.+)\}", sel)
            if m:
                return m.group(1)
    return None


def find_base_ref(root: Path, branch: str | None, base_branches: list[str]) -> str | None:
    for cand in base_branches:
        if branch and cand == branch:
            continue
        if run_git(["rev-parse", "-q", "--verify", f"refs/heads/{cand}"], root, check=False).strip() or run_git(
            ["rev-parse", "-q", "--verify", f"refs/remotes/{cand}"], root, check=False
        ).strip():
            return cand
    return None


def extract_pr_refs(text: str) -> list[str]:
    """Pull request / merge request references mentioned in a commit message."""
    refs: list[str] = []

    def add(ref: str) -> None:
        if ref not in refs:
            refs.append(ref)

    for m in re.finditer(r"Merge pull request #(\d+)", text):
        add(f"#{m.group(1)}")
    for m in re.finditer(r"See merge request ([\w./\-]*!\d+)", text):
        add(m.group(1))
    for m in re.finditer(r"\(#(\d+)\)", text):
        add(f"#{m.group(1)}")
    for m in re.finditer(r"\bMR\s*!(\d+)", text, re.I):
        add(f"!{m.group(1)}")
    return refs


def collect_commits(
    root: Path,
    *,
    branch: str | None,
    base_branches: list[str],
    limit: int = 50,
    since: str | None = None,
    extra_sensitive: list[str] | None = None,
) -> tuple[list[CommitInfo], str | None]:
    """Commits unique to ``branch`` (vs. a base branch) or the last ``limit`` commits.

    Returns (commits oldest-first, base_ref_used).
    """
    if not run_git(["rev-parse", "-q", "--verify", "HEAD"], root, check=False).strip():
        return [], None  # repository without commits
    base = find_base_ref(root, branch, base_branches)
    rev = f"{base}..HEAD" if base else "HEAD"
    fmt = "%x1e%H%x1f%an%x1f%aI%x1f%s%x1f%b%x1d"
    args = ["log", f"--max-count={limit}", "--name-status", "-M", f"--format={fmt}"]
    if since:
        args.append(f"--since={since}")
    args.append(rev)
    out = run_git(args, root, check=False)
    commits: list[CommitInfo] = []
    for chunk in out.split("\x1e"):
        if not chunk.strip():
            continue
        header, _, filepart = chunk.partition("\x1d")
        fields = header.split("\x1f")
        if len(fields) < 5:
            continue
        h, author, date, subject, body = fields[0], fields[1], fields[2], fields[3], fields[4]
        files: list[str] = []
        excluded = 0
        for ln in filepart.strip().splitlines():
            parts = ln.split("\t")
            if len(parts) < 2:
                continue
            status, path = parts[0], parts[-1]  # for renames the new path is last
            label = path if not status.startswith("R") else f"{parts[1]} -> {parts[2]}"
            if any(is_sensitive_path(p, extra_sensitive) for p in parts[1:]):
                excluded += 1
                continue
            files.append(f"{status[0]} {label}")
        subject_clean, _ = redact_secrets(subject)
        commits.append(
            CommitInfo(
                hash=h.strip(),
                author=author,
                date=date,
                subject=subject_clean,
                files=files,
                excluded_files=excluded,
                pr_refs=extract_pr_refs(subject + "\n" + body),
            )
        )
    commits.reverse()  # oldest first
    return commits, base

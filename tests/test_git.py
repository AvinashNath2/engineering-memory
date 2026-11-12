from __future__ import annotations

import pytest

from engineering_memory.capture.activity_collector import (
    collect_activity,
    detect_all_tickets,
    detect_ticket,
    restrict_to_ticket,
)
from engineering_memory.capture.git_collector import GitError, extract_pr_refs, find_repo_root, repo_info
from engineering_memory.config import DEFAULT_TICKET_PATTERN, Config

from conftest import git

P = DEFAULT_TICKET_PATTERN


@pytest.mark.parametrize(
    "branch, expected",
    [
        ("feature/pay-1234-payment-retry", "PAY-1234"),
        ("PAY-99", "PAY-99"),
        ("bugfix/ABC2-7_fix", "ABC2-7"),
        ("user/dev/PROJ-123/more", "PROJ-123"),
        ("main", None),
        ("feature/no-ticket-here", None),
        (None, None),
    ],
)
def test_ticket_detection(branch, expected):
    assert detect_ticket(branch, P) == expected


def test_detect_all_tickets_unique_in_order():
    assert detect_all_tickets("PAY-1 and pay-2 and PAY-1", P) == ["PAY-1", "PAY-2"]


def test_pr_ref_extraction():
    text = "Merge pull request #42 from x\n\nSee merge request grp/proj!77\nfix (#13) MR !5"
    assert extract_pr_refs(text) == ["#42", "grp/proj!77", "#13", "!5"]
    assert extract_pr_refs("nothing here") == []


def test_not_a_repo(tmp_path):
    with pytest.raises(GitError):
        find_repo_root(tmp_path)


def test_repo_info_branch_and_name(repo):
    info = repo_info(repo)
    assert info.branch == "feature/pay-1234-payment-retry"
    assert info.name == "payment-service"
    assert info.remote is None
    assert info.head


def test_remote_credentials_are_stripped_and_name_derived(repo):
    git(repo, "remote", "add", "origin", "https://user:s3cr3t@github.com/acme/payments-api.git")
    info = repo_info(repo)
    assert info.remote == "https://github.com/acme/payments-api.git"
    assert "s3cr3t" not in (info.remote or "")
    assert info.name == "payments-api"


def test_collect_commits_branch_only_and_ordered(repo):
    act = collect_activity(repo, Config())
    assert act.ticket == "PAY-1234"
    assert act.base_ref == "main"
    assert [c.subject for c in act.commits] == ["PAY-1234: add retry with backoff (#42)", "PAY-1234: cap attempts"]
    assert all(len(c.hash) == 40 for c in act.commits)
    assert act.pr_refs == ["#42"]


def test_sensitive_files_never_captured(repo):
    act = collect_activity(repo, Config())
    assert act.files == ["src/retry.py"]
    assert act.excluded_files == 2  # .env and deploy.pem
    blob = " ".join(f for c in act.commits for f in c.files)
    assert ".env" not in blob and ".pem" not in blob


def test_commit_subject_secrets_redacted(repo):
    (repo / "x.txt").write_text("x")
    git(repo, "add", "x.txt")
    git(repo, "commit", "-qm", "PAY-1234: use password=hunter2hunter2 for tests")
    act = collect_activity(repo, Config())
    assert "hunter2" not in act.commits[-1].subject


def test_explicit_ticket_overrides_branch(repo):
    assert collect_activity(repo, Config(), ticket="abc-9").ticket == "ABC-9"


def test_limit(repo):
    assert len(collect_activity(repo, Config(), limit=1).commits) == 1


def test_since_filters_old_commits(tmp_path, monkeypatch):
    r = tmp_path / "dated"
    r.mkdir()
    git(r, "init", "-q", "-b", "trunk")
    for when, msg in (("2020-01-01T10:00:00Z", "PAY-5: old"), (None, "PAY-5: recent")):
        if when:
            monkeypatch.setenv("GIT_AUTHOR_DATE", when)
            monkeypatch.setenv("GIT_COMMITTER_DATE", when)
        else:
            monkeypatch.delenv("GIT_AUTHOR_DATE")
            monkeypatch.delenv("GIT_COMMITTER_DATE")
        (r / f"{msg[-3:]}.txt").write_text(msg)
        git(r, "add", ".")
        git(r, "commit", "-qm", msg)
    subjects = [c.subject for c in collect_activity(r, Config(), ticket="PAY-5", since="2024-01-01").commits]
    assert subjects == ["PAY-5: recent"]


def test_renames_are_recorded(repo):
    git(repo, "mv", "src/retry.py", "src/backoff.py")
    git(repo, "commit", "-qm", "PAY-1234: rename")
    act = collect_activity(repo, Config())
    assert any(f.startswith("R src/retry.py -> src/backoff.py") for c in act.commits for f in c.files)


def test_empty_repository_has_no_commits(tmp_path):
    r = tmp_path / "empty"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    act = collect_activity(r, Config(), ticket="PAY-1")
    assert act.commits == []


def test_detached_head(repo):
    git(repo, "checkout", "-q", "--detach")
    info = repo_info(repo)
    assert info.branch is None


def test_without_base_branch_restricts_to_ticket_mentions(tmp_path):
    r = tmp_path / "solo"
    r.mkdir()
    git(r, "init", "-q", "-b", "trunk")  # 'trunk' is not a configured base branch
    for msg in ("unrelated work", "PAY-7: the real change", "more unrelated"):
        (r / f"{msg[:4]}.txt").write_text(msg)
        git(r, "add", ".")
        git(r, "commit", "-qm", msg)
    act = collect_activity(r, Config(), ticket="PAY-7")
    assert act.base_ref is None and len(act.commits) == 3
    act, restricted = restrict_to_ticket(act)
    assert restricted and [c.subject for c in act.commits] == ["PAY-7: the real change"]


def test_branch_creation_date_best_effort(repo):
    info = repo_info(repo)
    assert info.branch_created is None or info.branch_created[:4].isdigit()

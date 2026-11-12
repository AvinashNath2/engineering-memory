from __future__ import annotations

import pytest

from engineering_memory.capture.activity_collector import collect_activity
from engineering_memory.config import Config
from engineering_memory.memory.creator import add_project, create_ticket, init_memory_dir
from engineering_memory.memory.layout import (
    MARKER,
    TICKET_FILES,
    MemoryError_,
    find_ticket_dir,
    normalize_ticket,
    require_memory_dir,
    resolve_project,
)
from engineering_memory.memory.updater import (
    EntryRequest,
    LinkRequest,
    PersonRequest,
    record_activity,
    update_ticket,
)
from engineering_memory.search.retriever import load_ticket, timeline_entries
from engineering_memory.store import ConflictError, atomic_write, parse_frontmatter, read_text

from conftest import git


# ---- init / projects ----------------------------------------------------
def test_init_creates_structure(memdir):
    for d in ("projects", "people", "decisions", "discussions", "daily-logs", "attachments"):
        assert (memdir / d).is_dir()
    for f in ("INDEX.md", "PROJECTS.md", "PEOPLE.md", "CLAUDE.md", "AGENTS.md", MARKER, ".gitignore"):
        assert (memdir / f).exists()
    assert "Engineering Memory protocol" in (memdir / "CLAUDE.md").read_text()


def test_init_is_idempotent_and_keeps_edits(memdir):
    (memdir / "CLAUDE.md").write_text("my custom instructions\n")
    _, created = init_memory_dir(memdir, git=False)
    assert created is False
    assert (memdir / "CLAUDE.md").read_text() == "my custom instructions\n"


def test_init_can_create_git_repo(tmp_path):
    path, _ = init_memory_dir(tmp_path / "withgit", git=True)
    assert (path / ".git").exists()


def test_require_memory_dir(tmp_path):
    with pytest.raises(MemoryError_):
        require_memory_dir(tmp_path)


def test_multiple_projects_and_resolution(memdir):
    add_project(memdir, "payment-service", repo_name="payment-service", ticket_prefixes=["PAY"])
    add_project(memdir, "user-service", remote="https://x/user-service.git", ticket_prefixes=["USR"])
    assert resolve_project(memdir, repo_name="payment-service").slug == "payment-service"
    assert resolve_project(memdir, remote="https://x/user-service.git").slug == "user-service"
    assert resolve_project(memdir, ticket="USR-5").slug == "user-service"
    assert resolve_project(memdir, ticket="ZZZ-1") is None
    assert resolve_project(memdir, explicit="user-service").slug == "user-service"
    with pytest.raises(MemoryError_):
        resolve_project(memdir, explicit="nope")


def test_add_project_twice_merges_without_duplicates(memdir):
    add_project(memdir, "p", repo_name="r1", ticket_prefixes=["A"])
    add_project(memdir, "p", repo_name="r1", ticket_prefixes=["a", "B"])
    add_project(memdir, "p", repo_name="r2")
    meta, _ = parse_frontmatter(read_text(memdir / "projects" / "p" / "PROJECT.md"))
    assert [r["name"] for r in meta["repos"]] == ["r1", "r2"]
    assert meta["ticket_prefixes"] == ["A", "B"]


# ---- ticket creation ----------------------------------------------------
def test_create_ticket_makes_all_files_with_metadata(memdir, project):
    tdir, created = create_ticket(memdir, project, "pay-1234", title="Retry")
    assert created and tdir.name == "PAY-1234"
    assert sorted(p.name for p in tdir.iterdir()) == sorted(TICKET_FILES)
    meta, body = parse_frontmatter(read_text(tdir / "MEMORY.md"))
    assert meta["ticket"] == "PAY-1234" and meta["project"] == project and meta["status"] == "in-progress"
    assert meta["created"] == meta["updated"]
    assert "## Implementation" in body and "## Team" in body


def test_duplicate_ticket_is_not_recreated(memdir, project):
    tdir, _ = create_ticket(memdir, project, "PAY-1")
    update_ticket(memdir, "PAY-1", entries=[EntryRequest("implementation", "kept", "unknown")])
    again, created = create_ticket(memdir, project, "PAY-1")
    assert not created and again == tdir
    assert "kept" in read_text(tdir / "MEMORY.md")


def test_invalid_ticket_and_unknown_project(memdir, project):
    with pytest.raises(MemoryError_):
        normalize_ticket("not a ticket")
    with pytest.raises(MemoryError_):
        create_ticket(memdir, "ghost", "PAY-2")
    with pytest.raises(MemoryError_):
        create_ticket(memdir, project, "PAY-3", ticket_type="epic")


# ---- updates ------------------------------------------------------------
def test_update_adds_entry_and_timeline_and_daily_log(memdir, ticket):
    res = update_ticket(memdir, ticket, entries=[
        EntryRequest("implementation", "Added retry worker", "verified", "MR !1")])
    assert res.changed
    tm = load_ticket(memdir, ticket)
    assert "Added retry worker" in tm.files["MEMORY.md"]
    assert any("Memory updated: implementation" in t for _, t in timeline_entries(tm))
    assert list((memdir / "daily-logs").glob("*.md"))


def test_update_never_removes_history(memdir, ticket):
    update_ticket(memdir, ticket, entries=[EntryRequest("decisions", "Decision A", "unknown")])
    before = read_text(find_ticket_dir(memdir, ticket) / "technical-decisions.md")
    update_ticket(memdir, ticket, entries=[EntryRequest("decisions", "Decision B", "unknown")])
    after = read_text(find_ticket_dir(memdir, ticket) / "technical-decisions.md")
    assert "Decision A" in after and "Decision B" in after
    assert after.index("Decision A") < after.index("Decision B")
    assert before.split("## Decisions")[0] == after.split("## Decisions")[0]


def test_duplicate_and_near_duplicate_prevented(memdir, ticket):
    req = EntryRequest("implementation", "Uses exponential backoff", "unknown")
    assert update_ticket(memdir, ticket, entries=[req]).added
    r2 = update_ticket(memdir, ticket, entries=[EntryRequest("implementation", "uses exponential backoff.", "unknown")])
    assert not r2.added and r2.skipped
    r3 = update_ticket(memdir, ticket, entries=[req])
    assert not r3.added
    assert read_text(find_ticket_dir(memdir, ticket) / "MEMORY.md").count("exponential backoff") == 1


def test_verified_without_source_rejected(memdir, ticket):
    with pytest.raises(MemoryError_):
        update_ticket(memdir, ticket, entries=[EntryRequest("implementation", "x", "verified", None)])


def test_unknown_section_and_missing_ticket(memdir, ticket):
    with pytest.raises(MemoryError_):
        update_ticket(memdir, ticket, entries=[EntryRequest("gossip", "x", "unknown")])
    with pytest.raises(MemoryError_):
        update_ticket(memdir, "PAY-9999", entries=[EntryRequest("testing", "x", "unknown")])


def test_secrets_are_redacted_on_write(memdir, ticket):
    res = update_ticket(memdir, ticket, entries=[EntryRequest(
        "implementation", "Config uses api_key = sk_live_ABCDEFGH123456 and AKIAIOSFODNN7EXAMPLE", "unknown")])
    text = read_text(find_ticket_dir(memdir, ticket) / "MEMORY.md")
    assert res.redactions >= 2
    assert "sk_live_ABCDEFGH123456" not in text and "AKIAIOSFODNN7EXAMPLE" not in text
    assert "[REDACTED" in text


def test_people_roles_never_invented(memdir, ticket):
    update_ticket(memdir, ticket, people=[PersonRequest("Priya Sharma")])
    update_ticket(memdir, ticket, people=[PersonRequest("Priya Sharma", "QA", "Ticket comment")])
    meta = load_ticket(memdir, ticket).meta
    assert {(m["name"], m["role"], m["confidence"]) for m in meta["team"]} == {
        ("Priya Sharma", "unknown", "unconfirmed"), ("Priya Sharma", "QA", "verified")}
    again = update_ticket(memdir, ticket, people=[PersonRequest("priya sharma", "qa", "Ticket comment")])
    assert not again.added  # case-insensitive duplicate


def test_links_are_sanitised_deduped_and_validated(memdir, ticket):
    res = update_ticket(memdir, ticket, links=[
        LinkRequest("https://user:pw@acme.atlassian.net/wiki/x", "Design", "docs"),
        LinkRequest("javascript:alert(1)", "bad", "docs"),
    ])
    meta = load_ticket(memdir, ticket).meta
    assert [l["url"] for l in meta["links"]] == ["https://acme.atlassian.net/wiki/x"]
    assert any("ignored link" in w for w in res.warnings)
    assert not update_ticket(memdir, ticket, links=[LinkRequest("https://acme.atlassian.net/wiki/x")]).added


def test_set_status_title_type_validated_and_logged(memdir, ticket):
    update_ticket(memdir, ticket, set_meta={"status": "done", "title": "Retry with backoff"})
    tm = load_ticket(memdir, ticket)
    assert tm.meta["status"] == "done" and tm.meta["title"] == "Retry with backoff"
    assert f"# {ticket} — Retry with backoff" in tm.files["MEMORY.md"]
    assert any("Status changed: in-progress → done" in t for _, t in timeline_entries(tm))
    with pytest.raises(MemoryError_):
        update_ticket(memdir, ticket, set_meta={"status": "finished-ish"})
    with pytest.raises(MemoryError_):
        update_ticket(memdir, ticket, set_meta={"created": "1999-01-01"})


def test_updated_timestamp_moves_forward_created_does_not(memdir, ticket):
    before = load_ticket(memdir, ticket).meta
    update_ticket(memdir, ticket, entries=[EntryRequest("testing", "ran suite", "unknown")])
    after = load_ticket(memdir, ticket).meta
    assert after["created"] == before["created"] and after["updated"] >= before["updated"]


def test_conflict_protection_when_file_changes_mid_update(memdir, ticket, monkeypatch):
    from engineering_memory.memory import updater

    tdir = find_ticket_dir(memdir, ticket)
    original_save = updater.TicketSession.save

    def sneaky_save(self):
        atomic_write(tdir / "MEMORY.md", read_text(tdir / "MEMORY.md") + "\nhand edit\n")
        return original_save(self)

    monkeypatch.setattr(updater.TicketSession, "save", sneaky_save)
    with pytest.raises(ConflictError):
        update_ticket(memdir, ticket, entries=[EntryRequest("testing", "x", "unknown")])
    assert "hand edit" in read_text(tdir / "MEMORY.md")  # the concurrent edit survived


# ---- git capture --------------------------------------------------------
def test_record_activity_populates_git_sections(memdir, ticket, repo):
    act = collect_activity(repo, Config())
    res = record_activity(memdir, ticket, act, pr_links=["https://gitlab.acme.io/pay/-/merge_requests/482"])
    tm = load_ticket(memdir, ticket)
    assert res.changed
    assert tm.meta["branches"] == ["feature/pay-1234-payment-retry"]
    assert len(tm.meta["commits"]) == 2
    assert "`src/retry.py`" in tm.files["MEMORY.md"]
    assert ".env" not in tm.files["MEMORY.md"] and "deploy.pem" not in tm.files["MEMORY.md"]
    assert {"ref": "#42"} in tm.meta["pull_requests"]
    assert any("merge_requests/482" in str(p) for p in tm.meta["pull_requests"])
    assert len(timeline_entries(tm)) >= 2
    assert any("sensitive" in w for w in res.warnings)


def test_record_activity_is_idempotent_and_incremental(memdir, ticket, repo):
    record_activity(memdir, ticket, collect_activity(repo, Config()))
    snapshot = read_text(find_ticket_dir(memdir, ticket) / "MEMORY.md")
    again = record_activity(memdir, ticket, collect_activity(repo, Config()))
    assert not again.changed
    assert read_text(find_ticket_dir(memdir, ticket) / "MEMORY.md") == snapshot
    (repo / "src" / "new.py").write_text("x = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "PAY-1234: third commit")
    third = record_activity(memdir, ticket, collect_activity(repo, Config()))
    assert third.changed
    assert len(load_ticket(memdir, ticket).meta["commits"]) == 3
    assert "cap attempts" in read_text(find_ticket_dir(memdir, ticket) / "MEMORY.md")  # old history kept

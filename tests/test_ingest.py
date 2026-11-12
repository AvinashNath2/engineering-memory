from __future__ import annotations

import pytest

from engineering_memory.memory.ingest import (
    SourceFile,
    classify_url,
    extract_links,
    extract_people,
    extract_title,
    ingest_sources,
    looks_like_diff,
    split_diff,
)
from engineering_memory.memory.layout import MemoryError_, find_ticket_dir
from engineering_memory.search.retriever import load_ticket
from engineering_memory.store import read_text

TICKET_TXT = """# PAY-1234: Add payment retry with backoff
Assignee: Dev One
Reporter: **Meera Nair**
Reviewers: Arjun Rao, Priya Sharma (backend)
QA: unassigned
Confluence: https://acme.atlassian.net/wiki/spaces/PAY/pages/123/Retry+Design.
Comment (Meera): token = ghp_""" + "a" * 36 + """
"""

MR_TXT = """MR !482 for PAY-1234
Author: Dev One
Approved by: Arjun Rao
https://gitlab.acme.io/pay/svc/-/merge_requests/482
diff --git a/src/retry.py b/src/retry.py
--- a/src/retry.py
+++ b/src/retry.py
@@ -1 +1,3 @@
+SECRET_CODE_LINE = 1
+def retry(): pass
-old_line
"""


def test_extract_people_only_from_explicit_fields():
    people = extract_people(TICKET_TXT, "ticket")
    assert ("Dev One", "assignee") in people
    assert ("Meera Nair", "reporter") in people
    assert ("Arjun Rao", "reviewer") in people and ("Priya Sharma", "reviewer") in people
    assert not any(r == "QA" for _, r in people)  # "unassigned" is not a person
    assert extract_people("Priya did the testing and Arjun reviewed it", "ticket") == []  # prose is never guessed


def test_author_in_mr_gets_mr_role():
    assert ("Dev One", "MR author") in extract_people("Author: Dev One", "mr")
    assert ("Dev One", "author") in extract_people("Author: Dev One", "ticket")


def test_link_classification():
    assert classify_url("https://gitlab.com/g/p/-/merge_requests/9") == "mr"
    assert classify_url("https://github.com/o/r/pull/12") == "mr"
    assert classify_url("https://acme.atlassian.net/browse/PAY-1") == "ticket"
    assert classify_url("https://acme.atlassian.net/wiki/spaces/X/pages/1") == "docs"
    assert classify_url("https://example.com/random") is None
    found = extract_links("see https://acme.atlassian.net/wiki/spaces/X/pages/1, and https://example.com/x.")
    assert found == [("https://acme.atlassian.net/wiki/spaces/X/pages/1", "docs")]


def test_title_extraction():
    assert extract_title("# PAY-1: Add retry\nbody", "PAY-1") == "Add retry"
    assert extract_title("Summary: Fix login\n", "PAY-1") == "Fix login"
    assert extract_title("no heading", "PAY-1") is None


def test_diff_is_reduced_to_paths_and_counts():
    assert looks_like_diff(MR_TXT)
    prose, stats = split_diff(MR_TXT)
    assert stats == [("src/retry.py", 2, 1)]
    assert "SECRET_CODE_LINE" not in prose and "Approved by" in prose


def test_ingest_end_to_end(memdir, ticket, tmp_path):
    t = tmp_path / "ticket.md"; t.write_text(TICKET_TXT)
    m = tmp_path / "mr.diff"; m.write_text(MR_TXT)
    res = ingest_sources(memdir, ticket, [SourceFile("ticket", t), SourceFile("mr", m)])
    assert len(res.saved) == 2 and res.update.changed
    tm = load_ticket(memdir, ticket)
    assert tm.meta["title"] == "Payment retry"  # existing title is not overwritten
    names = {(p["name"], p["role"]) for p in tm.meta["team"]}
    assert {("Dev One", "assignee"), ("Arjun Rao", "approver"), ("Dev One", "MR author")} <= names
    assert all(p["confidence"] == "verified" and p["source"] for p in tm.meta["team"])
    assert any("merge_requests/482" in l["url"] for l in tm.meta["pull_requests"])
    assert any("Retry+Design" in l["url"] for l in tm.meta["links"])
    # sanitised copies exist, secrets and code are gone
    stored = "\n".join(read_text(p) for p in (memdir / p for p in res.saved))
    assert "ghp_" not in stored and "[REDACTED:github-token]" in stored
    assert "SECRET_CODE_LINE" not in stored and "`src/retry.py` (+2 / -1)" in stored
    assert "Sources" in tm.files["MEMORY.md"] and "attachments/PAY-1234/" in tm.files["MEMORY.md"]


def test_ingest_sets_title_when_unknown(memdir, project, tmp_path):
    from engineering_memory.memory.creator import create_ticket

    create_ticket(memdir, project, "PAY-77")
    f = tmp_path / "t.md"; f.write_text("# PAY-77: Refund API\nAssignee: A B\n")
    res = ingest_sources(memdir, "PAY-77", [SourceFile("ticket", f)])
    assert res.title_found == "Refund API"
    assert load_ticket(memdir, "PAY-77").meta["title"] == "Refund API"


def test_ingest_is_idempotent(memdir, ticket, tmp_path):
    t = tmp_path / "ticket.md"; t.write_text(TICKET_TXT)
    ingest_sources(memdir, ticket, [SourceFile("ticket", t)])
    snapshot = read_text(find_ticket_dir(memdir, ticket) / "MEMORY.md")
    n_files = len(list((memdir / "attachments" / ticket).iterdir()))
    again = ingest_sources(memdir, ticket, [SourceFile("ticket", t)])
    assert not again.update.changed
    assert read_text(find_ticket_dir(memdir, ticket) / "MEMORY.md") == snapshot
    assert len(list((memdir / "attachments" / ticket).iterdir())) == n_files


def test_changed_source_keeps_old_version(memdir, ticket, tmp_path):
    t = tmp_path / "page.md"
    t.write_text("PAY-1234 design v1")
    ingest_sources(memdir, ticket, [SourceFile("docs", t)])
    t.write_text("PAY-1234 design v2")
    ingest_sources(memdir, ticket, [SourceFile("docs", t)])
    texts = sorted(read_text(p) for p in (memdir / "attachments" / ticket).iterdir())
    assert any("v1" in x for x in texts) and any("v2" in x for x in texts)


def test_sensitive_and_binary_files_skipped(memdir, ticket, tmp_path):
    env = tmp_path / ".env"; env.write_text("PASSWORD=abc")
    binf = tmp_path / "shot.md"; binf.write_bytes(b"\x89PNG\x00\x00data")
    ok = tmp_path / "ok.md"; ok.write_text("PAY-1234 fine")
    res = ingest_sources(memdir, ticket, [SourceFile("note", env), SourceFile("note", binf), SourceFile("note", ok)])
    assert len(res.saved) == 1
    assert any("sensitive" in w for w in res.warnings) and any("binary" in w for w in res.warnings)


def test_wrong_ticket_warning(memdir, ticket, tmp_path):
    f = tmp_path / "other.md"; f.write_text("This is about ABC-1 only")
    res = ingest_sources(memdir, ticket, [SourceFile("note", f)])
    assert any("never mentions PAY-1234" in w for w in res.warnings)


def test_ingest_errors(memdir, ticket, tmp_path):
    with pytest.raises(MemoryError_):
        ingest_sources(memdir, ticket, [])
    with pytest.raises(MemoryError_):
        ingest_sources(memdir, ticket, [SourceFile("ticket", tmp_path / "missing.md")])
    f = tmp_path / "a.md"; f.write_text("x")
    with pytest.raises(MemoryError_):
        ingest_sources(memdir, "PAY-404", [SourceFile("note", f)])

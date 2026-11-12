from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from engineering_memory.memory.creator import add_project, create_ticket
from engineering_memory.memory.indexer import build_fts, fts_available, fts_is_fresh, rebuild_index
from engineering_memory.memory.layout import find_ticket_dir
from engineering_memory.memory.updater import EntryRequest, PersonRequest, update_ticket
from engineering_memory.memory.validator import validate
from engineering_memory.search.retriever import find_people
from engineering_memory.search.searcher import parse_query, search
from engineering_memory.store import atomic_write, dump_frontmatter, parse_frontmatter, read_text


@pytest.fixture
def populated(memdir):
    add_project(memdir, "payment-service", ticket_prefixes=["PAY"])
    add_project(memdir, "user-service", ticket_prefixes=["USR"])
    create_ticket(memdir, "payment-service", "PAY-1234", title="Payment retry implementation")
    create_ticket(memdir, "user-service", "USR-9", title="Login throttling")
    update_ticket(memdir, "PAY-1234",
                  entries=[EntryRequest("implementation", "Exponential backoff for failed charges", "verified", "MR !482"),
                           EntryRequest("testing", "Gateway timeout simulation passed", "verified", "QA notes")],
                  people=[PersonRequest("Priya Sharma", "QA", "QA notes")])
    update_ticket(memdir, "USR-9", entries=[EntryRequest("implementation", "Rate limit per IP", "unknown")],
                  people=[PersonRequest("Arjun Rao", "backend", "ticket")])
    rebuild_index(memdir)
    return memdir


# ---- search -------------------------------------------------------------
def test_parse_query():
    terms = parse_query('who tested "payment retry" for PAY-1234?')
    assert "payment retry" in terms and "pay-1234" in terms and "who" not in terms and "for" not in terms


def test_search_by_ticket_keyword_and_phrase(populated):
    hits, strategy = search(populated, "PAY-1234", use_fts=False)
    assert strategy == "text"
    assert hits[0].path.endswith("PAY-1234/MEMORY.md") or "PAY-1234" in hits[0].path
    assert all("USR-9" not in h.path for h in hits)
    assert search(populated, "exponential backoff", use_fts=False)[0][0].ticket == "PAY-1234"
    assert search(populated, '"Rate limit per IP"', use_fts=False)[0][0].ticket == "USR-9"


def test_search_ranking_prefers_more_terms(populated):
    hits, _ = search(populated, "backoff gateway", use_fts=False)
    # the two terms live in different files of the same ticket: that ticket must outrank everything else
    assert hits[0].ticket == "PAY-1234" and hits[0].ticket_matched == 2
    assert all(h.ticket == "PAY-1234" for h in hits if h.ticket_matched == 2)


def test_search_filters(populated):
    assert {h.project for h in search(populated, "implementation", project="user-service", use_fts=False)[0]} == {"user-service"}
    assert {h.ticket for h in search(populated, "tested", person="Priya", use_fts=False)[0] if h.ticket} == {"PAY-1234"}
    future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    assert search(populated, "backoff", since=future, use_fts=False)[0] == []
    assert search(populated, "backoff", since="2000-01-01", use_fts=False)[0]


def test_search_no_results_and_empty_query(populated):
    assert search(populated, "kubernetes operator", use_fts=False)[0] == []
    assert search(populated, "the of and", use_fts=False)[0] == []


def test_search_snippets_have_line_numbers_and_no_html_ids(populated):
    hit = search(populated, "backoff", use_fts=False)[0][0]
    assert hit.snippets and all(isinstance(n, int) and "<!--" not in s for n, s in hit.snippets)


def test_agent_instruction_files_are_not_search_results(populated):
    assert not any(h.path in ("CLAUDE.md", "AGENTS.md") for h in search(populated, "memory protocol", use_fts=False)[0])


@pytest.mark.skipif(not fts_available(), reason="SQLite built without FTS5")
def test_fts_index_is_rebuildable_and_used_when_fresh(populated):
    assert not fts_is_fresh(populated)
    n = build_fts(populated)
    assert n > 5 and fts_is_fresh(populated)
    hits, strategy = search(populated, "backoff")
    assert strategy == "fts" and hits[0].ticket == "PAY-1234"
    # a memory change makes the index stale -> automatic fallback to text scan, never wrong answers
    update_ticket(populated, "PAY-1234", entries=[EntryRequest("implementation", "Added jitter to delays", "unknown")])
    assert not fts_is_fresh(populated)
    hits, strategy = search(populated, "jitter")
    assert strategy == "text" and hits
    # deleting it loses nothing
    (populated / ".engineering-memory.sqlite").unlink()
    assert search(populated, "jitter")[0]


def test_fts_and_text_agree_on_top_result(populated):
    if not fts_available():
        pytest.skip("no FTS5")
    build_fts(populated)
    a = search(populated, "gateway timeout", use_fts=True)[0][0].path
    b = search(populated, "gateway timeout", use_fts=False)[0][0].path
    assert a == b


# ---- people -------------------------------------------------------------
def test_people_search(populated):
    assert [p["name"] for p in find_people(populated)] == ["Arjun Rao", "Priya Sharma"]
    assert [p["name"] for p in find_people(populated, "qa")] == ["Priya Sharma"]
    assert [p["name"] for p in find_people(populated, "USR-9")] == ["Arjun Rao"]
    assert find_people(populated, "nobody") == []


# ---- index --------------------------------------------------------------
def test_index_contents_and_determinism(populated):
    idx = read_text(populated / "INDEX.md")
    assert "PAY-1234" in idx and "USR-9" in idx and "## payment-service" in idx
    assert "(projects/payment-service/tickets/PAY-1234/MEMORY.md)" in idx
    assert rebuild_index(populated) == []  # nothing changed -> no noisy diffs
    assert "Priya Sharma" in read_text(populated / "PEOPLE.md")
    assert "payment-service" in read_text(populated / "PROJECTS.md")


def test_people_files_preserve_manual_notes(populated):
    f = populated / "people" / "priya-sharma.md"
    atomic_write(f, read_text(f) + "\nPrefers async reviews.\n")
    update_ticket(populated, "PAY-1234", people=[PersonRequest("Priya Sharma", "reviewer", "MR")])
    rebuild_index(populated)
    text = read_text(f)
    assert "Prefers async reviews." in text and "reviewer" in text


def test_index_on_empty_memory(memdir):
    assert "0** project(s)" in read_text(memdir / "INDEX.md") or "**0**" in read_text(memdir / "INDEX.md")


# ---- validator ----------------------------------------------------------
def errors(memdir):
    return [str(i) for i in validate(memdir) if i.level == "error"]


def test_valid_memory_passes(populated):
    assert errors(populated) == []


def test_missing_required_file_detected(populated):
    (find_ticket_dir(populated, "PAY-1234") / "testing.md").unlink()
    assert any("testing.md" in e and "missing" in e for e in errors(populated))


def test_not_a_memory_dir(tmp_path):
    assert validate(tmp_path)[0].level == "error"


def test_frontmatter_problems_detected(populated):
    mem = find_ticket_dir(populated, "PAY-1234") / "MEMORY.md"
    meta, body = parse_frontmatter(read_text(mem))
    meta["status"] = "weird"
    meta["ticket"] = "PAY-9999"
    del meta["updated"]
    atomic_write(mem, dump_frontmatter(meta, body))
    errs = " | ".join(errors(populated))
    assert "invalid status" in errs and "does not match folder" in errs and "missing 'updated'" in errs


def test_broken_yaml_detected(populated):
    mem = find_ticket_dir(populated, "PAY-1234") / "MEMORY.md"
    atomic_write(mem, "---\nkey: [oops\n---\nbody")
    assert any("invalid YAML" in e for e in errors(populated))


def test_verified_entry_without_source_detected(populated):
    mem = find_ticket_dir(populated, "PAY-1234") / "MEMORY.md"
    atomic_write(mem, read_text(mem).replace("## Open items\n", "## Open items\n\n- **2026-11-07** [verified] hand-written claim <!-- em:handmade01 -->\n", 1))
    assert any("[verified] entry has no source" in e for e in errors(populated))


def test_broken_link_detected(populated):
    mem = find_ticket_dir(populated, "PAY-1234") / "MEMORY.md"
    atomic_write(mem, read_text(mem) + "\nSee [gone](../../../../attachments/nope.md)\n")
    assert any("broken link" in e for e in errors(populated))


def test_secret_in_memory_detected(populated):
    mem = find_ticket_dir(populated, "PAY-1234") / "testing.md"
    atomic_write(mem, read_text(mem) + "\nkey AKIAIOSFODNN7EXAMPLE\n")
    assert any("possible secret" in e for e in errors(populated))


def test_timeline_future_and_order_detected(populated):
    tl = find_ticket_dir(populated, "PAY-1234") / "timeline.md"
    future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    atomic_write(tl, read_text(tl) + f"\n- **{future}** [unknown] time traveller <!-- em:tt0001 -->\n- **2026-01-01T00:00:00Z** [unknown] late <!-- em:tt0002 -->\n")
    errs = " | ".join(errors(populated))
    assert "in the future" in errs and "not chronological" in errs


def test_created_after_updated_detected(populated):
    mem = find_ticket_dir(populated, "PAY-1234") / "MEMORY.md"
    meta, body = parse_frontmatter(read_text(mem))
    meta["created"] = "2099-01-01T00:00:00Z"
    atomic_write(mem, dump_frontmatter(meta, body))
    assert any("created is later than updated" in e for e in errors(populated))


def test_duplicate_entry_ids_and_duplicate_tickets_detected(populated):
    mem = find_ticket_dir(populated, "PAY-1234") / "MEMORY.md"
    text = read_text(mem)
    line = next(l for l in text.splitlines() if "Exponential backoff" in l)
    atomic_write(mem, text.replace(line, line + "\n" + line))
    # same ticket id under a second project
    add_project(populated, "other", ticket_prefixes=["OTH"])
    (populated / "projects" / "other" / "tickets" / "PAY-1234").mkdir(parents=True)
    errs = " | ".join(errors(populated))
    assert "duplicate entry id" in errs and "duplicate memory for PAY-1234" in errs


def test_unverified_claims_are_allowed(populated):
    update_ticket(populated, "PAY-1234", entries=[EntryRequest("open-items", "Reviewer unknown", "unknown")])
    assert errors(populated) == []

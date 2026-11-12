from __future__ import annotations

import pytest

from engineering_memory.memory.entries import (
    ENTRY_START,
    PLACEHOLDER,
    Entry,
    EntryError,
    append_entry,
    entry_id,
    find_duplicate,
    render,
    section_entries,
)

BODY = f"# T\n\n## Implementation\n\n{PLACEHOLDER}\n\n## Open items\n\n{PLACEHOLDER}\n"


def test_verified_requires_source():
    with pytest.raises(EntryError):
        render("implementation", Entry("x", "verified", None, "2026-11-07"))
    render("implementation", Entry("x", "verified", "MR !1", "2026-11-07"))


def test_bad_confidence_and_empty_text():
    with pytest.raises(EntryError):
        render("implementation", Entry("x", "certain", "s", "d"))
    with pytest.raises(EntryError):
        render("implementation", Entry("   ", "unknown", None, "d"))


def test_render_format_and_id_is_stable():
    e = Entry("Uses backoff", "verified", "MR !1", "2026-11-07")
    line = render("implementation", e)
    assert line.startswith("- **2026-11-07** [verified] Uses backoff _(source: MR !1)_ <!-- em:")
    assert entry_id("implementation", e) in line
    assert ENTRY_START.match(line)
    assert entry_id("implementation", e) != entry_id("decisions", e)


def test_append_replaces_placeholder_and_keeps_other_sections():
    out = append_entry(BODY, "Implementation", "- **2026-11-07** [unknown] a")
    assert PLACEHOLDER in out  # the Open items placeholder remains
    assert out.count(PLACEHOLDER) == 1
    assert section_entries(out, "Implementation") == ["- **2026-11-07** [unknown] a"]
    assert section_entries(out, "Open items") == []


def test_append_preserves_existing_entries_in_order():
    out = BODY
    for i in range(3):
        out = append_entry(out, "Implementation", f"- **2026-11-0{i + 1}** [unknown] entry {i}")
    assert [b.split("] ")[1] for b in section_entries(out, "Implementation")] == ["entry 0", "entry 1", "entry 2"]


def test_missing_section_is_created():
    out = append_entry("# T\n", "Brand new", "- **d** [unknown] x")
    assert "## Brand new" in out and "x" in out


def test_multiline_entry_roundtrip():
    e = Entry("first line\nsecond line\nthird", "unknown", "doc", "2026-11-07")
    line = render("decisions", e)
    out = append_entry(BODY, "Implementation", line)
    blocks = section_entries(out, "Implementation")
    assert len(blocks) == 1 and "second line" in blocks[0] and "<!-- em:" in blocks[0]


def test_duplicate_detection_exact_and_near():
    e = Entry("Uses exponential backoff for retries", "verified", "MR", "2026-11-07")
    body = append_entry(BODY, "Implementation", render("implementation", e))
    assert find_duplicate(body, "Implementation", "implementation", e)
    near = Entry("Uses exponential backoff for retries!", "unknown", None, "2026-11-08")
    assert find_duplicate(body, "Implementation", "implementation", near)
    different = Entry("Added a circuit breaker", "unknown", None, "2026-11-08")
    assert find_duplicate(body, "Implementation", "implementation", different) is None


def test_explicit_key_identity_not_fuzzy():
    a = Entry("`abc12345` fix", "verified", "c", "d", key="commit:abc")
    b = Entry("`abc12346` fix", "verified", "c", "d", key="commit:abd")
    body = append_entry(BODY, "Implementation", render("implementation", a))
    assert find_duplicate(body, "Implementation", "implementation", b) is None


def test_timeline_insertion_is_sorted():
    body = "# T\n\n## Events\n\n" + PLACEHOLDER + "\n"
    for ts in ("2026-11-03T10:00:00Z", "2026-11-01T10:00:00Z", "2026-11-02T10:00:00Z"):
        body = append_entry(body, "Events", render("timeline", Entry(f"e {ts[:10]}", "unknown", None, ts)), sort_timeline=True)
    whens = [ENTRY_START.match(b).group(1) for b in section_entries(body, "Events")]
    assert whens == sorted(whens) and len(whens) == 3

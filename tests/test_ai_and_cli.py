from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

from engineering_memory.ai.prompts import build_brief, build_extraction_prompt
from engineering_memory.ai.provider import (
    CommandProvider,
    NullProvider,
    ProviderError,
    extract_json,
    get_provider,
    to_requests,
)
from engineering_memory.cli import app
from engineering_memory.config import Config, load_config, save_config, set_value, ConfigError
from engineering_memory.memory.layout import find_ticket_dir
from engineering_memory.store import read_text

from conftest import git

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---- AI adapter ---------------------------------------------------------
def test_null_provider_explains_how_to_configure():
    with pytest.raises(ProviderError, match="No AI provider configured"):
        NullProvider().complete("x")
    assert isinstance(get_provider(Config()), NullProvider)


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure!\n```json\n{"a": 2}\n```\nDone') == {"a": 2}
    assert extract_json('prefix {"a": 3} suffix') == {"a": 3}
    for bad in ("no json here", '{"a": ', "[1, 2]"):
        with pytest.raises(ProviderError):
            extract_json(bad)


def test_to_requests_downgrades_unverifiable_claims_and_drops_bad_sections():
    entries, people, links, meta, warnings = to_requests({
        "entries": [
            {"section": "implementation", "text": "claimed", "confidence": "verified"},
            {"section": "implementation", "text": "sourced", "confidence": "verified", "source": "MR"},
            {"section": "bogus", "text": "x"},
            {"section": "sources", "text": "forged source list"},
            {"section": "testing", "text": "weird conf", "confidence": "certain"},
        ],
        "people": [{"name": "Priya", "role": "QA"}, {"role": "no name"}],
        "links": [{"url": "https://a.b/c"}, {"label": "no url"}],
        "set": {"status": "done", "created": "1999"},
    })
    by_text = {e.text: e for e in entries}
    assert by_text["claimed"].confidence == "unconfirmed"
    assert by_text["sourced"].confidence == "verified"
    assert by_text["weird conf"].confidence == "unconfirmed"
    assert "x" not in by_text and "forged source list" not in by_text
    assert [p.name for p in people] == ["Priya"] and people[0].source is None
    assert len(links) == 1 and meta == {"status": "done"} and warnings


def test_command_provider(tmp_path):
    script = tmp_path / "ai.py"
    script.write_text("import sys; print('echo:' + sys.stdin.read().strip())")
    assert CommandProvider(f"{sys.executable} {script}").complete("hello").strip() == "echo:hello"
    with pytest.raises(ProviderError, match="not found"):
        CommandProvider("definitely-not-a-command-xyz").complete("x")
    fail = tmp_path / "fail.py"
    fail.write_text("import sys; sys.stderr.write('boom'); sys.exit(3)")
    with pytest.raises(ProviderError, match="boom"):
        CommandProvider(f"{sys.executable} {fail}").complete("x")
    slow = tmp_path / "slow.py"
    slow.write_text("import time; time.sleep(5)")
    with pytest.raises(ProviderError, match="timed out"):
        CommandProvider(f"{sys.executable} {slow}", timeout=1).complete("x")
    with pytest.raises(ProviderError):
        CommandProvider("").complete("x")


def test_prompts_contain_rules_sources_and_schema(memdir, ticket, tmp_path):
    from engineering_memory.memory.ingest import SourceFile, ingest_sources

    f = tmp_path / "t.md"; f.write_text("PAY-1234 ticket text with UNIQUE_MARKER")
    res = ingest_sources(memdir, ticket, [SourceFile("ticket", f)])
    prompt, _ = build_extraction_prompt(memdir, ticket)
    assert "UNIQUE_MARKER" in prompt and "Never invent" in prompt and '"entries"' in prompt and "CURRENT MEMORY" in prompt
    brief = build_brief(memdir, ticket, res.saved)
    assert "memory update PAY-1234" in brief and "attachments/PAY-1234" in brief


# ---- config -------------------------------------------------------------
def test_config_roundtrip_and_set_value(tmp_path):
    cfg = Config()
    set_value(cfg, "ai.provider", "command")
    set_value(cfg, "ai.command", "claude -p")
    set_value(cfg, "max_commits", "7")
    set_value(cfg, "search.use_fts", "false")
    set_value(cfg, "extra_sensitive_patterns", "*.cfg, *.conf")
    save_config(cfg, tmp_path / "c.yaml")
    loaded = load_config(tmp_path / "c.yaml")
    assert (loaded.ai.provider, loaded.ai.command, loaded.max_commits, loaded.search.use_fts) == ("command", "claude -p", 7, False)
    assert loaded.extra_sensitive_patterns == ["*.cfg", "*.conf"]
    for bad in (("nope", "x"), ("ai.nope", "x"), ("max_commits", "abc")):
        with pytest.raises(ConfigError):
            set_value(cfg, *bad)


def test_missing_and_invalid_config(tmp_path):
    assert load_config(tmp_path / "absent.yaml").max_commits == 50
    bad = tmp_path / "bad.yaml"; bad.write_text("- just\n- a list\n")
    with pytest.raises(ConfigError):
        load_config(bad)


# ---- CLI: help ----------------------------------------------------------
COMMANDS = ["init", "config", "start", "capture", "ingest", "update", "search", "show", "timeline", "people",
            "index", "validate", "status", "export", "commit", "install-instructions"]


@pytest.mark.parametrize("cmd", COMMANDS + ["project add", "project list"])
def test_every_command_has_help(runner, cmd):
    result = runner.invoke(app, [*cmd.split(), "--help"])
    assert result.exit_code == 0 and "Usage" in result.output


def test_version(runner):
    assert "engineering-memory" in runner.invoke(app, ["--version"]).output


# ---- CLI: full developer workflow ---------------------------------------
def test_full_workflow(runner, repo, tmp_path, monkeypatch):
    monkeypatch.chdir(repo)
    mem = tmp_path / "mem"
    inv = lambda *a: runner.invoke(app, list(a))  # noqa: E731

    # not initialised yet -> clear error
    r = inv("status")
    assert "NOT initialised" in r.output

    assert inv("init", str(mem)).exit_code == 0
    assert inv("init", str(mem)).exit_code == 0  # idempotent
    assert load_config().memory_dir == str(mem.resolve())

    # start before the project is registered explains what to do
    r = inv("start")
    assert r.exit_code == 1 and "memory project add" in r.output

    assert inv("project", "add", "payment-service", "--here", "--prefix", "PAY").exit_code == 0
    assert "payment-service" in inv("project", "list").output

    r = inv("start")  # branch is feature/pay-1234-payment-retry
    assert r.exit_code == 0, r.output
    assert "Created memory for PAY-1234" in r.output and "commit" in r.output
    tdir = find_ticket_dir(mem, "PAY-1234")
    assert tdir and "src/retry.py" in read_text(tdir / "MEMORY.md")

    # starting again resumes instead of duplicating
    r = inv("start")
    assert "Resuming existing memory" in r.output
    assert len(list((mem / "projects" / "payment-service" / "tickets").glob("PAY-*"))) == 1

    # sources -> ingest
    (tmp_path / "ticket.md").write_text("# PAY-1234: Payment retry\nAssignee: Dev One\npassword = 'hunter2hunter2'\n")
    (tmp_path / "mr.md").write_text("MR for PAY-1234\nReviewers: Arjun Rao\nhttps://gitlab.acme.io/p/-/merge_requests/9\n")
    r = inv("ingest", "PAY-1234", "--ticket", str(tmp_path / "ticket.md"), "--mr", str(tmp_path / "mr.md"))
    assert r.exit_code == 0, r.output
    assert "Arjun Rao" in r.output and "extraction brief" in r.output
    assert "hunter2" not in "".join(read_text(p) for p in mem.rglob("*.md"))

    # agent-style updates
    r = inv("update", "PAY-1234", "-s", "decisions", "-c", "verified", "--source", "Confluence design page",
            "--text", "Chose exponential backoff to avoid retry storms")
    assert r.exit_code == 0 and "+ decisions" in r.output
    r = inv("update", "PAY-1234", "--person", "Priya Sharma", "--role", "QA", "--source", "Ticket comment")
    assert r.exit_code == 0
    r = inv("update", "PAY-1234", "-s", "testing", "-c", "verified", "--text", "x")  # verified without source
    assert r.exit_code == 1 and "requires a source" in r.output
    r = inv("update", "PAY-1234")
    assert r.exit_code == 1 and "Nothing to update" in r.output
    assert inv("update", "PAY-1234", "--set", "status=done").exit_code == 0

    # retrieval
    r = inv("search", "who tested PAY-1234", "--person", "Priya")
    assert r.exit_code == 0 and "PAY-1234" in r.output
    r = inv("search", "backoff", "--json")
    assert json.loads(r.output)[0]["path"].startswith("projects/")
    assert "No matches" in inv("search", "kubernetes").output
    assert "Priya Sharma" in inv("people", "QA").output
    assert "Status changed" in inv("timeline", "PAY-1234").output
    shown = inv("show", "PAY-1234").output
    assert "Chose exponential backoff" in shown and "<!--" not in shown
    assert "---\nschema_version" in inv("show", "PAY-1234", "--raw").output

    assert inv("validate").exit_code == 0
    r = inv("index", "--fts")
    assert r.exit_code == 0 and "FTS index built" in r.output

    r = inv("status")
    assert "PAY-1234" in r.output and "uncaptured commits: 0" in r.output
    (repo / "extra.py").write_text("y=2\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "PAY-1234: another change")
    assert "uncaptured commits: 1" in inv("status").output
    r = inv("capture", "--dry-run")
    assert "another change" in r.output
    assert "uncaptured commits: 1" in inv("status").output  # dry run wrote nothing
    assert inv("capture", "--pr", "https://gitlab.acme.io/p/-/merge_requests/9").exit_code == 0
    assert "uncaptured commits: 0" in inv("status").output

    # export & commit
    out = tmp_path / "backup.zip"
    assert inv("export", "-o", str(out)).exit_code == 0
    names = zipfile.ZipFile(out).namelist()
    assert any(n.endswith("PAY-1234/MEMORY.md") for n in names)
    assert not any(".sqlite" in n or ".em.lock" in n or "/.git/" in n for n in names)
    assert inv("commit", "-m", "PAY-1234: done").exit_code == 0
    assert "Nothing to commit" in inv("commit").output


def test_portable_after_copy(runner, repo, tmp_path, monkeypatch):
    """Acceptance: copy the memory dir elsewhere and keep using it."""
    import shutil

    monkeypatch.chdir(repo)
    mem = tmp_path / "mem"
    runner.invoke(app, ["init", str(mem)])
    runner.invoke(app, ["project", "add", "payment-service", "--here", "--prefix", "PAY"])
    runner.invoke(app, ["start"])
    moved = tmp_path / "other-machine" / "memory-copy"
    shutil.copytree(mem, moved)
    shutil.rmtree(mem)
    r = runner.invoke(app, ["-d", str(moved), "show", "PAY-1234"])
    assert r.exit_code == 0 and "src/retry.py" in r.output
    assert runner.invoke(app, ["-d", str(moved), "validate"]).exit_code == 0
    assert runner.invoke(app, ["-d", str(moved), "update", "PAY-1234", "-s", "open-items", "-c", "unknown", "--text", "still works"]).exit_code == 0


def test_start_without_ticket_in_branch(runner, repo, tmp_path, monkeypatch):
    monkeypatch.chdir(repo)
    git(repo, "checkout", "-q", "main")
    runner.invoke(app, ["init", str(tmp_path / "mem")])
    r = runner.invoke(app, ["start"])
    assert r.exit_code == 1 and "No ticket id" in r.output


def test_start_outside_repo_needs_project(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", str(tmp_path / "mem")])
    r = runner.invoke(app, ["start", "PAY-1"])
    assert r.exit_code == 1 and "Not inside a Git repository" in r.output


def test_commands_fail_cleanly_without_init(runner, tmp_path):
    for args in (["show", "PAY-1"], ["search", "x"], ["update", "PAY-1", "--set", "status=done"], ["index"]):
        r = runner.invoke(app, args)
        assert r.exit_code == 1 and "not an initialised memory directory" in r.output
    assert runner.invoke(app, ["validate"]).exit_code == 1


def test_show_unknown_ticket(runner, memdir):
    r = runner.invoke(app, ["show", "PAY-4040"])
    assert r.exit_code == 1 and "memory start PAY-4040" in r.output


def test_update_ai_with_fake_command_provider(runner, memdir, ticket, tmp_path):
    answer = {"entries": [{"section": "implementation", "text": "Retry worker added", "confidence": "verified", "source": "MR !1"},
                          {"section": "decisions", "text": "Unsourced claim", "confidence": "verified"}],
              "people": [{"name": "Priya Sharma", "role": "QA", "source": "ticket"}]}
    script = tmp_path / "ai.py"
    script.write_text(f"import sys, json; sys.stdin.read(); print('here you go ```json\\n' + json.dumps({answer!r}) + '\\n```')".replace("'", '"') if False else
                      "import sys, json\nsys.stdin.read()\nprint('```json')\nprint(json.dumps(" + repr(answer) + "))\nprint('```')\n")
    src = tmp_path / "src.md"; src.write_text("PAY-1234 source")
    runner.invoke(app, ["ingest", ticket, "--note", str(src), "--no-brief"])
    assert runner.invoke(app, ["update", ticket, "--ai"]).exit_code == 1  # no provider configured
    runner.invoke(app, ["config", "--set", "ai.provider=command", "--set", f"ai.command={sys.executable} {script}"])
    r = runner.invoke(app, ["update", ticket, "--ai"])
    assert r.exit_code == 0, r.output
    text = read_text(find_ticket_dir(memdir, ticket) / "MEMORY.md")
    assert "Retry worker added" in text and "Priya Sharma" in text
    assert "downgraded to unconfirmed" in r.output
    dec = read_text(find_ticket_dir(memdir, ticket) / "technical-decisions.md")
    assert "[unconfirmed] Unsourced claim" in dec


def test_install_instructions_idempotent_and_non_destructive(runner, tmp_path):
    target = tmp_path / "app"
    target.mkdir()
    (target / "CLAUDE.md").write_text("# My project rules\nBe nice.\n")
    for _ in range(2):
        assert runner.invoke(app, ["install-instructions", "claude", "--target", str(target), "--memory-path", "/data/mem"]).exit_code == 0
    text = (target / "CLAUDE.md").read_text()
    assert text.startswith("# My project rules") and text.count("engineering-memory:start") == 1
    assert "/data/mem" in text and "{{MEMORY_DIR}}" not in text
    for agent, rel in (("cursor", ".cursor/rules/engineering-memory.mdc"), ("codex", "AGENTS.md"), ("generic", "ENGINEERING_MEMORY.md")):
        assert runner.invoke(app, ["install-instructions", agent, "--target", str(target)]).exit_code == 0
        assert (target / rel).exists()
    assert "alwaysApply: true" in (target / ".cursor/rules/engineering-memory.mdc").read_text()
    assert runner.invoke(app, ["install-instructions", "vim"]).exit_code == 1


def test_packaged_and_root_instructions_in_sync():
    pkg = REPO_ROOT / "src" / "engineering_memory" / "instructions"
    for f in ("CLAUDE.md", "CURSOR.md", "CODEX.md", "GENERIC_AGENT.md"):
        assert (pkg / f).read_text() == (REPO_ROOT / "instructions" / f).read_text()
        assert "Never invent anything" in (pkg / f).read_text()

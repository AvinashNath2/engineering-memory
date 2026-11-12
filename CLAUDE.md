# CLAUDE.md - working on the Engineering Memory codebase

This file is for developing **this project**. (The instructions for *using* the memory in your own repos are in `instructions/CLAUDE.md` and are installed with `memory install-instructions claude`.)

## What this is

A local-first, Markdown-based engineering memory with a Typer CLI (`memory`). Markdown files are the only source of truth; SQLite FTS5 is an optional, rebuildable cache. Python 3.12+, PyYAML, Typer, Git CLI.

## Commands

```bash
pip install -e ".[dev]"     # install
pytest                      # all tests (fast, uses temp Git repos)
memory --help               # CLI
```

## Architecture in one paragraph

`cli.py` parses and prints. Logic lives in `capture/` (Git → `Activity`), `memory/` (`creator`, `entries`, `updater`, `ingest`, `indexer`, `validator`, `layout`), `search/`, `ai/`, with `store.py` (atomic IO, lock, frontmatter), `security.py` (redaction, sensitive paths) and `config.py` beneath. All ticket mutations go through `TicketSession` in `memory/updater.py`. Details: `docs/ARCHITECTURE.md`; file format: `docs/MEMORY_FORMAT.md`.

## Invariants - never break these

- Append-only: no code path may delete or rewrite an existing entry.
- `verified` entries must have a source (enforced in `entries.Entry.validate`, `to_requests`, and the validator).
- Everything written passes `security.redact_secrets`; Git paths pass `is_sensitive_path`; diffs are reduced to paths.
- Never invent people, roles, tickets, dates or decisions in code or templates. Role comes from an explicit `Field: Name` line or a cited source.
- Generated files (`INDEX.md`, `PEOPLE.md`, `PROJECTS.md`, `people/*.md`) are deterministic and written only when changed.
- The core must work with no network, no AI key, and no SQLite FTS5.
- `instructions/` and `src/engineering_memory/instructions/` must stay identical (a test enforces it).

## Conventions

- Typed, small functions; user-facing failures raise `MemoryError_`, `GitError`, `StoreError`, `ConfigError` or `ProviderError`, which `@guarded` turns into `error: …` and exit code 1.
- Tests: pytest, real temp repos over mocks, one behaviour per test, include the unhappy path. Fixtures are in `tests/conftest.py`.
- Update `README.md`, `docs/USAGE.md` and `docs/MEMORY_FORMAT.md` in the same change as behaviour.

## When asked to extend

Importers for Jira, Confluence or Git hosts belong in new optional modules that emit sanitised text and call `update_ticket`; do not add network code to the core paths.

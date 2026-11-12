# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Python 3.12+, Git on `PATH`. No services are needed. Tests build throwaway Git repositories in temporary directories and never touch your home directory or real memory (`tests/conftest.py` redirects `EM_CONFIG`, `EM_MEMORY_DIR`, `HOME` and Git config).

## Ground rules

These are product requirements, not style preferences. A change that breaks one will be rejected.

1. Markdown stays the source of truth. No mandatory database, server or network call; SQLite is a rebuildable cache.
2. No vendor SDK in the core. AI lives behind `ai/provider.py`.
3. Writes are additive. Nothing deletes or rewrites existing entries; all file writes go through `store.atomic_write`/`write_if_unchanged` under `memory_lock`.
4. Facts need provenance: `verified` requires a source; unknown is recorded as `unknown`.
5. Privacy: nothing that could contain a secret or source code is stored without passing `security.redact_secrets`; sensitive paths are never captured.
6. Output is Git-friendly: one entry per line, deterministic generated files, no timestamps in generated views.
7. No placeholder implementations for core features.

## Layout

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Keep printing and argument parsing in `cli.py`; keep logic in the packages below it so it is testable without the CLI.

## Tests

- Add a test with every behaviour change; prefer integration tests over mocks (real temporary Git repos, real files).
- Cover the unhappy paths: missing information, duplicates, malformed files, secrets.
- `pytest -q` must pass. The suite includes a check that `instructions/` and `src/engineering_memory/instructions/` are identical; edit one and copy to the other.

## Adding things

| Task | Where |
| --- | --- |
| New CLI command | `cli.py` (use `@guarded`, `_env`), add the name to `COMMANDS` in `tests/test_ai_and_cli.py` and document it in README and USAGE |
| New memory section | `SECTIONS` in `memory/updater.py`, the template in `templates/`, `docs/MEMORY_FORMAT.md` |
| New secret pattern | `_PATTERNS` in `security.py` + parametrised case in `tests/test_security_store.py` |
| New agent instructions | `instructions/` and `src/engineering_memory/instructions/`, `install_instructions` in `cli.py` |
| Importer (Jira, Confluence, …) | New module that writes sanitised sources and calls `update_ticket`; keep it optional |

## Commit and PR style

Small, focused commits; describe the behaviour change and the test that proves it. Update the docs in the same change. Changing the on-disk format requires a `schema_version` bump and a migration path.

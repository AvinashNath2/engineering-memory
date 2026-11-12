# Architecture

## Principles

1. **Markdown is the database.** Everything the system knows lives in `.md` files. The optional SQLite file is a cache that can be deleted at any time.
2. **The memory is separate from the tool.** The CLI is installed per machine; the memory directory is data you clone, copy and back up.
3. **The engine is agent-independent.** No vendor SDK is imported. Agents interact through the CLI (or by reading/writing the Markdown following `MEMORY_FORMAT.md`). One optional adapter can call any command-line AI.
4. **Deterministic first, AI second.** Everything that can be done reliably without AI (Git capture, redaction, link and explicit-field extraction, dedupe, indexing, validation, search) is done in code. The AI supplies only the narrative judgement (what was built, why, which decisions) and is constrained to cite sources.
5. **Never lose or invent history.** Append-only writes, duplicate detection, conflict checks, `verified` requires a source, `unknown` is first-class.

## Module map

```
src/engineering_memory/
├── cli.py                Typer commands: parsing, printing, error handling only
├── config.py             machine-local YAML config (memory_dir, ai, search, patterns)
├── security.py           secret redaction/detection, sensitive path rules, URL sanitising
├── store.py              frontmatter, atomic writes, lock, compare-before-write, timestamps
├── capture/
│   ├── git_collector.py  Git CLI wrapper: repo, branch, base ref, commits, changed paths, PR refs
│   └── activity_collector.py  ticket detection + Activity snapshot; no-base-branch safeguard
├── memory/
│   ├── layout.py         paths, constants, ticket/project lookup and resolution
│   ├── creator.py        init memory dir, projects, ticket creation (from templates/)
│   ├── entries.py        entry format, ids, duplicate detection, section editing
│   ├── updater.py        TicketSession + update_ticket + record_activity (all writes)
│   ├── ingest.py         sources → sanitised attachments, diff reduction, link/people extraction
│   ├── indexer.py        INDEX/PROJECTS/PEOPLE/people generation; optional FTS5 index
│   └── validator.py      schema, links, chronology, duplicates, evidence, secrets
├── search/
│   ├── searcher.py       text scan + FTS5 candidate path, ranking, filters, snippets
│   └── retriever.py      load ticket, timeline, people aggregation
├── ai/
│   ├── provider.py       AIProvider protocol, NullProvider, CommandProvider, response validation
│   └── prompts.py        agent brief and strict-JSON extraction prompt
├── templates/            feature.md, timeline.md, decision.md, discussion.md, testing.md, project.md
└── instructions/         CLAUDE.md, CURSOR.md, CODEX.md, GENERIC_AGENT.md (copied by init / install-instructions)
```

Dependency direction: `cli → {capture, memory, search, ai} → store/security/config`. Nothing below `cli` prints; nothing outside `ai/` knows about models.

## Data flow

```
 Git repo ──capture──► Activity ──record_activity──┐
                                                   ▼
 ticket / Confluence / MR text ──ingest──► attachments/ + Sources, team, links
                                                   │
 AI agent (or `update --ai`) reads attachments ──memory update──► TicketSession
                                                   │
                       ┌───────────────────────────┴──────────────────────────┐
                       ▼                                                      ▼
        ticket Markdown files (source of truth)                     indexer: INDEX/PEOPLE/PROJECTS
                       │
                       ▼
     search (text | FTS) · show · timeline · AI agent reading the folder
```

## Write path (safety)

Every mutation goes through `TicketSession`:

1. Take the memory-wide lock (`.em.lock`, exclusive create, stale-lock recovery).
2. Read the five ticket files and remember each content hash.
3. For each requested entry: redact secrets → validate (confidence, source) → duplicate/near-duplicate check → render → insert into the section (timeline entries sorted by time).
4. Write only changed files, each via temp file + `fsync` + `os.replace`, after confirming the file still has the hash that was read (`ConflictError` otherwise).
5. Rebuild the generated indexes (content-compared; unchanged files are not rewritten).

Because only additions are expressible, a bug or a bad agent answer can add noise but cannot erase history, and the memory directory's own Git repository provides recovery.

## Privacy model

- Git collection reads metadata (names, paths, messages), never file contents.
- Path denylist: `.env*`, keys, certificates, credential stores, `*.tfvars`, `.aws/`, `.ssh/`, plus user patterns.
- Redaction of private keys, AWS/GitHub/GitLab/Slack tokens, JWTs, bearer tokens, URL credentials and `password=`-style assignments runs on commit subjects, ingested sources and every `memory update` text.
- MR diffs are reduced to paths and line counts before being stored.
- Remote URLs have embedded credentials removed.
- `memory validate` re-scans the stored Markdown as a second line of defence.

## Search

`searcher.py` loads all Markdown files outside hidden directories (excluding the two agent instruction files), tokenises the query (phrases preserved, stop-words dropped), scores each file (term frequency, hits in path/title/headings, ticket-ID boost), and groups by ticket so a ticket whose files together match more of the query outranks one file matching a single term. Filters: project, person (ticket-level), since (ticket `updated`). If `.engineering-memory.sqlite` exists and its signature (path/size/mtime of all Markdown) matches, FTS5 supplies the candidate set; otherwise the scan runs. Both paths share ranking and snippet code, which is why results agree.

## AI integration

`AIProvider.complete(prompt) -> str`. `CommandProvider` runs a configured command with the prompt on stdin. The prompt contains the rules, current memory, stored sources and a JSON schema. `to_requests()` validates the answer: sections must exist, `sources` is not writable by the AI, unknown confidences become `unconfirmed`, and a `verified` claim without a source is downgraded to `unconfirmed` with a warning. Most users will skip this and let their coding agent run `memory update` directly, guided by the instruction files.

## Extensibility

- **Importers** (Jira, Confluence, GitHub/GitLab APIs, Slack) should produce the same artefacts `ingest` does (sanitised text files plus `update_ticket` calls). Nothing in the core needs to change.
- **New sections** are added to `SECTIONS` in `updater.py` and the template.
- **New agents**: add an instruction file and an entry in `install_instructions`.
- **Schema changes**: bump `SCHEMA_VERSION`, add a migration command.

## Known limitations

- Roles are recorded only from explicit `Field: Name` lines or by the agent citing a source; free-text role detection is deliberately not attempted.
- Branch creation time comes from the local reflog and is absent on machines that did not create the branch.
- Without a base branch, commit-to-ticket matching relies on the ticket ID appearing in the commit subject.
- Search is lexical, not semantic.

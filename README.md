# Engineering Memory

A persistent, file-based memory for software developers and their AI coding agents.

You finish a ticket, hand over the ticket, the Confluence page and the merge request, and the facts are filed into structured Markdown. Months later, Claude Code, Cursor, Codex or you yourself can ask *"why did we choose exponential backoff?"* or *"who tested PAY-1234?"* and get an answer grounded in real files, with references.

- **No server, no frontend, no database.** Markdown files are the only source of truth.
- **Agent independent.** The engine never imports an AI SDK. Any agent that can read files and run a shell command can use it.
- **Git-backed and portable.** Copy or clone the folder to another machine and keep going.
- **Honest by design.** Every fact carries a confidence tag (`verified`, `inferred`, `unconfirmed`, `unknown`, `conflict`) and a source. Verified facts without a source are rejected.
- **Private by design.** Secrets are redacted before anything is written; `.env`/key files are never captured; diffs are reduced to file paths.

## 60-second tour

```bash
pip install -e .                                   # see docs/INSTALLATION.md

memory init ~/engineering-memory-data              # one central memory, outside your repos
cd ~/code/payment-service                          # any Git repo
memory project add payment-service --here --prefix PAY

git checkout -b feature/PAY-1234-payment-retry
memory start                                       # detects repo, branch, ticket; creates the memory
# ... work, commit ...
memory capture                                     # branch, commits, changed file paths, PR refs

# ticket done: give it the written material (any Git platform)
memory ingest PAY-1234 --ticket ticket.md --docs confluence.md --mr mr.md
```

`ingest` stores sanitised copies of the sources, extracts links and explicitly labelled people (`Assignee: …`, `Reviewers: …`), and prints an extraction brief. Your AI agent reads the stored sources and records what it learns:

```bash
memory update PAY-1234 --section decisions --confidence verified \
    --source "Confluence: Payment retry design" \
    --text "Chose exponential backoff over fixed delay to avoid retry storms"
memory update PAY-1234 --person "Priya Sharma" --role QA --source "Ticket comment"
memory update PAY-1234 --section open-items --confidence unknown \
    --text "The MR reviewer is not stated in any source"
memory update PAY-1234 --set status=done
```

Later, with no AI at all:

```bash
memory search "payment retry"
memory search "features tested by Priya"
memory show PAY-1234
memory timeline PAY-1234
memory people QA
```

Or open Claude Code on the memory folder and ask in plain English. The instruction files (`CLAUDE.md`, `AGENTS.md`) installed in the memory folder tell the agent exactly how to look things up and cite files.

## What gets stored

```
engineering-memory-data/
├── INDEX.md  PROJECTS.md  PEOPLE.md        generated overviews (start here)
├── CLAUDE.md  AGENTS.md                    agent protocol, so any agent opened here knows the rules
├── projects/payment-service/
│   ├── PROJECT.md                          repos and ticket prefixes
│   └── tickets/PAY-1234/
│       ├── MEMORY.md                       metadata, Git, implementation, code changes, team, docs, sources, open items
│       ├── timeline.md                     chronological events
│       ├── technical-decisions.md
│       ├── discussions.md                  written comments only
│       └── testing.md
├── people/  decisions/  discussions/  daily-logs/
└── attachments/PAY-1234/                   sanitised copies of ticket / Confluence / MR
```

## Commands

| Command | Purpose |
| --- | --- |
| `memory init [PATH]` | Create a memory directory (idempotent) |
| `memory config [--set k=v]` | Show or change configuration |
| `memory project add/list` | Register projects, repos and ticket prefixes |
| `memory start [TICKET]` | Start/resume a ticket, show recorded context |
| `memory capture [TICKET]` | Record branch, commits, changed file paths, PR refs |
| `memory ingest TICKET --ticket F --docs F --mr F` | Store sources, extract links and people, print agent brief |
| `memory update TICKET …` | Append entries, people, links, status (never overwrites) |
| `memory search "query"` | Search without AI (text scan or optional SQLite FTS5) |
| `memory show / timeline TICKET` | Read a ticket memory or its history |
| `memory people [query]` | Find team members and their stated roles |
| `memory index [--fts]` | Rebuild INDEX/PROJECTS/PEOPLE (and the optional search index) |
| `memory validate [--strict]` | Check schema, links, timestamps, duplicates, evidence, secrets |
| `memory status` | Repo, branch, ticket, memory, uncaptured commits |
| `memory export` / `memory commit` | Package or version-control the memory |
| `memory install-instructions AGENT` | Put agent rules into a repo (`claude`, `cursor`, `codex`, `generic`) |

Every command has `--help`. Full walkthrough in [docs/USAGE.md](docs/USAGE.md).

## Documentation

- [docs/INSTALLATION.md](docs/INSTALLATION.md): install, configure, connect an AI agent
- [docs/USAGE.md](docs/USAGE.md): workflows and recipes
- [docs/MEMORY_FORMAT.md](docs/MEMORY_FORMAT.md): the on-disk format (agents and humans)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): modules and design decisions
- [CONTRIBUTING.md](CONTRIBUTING.md): development and tests

## Scope

Version 0.1 is the local Git + Markdown + AI workflow. Jira, Confluence, Slack and Git-host API integrations are intentionally **not** included yet: you hand over exported text, which works with any platform. They can be added later as thin importers that write the same files.

## License

MIT

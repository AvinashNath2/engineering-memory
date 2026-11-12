# Usage

All examples assume the memory is initialised and the project is registered ([INSTALLATION.md](INSTALLATION.md)).

## Workflow A - starting a task

```bash
git checkout -b feature/PAY-1234-payment-retry
memory start            # or: memory start PAY-1234
```

`memory start` detects the repository and branch, extracts the ticket ID, resolves the project (by repository, then by ticket prefix), then either

- **creates** the ticket memory (and captures the branch and commits so far), or
- **resumes** it, printing the recorded summary, implementation, team, open items and the latest timeline entries.

It also lists other memories that mention the ticket. If no project matches, it tells you the exact `memory project add` command to run.

## Workflow B - capturing development activity

```bash
memory capture                 # current branch's ticket
memory capture --dry-run       # preview, write nothing
memory capture --pr https://gitlab.example.com/pay/svc/-/merge_requests/482
```

Captured: repository, branch (and creation time when the reflog has it), commit hashes, subjects, authors and dates, changed file paths with add/modify/delete/rename status, and PR/MR references found in commit messages (`#42`, `See merge request group/proj!77`).

Never captured: file contents, `.env` files, keys and certificates (`*.pem`, `*.key`, `id_rsa`, `.npmrc`, `terraform.tfvars`, …), anything matching `extra_sensitive_patterns`. Secrets in commit subjects are redacted. The warning "N sensitive file path(s) were excluded" tells you when this happened.

Commits belong to the ticket when they are on the branch but not on a base branch (`main`, `master`, `develop`, …). If no base branch exists, only commits whose subject mentions the ticket are captured (override with `--all-commits`). Running `capture` again only adds new commits.

## Workflow C - recording what you learned (the main workflow)

When a ticket is done, give the tool the written material. Export or copy each item to a text/Markdown file:

| Source | Flag | What to include |
| --- | --- | --- |
| Jira ticket | `--ticket` | description, fields (`Assignee:`, `Reporter:`), comments |
| Confluence page | `--docs` | the page text |
| Merge request | `--mr` | description, reviewers/approvers, review comments, optionally the diff |
| Anything else | `--note` | other written notes |

```bash
memory ingest PAY-1234 --ticket ticket.md --docs design.md --mr mr.md
```

This command is deterministic and needs no AI:

1. Redacts secrets and stores a copy under `attachments/PAY-1234/` (changing a source later stores a new version; old ones stay).
2. Reduces diffs to file paths and `+/-` counts. Code is dropped.
3. Extracts links (Confluence, MR/PR, ticket URLs), the title (if still unknown) and people from **explicit** fields only: `Assignee`, `Reporter`, `Author`, `Reviewer(s)`, `Approved by`, `QA`, `Tester`, `Backend`, `Frontend`, `Owner`. Prose such as "Priya did the testing" is never guessed.
4. Registers each source under **Sources** in `MEMORY.md`.
5. Warns when a file never mentions the ticket ID (wrong file?).
6. Prints an **extraction brief** for your agent.

Now the agent reads `attachments/PAY-1234/*` and records the narrative with `memory update`:

```bash
memory update PAY-1234 -s implementation -c verified --source "MR !482 description" \
    --text "Adds exponential backoff (max 5 attempts) to payment retries"
memory update PAY-1234 -s decisions -c verified --source "Confluence: Payment retry design" \
    --text "Backoff chosen over fixed delay to avoid thundering herd"
memory update PAY-1234 -s discussions -c verified --source "MR !482 review comment by Arjun Rao" \
    --text "Arjun asked whether the cap should be configurable; Dev One agreed to follow up"
memory update PAY-1234 -s testing -c unconfirmed --source "Ticket comment" \
    --text "Priya said she would test timeouts; no result recorded"
memory update PAY-1234 -s open-items -c unknown \
    --text "No source states who tested the feature"
memory update PAY-1234 --person "Priya Sharma" --role QA --source "Ticket comment"
memory update PAY-1234 --link https://acme.atlassian.net/wiki/x --link-label "Retry design"
memory update PAY-1234 --set status=done
```

Batch form for agents (one call, many entries):

```bash
memory update PAY-1234 --json answer.json
```

```json
{"entries": [{"section": "implementation", "text": "…", "confidence": "verified", "source": "MR !482"}],
 "people":  [{"name": "Priya Sharma", "role": "QA", "source": "Ticket comment"}],
 "links":   [{"url": "https://…", "label": "Design", "kind": "docs"}],
 "set":     {"status": "done"}}
```

Rules the tool enforces:

- `--confidence verified` **requires** `--source`.
- Exact and near-duplicates are skipped, so re-running an update is safe.
- Entries are only ever appended; timeline entries are inserted in chronological order.
- Secrets in any text are redacted before saving.
- `--set` accepts only `title`, `status`, `type`; each change is logged in the timeline.
- Sections: `summary implementation code-changes git team docs sources open-items decisions discussions testing timeline`.

Confidence levels: `verified` (stated in a source), `inferred` (deduced), `unconfirmed` (mentioned, not confirmed), `unknown` (needed but absent), `conflict` (sources disagree: state both).

## Workflow D - searching without AI

```bash
memory search "payment retry implementation"
memory search PAY-1234
memory search "features tested by Priya"
memory search backoff --project payment-service --since 2026-10-01
memory search "gateway timeout" --person Priya --json
```

Quoted text is an exact phrase; common words (`who`, `by`, `the`) are ignored. Results rank tickets whose files together cover more of your terms first, curated memory above raw attachments, and show file paths with line numbers.

Optional speed-up for large memories: `memory index --fts` builds a SQLite FTS5 index (`.engineering-memory.sqlite`, git-ignored). It is used only while it is fresh; after any change the search silently falls back to the plain text scan, so results are never stale. Delete it any time.

More views: `memory show PAY-1234`, `memory timeline PAY-1234`, `memory people`, `memory people QA`, and the generated `INDEX.md`, `PEOPLE.md`, `PROJECTS.md`.

## Workflow E - recalling through an AI agent

Open Claude Code (or Cursor/Codex) in the memory folder, or in a repo where you ran `memory install-instructions`, and ask:

- *What did I work on last month?*
- *Who tested the payment retry feature?*
- *Which branch had the changes for PAY-1234?*
- *Why did we choose exponential backoff?*
- *Which Confluence page documents it?*

The agent follows the protocol in `CLAUDE.md`/`AGENTS.md`: read `INDEX.md`, search, read the full files, answer only from them, mark what is unconfirmed, say "not recorded" for gaps, and cite file paths.

## Housekeeping

```bash
memory validate            # exit code 1 on errors; --strict also fails on warnings; --json for tooling
memory index               # refresh INDEX.md / PROJECTS.md / PEOPLE.md (also done after every write)
memory status              # repo, branch, ticket, memory file, how many commits are not yet captured
memory commit -m "PAY-1234: done"    # version the memory itself with Git
memory export -o backup.zip          # portable archive (add --include-git for full history)
```

`memory validate` checks: required files and frontmatter, ticket/project consistency, valid statuses, ISO timestamps, `created ≤ updated`, no future or out-of-order timeline entries, `[verified]` entries without a source, duplicate entry ids or duplicate ticket memories, broken relative links, and probable secrets.

## Working in a team

The memory is plain files in Git: share it through a private remote. The lock file and atomic writes protect against concurrent runs on one machine; if a file is edited by someone else while `memory update` runs, the update aborts with a conflict message instead of overwriting, and you simply re-run it. After pulling changes from teammates, run `memory validate` and `memory index`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `not an initialised memory directory` | `memory init <path>` or `memory config --set memory_dir=<path>` |
| `No project matches repo …` | `memory project add <name> --here --prefix <KEY>` |
| `No ticket id found in branch` | pass the ticket: `memory start PAY-1234` |
| `Memory is locked by another process` | wait, or delete a stale `.em.lock` in the memory folder |
| `modified by someone else while updating` | re-run the command |
| No FTS results / `[text]` strategy | the index is stale or missing; run `memory index --fts` |

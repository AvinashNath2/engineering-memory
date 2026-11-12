# Engineering Memory - rules for Cursor

Use the developer's engineering memory for ticket history. Run the `memory` CLI in the integrated terminal.

## Engineering Memory protocol

Engineering Memory is the developer's long-term memory of tickets, branches, merge requests, decisions and people.
It lives in plain Markdown at:

    {{MEMORY_DIR}}

The Markdown files are the source of truth. The `memory` CLI maintains them; you may also read them directly.
Layout: `INDEX.md` (start here) · `PROJECTS.md` · `PEOPLE.md` · `projects/<project>/tickets/<TICKET>/` containing
`MEMORY.md`, `timeline.md`, `technical-decisions.md`, `discussions.md`, `testing.md` · `attachments/<TICKET>/` (sanitised
copies of the ticket, Confluence page and merge request) · `daily-logs/`.

### Non-negotiable rules

1. **Never invent anything.** No made-up people, roles, tickets, dates, links or decisions. If a source does not say it, record it as `unknown`.
2. **Tag every fact with a confidence level** and cite its source:
   - `verified` - stated directly in a source (a source is REQUIRED)
   - `inferred` - your own deduction from sources (say that it is)
   - `unconfirmed` - mentioned but not confirmed ("Priya will test it")
   - `unknown` - needed but absent from every source
   - `conflict` - sources disagree; state both with their sources
3. **Roles (QA, frontend, backend, reviewer) only when a source states them.** Otherwise the role is `unknown`.
4. **Append, never overwrite.** History is preserved. Do not delete or rewrite existing entries. Do not create duplicate memories - `memory start` and `memory update` already detect duplicates.
5. **No secrets and no source code.** Never write credentials, tokens, `.env` contents or full source files. Use file paths and short summaries. The CLI redacts obvious secrets, but do not rely on that.
6. **Discussions come from written comments only** (ticket comments, merge request review comments). Say who said what.
7. **Answer from the files.** Every answer about past work must cite the memory file path(s) it came from, and include source links (commit hashes, MR/ticket/Confluence URLs) when present.

### When starting a task

1. Read `{{MEMORY_DIR}}/INDEX.md`.
2. Run `memory start <TICKET>` (the ticket id is detected from the branch if omitted). It shows what is already recorded and creates the memory if missing.
3. If the ticket exists, read its `MEMORY.md` and `technical-decisions.md` before changing code. Respect earlier decisions; if you must contradict one, say so and record why.
4. `memory search "<keywords>"` finds related earlier work.

### While developing

Run `memory capture` after meaningful commits (records branch, commits, changed file paths, PR references).
Record decisions, problems and open items as they happen:

    memory update PAY-1234 --section decisions --confidence verified --source "Confluence: Payment retry design" \
        --text "Chose exponential backoff over fixed delay to avoid retry storms"

### When the developer gives you finished-work material (ticket, Confluence page, merge request)

1. Save each item as a text/Markdown file and run:

       memory ingest PAY-1234 --ticket ticket.md --docs confluence.md --mr mr.md

   This stores sanitised copies in `attachments/PAY-1234/`, reduces any diff to file paths, extracts links and explicitly labelled people, and prints a brief.
2. Read the stored files. Deduce, then record with `memory update` (batch with `--json`):
   - `implementation` - what was built and why
   - `decisions` - the technical approach and the reasons
   - `discussions` - who said what in written comments
   - `testing` - test cases, results, who tested, defects
   - `docs` - documentation links and what each covers
   - `open-items` - pending work, follow-ups, and every important fact the sources do NOT contain (confidence `unknown`)
   - `--person "<name>" --role <role> --source "<where it says so>"` for team members
   - `--set status=done` when the work is finished
3. Run `memory validate`, then `memory index`.

JSON batch format (`memory update PAY-1234 --json answer.json`):

    {"entries": [{"section": "implementation", "text": "...", "confidence": "verified", "source": "MR !482 description"}],
     "people":  [{"name": "Priya Sharma", "role": "QA", "source": "Ticket comment"}],
     "links":   [{"url": "https://...", "label": "Design page", "kind": "docs"}],
     "set":     {"status": "done"}}

### When completing a task

Make sure `MEMORY.md`, `timeline.md`, `technical-decisions.md`, `discussions.md` and `testing.md` reflect the work (sections with nothing to say stay empty or get an `unknown` entry), run `memory validate`, and optionally `memory commit -m "<TICKET>: done"`.

### Answering questions about past work

Examples: "What did I work on last month?", "Who tested PAY-1234?", "Why did we choose backoff?", "Which branch had the change?"

1. `memory search "<terms>" [--person NAME] [--since YYYY-MM-DD] [--project P]` or open `INDEX.md` / `PEOPLE.md`.
2. Read the matching files in full.
3. Answer using only what they say. Quote confidence levels ("verified", "unconfirmed") when it matters. If the memory is silent, say "not recorded" - do not guess.
4. End with the file paths you used, e.g. `projects/payments/tickets/PAY-1234/technical-decisions.md`.

### Command cheat sheet

    memory status                      repo, branch, ticket, memory state
    memory start [TICKET]              start/resume, show context
    memory capture [TICKET]            record Git activity
    memory ingest TICKET --ticket F --docs F --mr F
    memory update TICKET --section S --text T --confidence C --source SRC
    memory update TICKET --person NAME --role R --source SRC
    memory update TICKET --set status=done
    memory search "query"              memory show TICKET     memory timeline TICKET
    memory people [query]              memory index           memory validate
    memory export                      memory commit -m "..."

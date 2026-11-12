# Installation

## Requirements

- Python 3.12 or newer
- Git on your `PATH`
- Nothing else. No database server, no API key. (SQLite ships with Python; FTS5 is used if available and is optional.)

## Install

```bash
git clone <this repository> engineering-memory
cd engineering-memory
python -m venv .venv && source .venv/bin/activate     # recommended
pip install -e .                                       # add ".[dev]" to run the tests
memory --version
```

To make `memory` available everywhere, use `pipx install -e .` (or `pipx install .`).

## Create your memory

Keep the memory **outside** your application repositories so one memory serves every repo and agent:

```bash
memory init ~/engineering-memory-data
```

This creates the folder layout, a `.gitignore`, the agent instruction files (`CLAUDE.md`, `AGENTS.md`), and runs `git init` so the memory has its own history (`--no-git` to skip). It also writes your machine-local configuration to `~/.config/engineering-memory/config.yaml`.

Register each project once, from inside its repository:

```bash
cd ~/code/payment-service
memory project add payment-service --here --prefix PAY
```

`--prefix` lets a ticket like `PAY-1234` find its project even when the repo name differs. Repeat for more repositories and projects.

## Configuration

See [`config.example.yaml`](../config.example.yaml). Inspect or change values with:

```bash
memory config --show
memory config --set max_commits=100
memory config --set ai.provider=command --set "ai.command=claude -p"
```

| Setting | Meaning |
| --- | --- |
| `memory_dir` | Where the memory lives (override per call with `-d PATH` or `$EM_MEMORY_DIR`) |
| `ticket_pattern` | Regex for ticket IDs in branch names |
| `base_branches` | Branches treated as the base when deciding which commits belong to a feature branch |
| `max_commits` | Upper bound per `memory capture` |
| `extra_sensitive_patterns` | Additional globs that are never captured |
| `ai.provider` / `ai.command` | Optional: `command` runs any CLI that reads a prompt on stdin and prints JSON |
| `search.use_fts` | Use the optional SQLite FTS5 index when it is fresh |

`$EM_CONFIG` selects a different config file.

## Connect an AI coding agent

The memory folder already contains `CLAUDE.md` and `AGENTS.md`, so an agent opened **in the memory folder** knows the protocol. To make agents working in your **code repositories** use it too:

```bash
memory install-instructions claude  --target ~/code/payment-service   # appends a marked block to CLAUDE.md
memory install-instructions cursor  --target ~/code/payment-service   # .cursor/rules/engineering-memory.mdc
memory install-instructions codex   --target ~/code/payment-service   # AGENTS.md
memory install-instructions generic --target ~/code/payment-service   # ENGINEERING_MEMORY.md
```

The command is idempotent: it replaces only its own marked block and never touches the rest of your file. The block embeds the absolute path of your memory folder.

### Claude Code

1. Run `memory install-instructions claude --target <repo>`.
2. In the repo, ask: *"Start work on PAY-1234"* or *"Why did we choose backoff for payment retries?"* Claude reads `INDEX.md`, runs `memory start` / `memory search`, and answers with file paths.
3. To ask about the whole history, run Claude Code inside the memory folder.

### Optional: let the CLI call an AI for you

`memory update PAY-1234 --ai` sends the stored sources to a command you configure and applies its JSON answer (unsourced "verified" claims are downgraded automatically):

```bash
memory config --set ai.provider=command --set "ai.command=claude -p"
```

This is optional. Without it, your agent runs `memory update` itself.

## Move to another machine

```bash
# on the old machine
cd ~/engineering-memory-data && git remote add origin <private-remote> && git push -u origin main
# on the new machine
git clone <private-remote> ~/engineering-memory-data
pip install -e ~/engineering-memory && memory config --set memory_dir=~/engineering-memory-data
```

Or `memory export` to produce a zip. Nothing machine-specific is stored inside the memory.

## Uninstall

`pip uninstall engineering-memory`. Your memory folder is plain Markdown and stays usable.

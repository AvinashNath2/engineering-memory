"""`memory` command line interface."""

from __future__ import annotations

import functools
import json
import re
import sys
import tarfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

import typer

from . import __version__
from .ai.prompts import build_brief, build_extraction_prompt
from .ai.provider import ProviderError, extract_json, get_provider, to_requests
from .capture.activity_collector import Activity, collect_activity, detect_ticket, restrict_to_ticket
from .capture.git_collector import GitError, repo_info
from .config import Config, ConfigError, config_path, load_config, save_config, set_value
from .memory.creator import add_project, create_ticket, init_memory_dir
from .memory.entries import CONFIDENCE, ID_RE
from .memory.indexer import FTS_FILE, build_fts, fts_available, rebuild_index
from .memory.ingest import SourceFile, ingest_sources
from .memory.layout import (
    MemoryError_,
    STATUSES,
    TYPES,
    find_ticket_dir,
    list_projects,
    normalize_ticket,
    require_memory_dir,
    resolve_project,
)
from .memory.updater import (
    SECTIONS,
    EntryRequest,
    LinkRequest,
    PersonRequest,
    UpdateResult,
    record_activity,
    update_ticket,
)
from .memory.validator import validate as validate_memory
from .search.retriever import filled_sections, find_people, load_ticket, require_ticket, timeline_entries
from .search.searcher import search as search_memory
from .store import StoreError, instructions_dir, parse_frontmatter

app = typer.Typer(
    name="memory",
    help="Engineering Memory: a local-first, Markdown-based memory for developers and AI coding agents.",
    no_args_is_help=True,
    add_completion=False,
)
project_app = typer.Typer(help="Register and list projects.", no_args_is_help=True)
app.add_typer(project_app, name="project")

ERRORS = (MemoryError_, GitError, StoreError, ConfigError, ProviderError)


def guarded(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        try:
            return fn(*args, **kwargs)
        except ERRORS as exc:
            typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

    return wrapper


def ok(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.GREEN)


def warn(msg: str) -> None:
    typer.secho(f"warning: {msg}", fg=typer.colors.YELLOW, err=True)


def _env(ctx: typer.Context, need_init: bool = True) -> tuple[Config, Path]:
    cfg = load_config()
    md = cfg.resolved_memory_dir((ctx.obj or {}).get("memory_dir"))
    if need_init:
        require_memory_dir(md)
    return cfg, md


def _clean(text: str) -> str:
    return re.sub(r"[ \t]+$", "", ID_RE.sub("", text), flags=re.M).rstrip()


def _version_cb(value: bool) -> None:
    if value:
        typer.echo(f"engineering-memory {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    memory_dir: Optional[Path] = typer.Option(
        None, "--memory-dir", "-d", help="Memory directory (default: config / $EM_MEMORY_DIR)."
    ),
    version: bool = typer.Option(False, "--version", help="Show version and exit.", callback=_version_cb, is_eager=True),
) -> None:
    ctx.obj = {"memory_dir": memory_dir}


# ---------------------------------------------------------------- init / config
@app.command()
@guarded
def init(
    ctx: typer.Context,
    path: Optional[Path] = typer.Argument(None, help="Where to create the memory directory."),
    no_git: bool = typer.Option(False, "--no-git", help="Do not run `git init` in the memory directory."),
) -> None:
    """Initialise a memory directory (idempotent) and point the configuration at it."""
    cfg = load_config()
    target = path or cfg.resolved_memory_dir((ctx.obj or {}).get("memory_dir"))
    target, created = init_memory_dir(target, git=not no_git)
    cfg.memory_dir = str(target)
    cfg_file = save_config(cfg)
    ok(f"{'Created' if created else 'Already initialised:'} memory directory {target}")
    typer.echo(f"Config saved to {cfg_file}")
    typer.echo("Next: `memory project add <name> --here --prefix <KEY>` then `memory start <TICKET>`.")


@app.command("config")
@guarded
def config_cmd(
    show: bool = typer.Option(False, "--show", help="Print the current configuration."),
    set_: List[str] = typer.Option([], "--set", help="Set key=value, e.g. ai.provider=command (repeatable)."),
) -> None:
    """Show or change configuration (memory directory, AI provider, search, ...)."""
    cfg = load_config()
    for item in set_:
        if "=" not in item:
            raise ConfigError(f"--set expects key=value, got '{item}'")
        k, v = item.split("=", 1)
        set_value(cfg, k.strip(), v)
    if set_:
        save_config(cfg)
        ok(f"Saved {config_path()}")
    if show or not set_:
        import yaml

        typer.echo(f"# {config_path()}")
        typer.echo(yaml.safe_dump(cfg.to_dict(), sort_keys=False))


# ---------------------------------------------------------------- projects
@project_app.command("add")
@guarded
def project_add(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Project name, e.g. payment-service."),
    here: bool = typer.Option(False, "--here", help="Register the Git repository in the current directory."),
    repo_name: Optional[str] = typer.Option(None, "--repo", help="Repository name to link to this project."),
    remote: Optional[str] = typer.Option(None, "--remote", help="Repository remote URL."),
    prefix: List[str] = typer.Option([], "--prefix", "-p", help="Ticket prefix, e.g. PAY (repeatable)."),
    description: str = typer.Option("", "--description"),
) -> None:
    """Register a project (and optionally link repositories and ticket prefixes)."""
    _, md = _env(ctx)
    if here:
        info = repo_info(Path.cwd())
        repo_name, remote = repo_name or info.name, remote or info.remote
    pdir = add_project(md, name, repo_name=repo_name, remote=remote, ticket_prefixes=prefix, description=description)
    rebuild_index(md)
    ok(f"Project ready: {pdir.relative_to(md)}")


@project_app.command("list")
@guarded
def project_list(ctx: typer.Context) -> None:
    """List registered projects."""
    _, md = _env(ctx)
    projects = list_projects(md)
    if not projects:
        typer.echo("No projects. Run `memory project add <name> --here --prefix KEY`.")
    for p in projects:
        repos = ", ".join(r.get("name", "?") for r in p.repos) or "-"
        typer.echo(f"{p.slug:30} prefixes: {', '.join(p.ticket_prefixes) or '-':12} repos: {repos}")


# ---------------------------------------------------------------- shared git helper
def _activity(cfg: Config, ticket: str | None, limit: int | None, since: str | None, all_commits: bool) -> Activity:
    act = collect_activity(Path.cwd(), cfg, ticket=ticket, limit=limit, since=since)
    if not act.ticket:
        raise MemoryError_(
            f"No ticket id found in branch '{act.repo.branch}'. Pass it explicitly, e.g. `memory capture PAY-1234`."
        )
    if not all_commits:
        act, restricted = restrict_to_ticket(act)
        if restricted:
            warn(
                f"no base branch ({', '.join(cfg.base_branches[:3])}...) found, so only commits mentioning "
                f"{act.ticket} are captured. Use --all-commits to capture everything."
            )
    return act


def _print_update(res: UpdateResult) -> None:
    for a in res.added:
        typer.echo(f"  + {a}")
    for s in res.skipped:
        typer.echo(f"  = skipped {s}")
    for w in res.warnings:
        warn(w)
    if res.redactions:
        warn(f"{res.redactions} potential secret(s) were redacted before saving")
    if not res.added and not res.skipped:
        typer.echo("  (already up to date)")


def _print_context(md: Path, ticket: str) -> None:
    tm = require_ticket(md, ticket)
    meta = tm.meta
    typer.secho(f"{tm.ticket} — {meta.get('title', 'unknown')}  [{meta.get('status')}]", bold=True)
    typer.echo(f"memory: {tm.rel(md)}/MEMORY.md")
    typer.echo(f"project: {meta.get('project')} · repository: {(meta.get('repository') or {}).get('name', 'unknown')}")
    if meta.get("branches"):
        typer.echo("branches: " + ", ".join(meta["branches"]))
    fs = filled_sections(tm)
    _, body = parse_frontmatter(tm.files["MEMORY.md"])
    current, shown = None, 0
    for line in body.splitlines():
        if line.startswith("## "):
            current, shown = line[3:].strip(), 0
            if current in ("Summary", "Implementation", "Team", "Open items") and fs.get(current):
                typer.secho(f"\n{current}", fg=typer.colors.CYAN, bold=True)
        elif current in ("Summary", "Implementation", "Team", "Open items") and line.startswith("- ") and shown < 8:
            typer.echo(_clean(line))
            shown += 1
    empty = [s for s in ("Implementation", "Team", "Documentation") if not fs.get(s)]
    if empty:
        typer.echo("\nNot yet recorded: " + ", ".join(empty))
    tl = timeline_entries(tm)[-5:]
    if tl:
        typer.secho("\nRecent timeline", fg=typer.colors.CYAN, bold=True)
        for ts, text in tl:
            typer.echo(f"  {ts}  {text}")


# ---------------------------------------------------------------- start
@app.command()
@guarded
def start(
    ctx: typer.Context,
    ticket: Optional[str] = typer.Argument(None, help="Ticket id (default: detected from the current branch)."),
    project: Optional[str] = typer.Option(None, "--project", help="Project (default: resolved from repo / ticket prefix)."),
    title: Optional[str] = typer.Option(None, "--title", help="Ticket title, if known."),
    ticket_type: str = typer.Option("feature", "--type", help=f"One of: {', '.join(TYPES)}"),
    all_commits: bool = typer.Option(False, "--all-commits", help="Capture all commits even without a base branch."),
) -> None:
    """Start or resume work on a ticket: detect repo/branch, show recorded context, create memory if missing."""
    cfg, md = _env(ctx)
    info = None
    try:
        info = repo_info(Path.cwd())
    except GitError:
        if not project:
            raise MemoryError_("Not inside a Git repository. Run from a repo, or pass --project and a ticket id.")
    resolved = normalize_ticket(ticket) if ticket else detect_ticket(info.branch if info else None, cfg.ticket_pattern)
    if not resolved:
        raise MemoryError_(f"No ticket id in branch '{info.branch if info else '?'}'. Pass one: `memory start PAY-1234`.")
    typer.secho(f"Repository: {info.name if info else '-'}   Branch: {(info.branch if info else None) or '(detached)'}   Ticket: {resolved}")
    existing = find_ticket_dir(md, resolved)
    if existing:
        typer.secho("Resuming existing memory.\n", fg=typer.colors.GREEN)
        _print_context(md, resolved)
    else:
        proj = resolve_project(md, explicit=project, repo_name=info.name if info else None,
                               remote=info.remote if info else None, ticket=resolved)
        if not proj:
            raise MemoryError_(
                f"No project matches repo '{info.name if info else '?'}' or prefix '{resolved.split('-')[0]}'. "
                f"Register one: memory project add <name> --here --prefix {resolved.split('-')[0]}"
            )
        tdir, _ = create_ticket(md, proj.slug, resolved, title=title, ticket_type=ticket_type,
                                repository={"name": info.name, "remote": info.remote} if info else None)
        ok(f"Created memory for {resolved} in project '{proj.slug}': {tdir.relative_to(md)}/")
        if info:
            act = _activity(cfg, resolved, None, None, all_commits)
            res = record_activity(md, resolved, act)
            _print_update(res)
        rebuild_index(md)
    hits, _ = search_memory(md, resolved, limit=6, use_fts=cfg.search.use_fts)
    own = f"/{resolved}/"
    other = [h for h in hits if own not in h.path and not h.path.startswith(f"attachments/{resolved}/")]
    if other:
        typer.secho("\nOther memories mentioning this ticket", fg=typer.colors.CYAN, bold=True)
        for h in other:
            typer.echo(f"  {h.path}")


# ---------------------------------------------------------------- capture
@app.command()
@guarded
def capture(
    ctx: typer.Context,
    ticket: Optional[str] = typer.Argument(None, help="Ticket id (default: detected from the branch)."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Max commits to inspect."),
    since: Optional[str] = typer.Option(None, "--since", help="Only commits since this date (git syntax)."),
    pr: List[str] = typer.Option([], "--pr", help="Pull/merge request URL to record (repeatable)."),
    all_commits: bool = typer.Option(False, "--all-commits", help="Capture all commits even without a base branch."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be captured; write nothing."),
) -> None:
    """Capture Git activity (branch, commits, changed file paths, PR refs) into the ticket memory."""
    cfg, md = _env(ctx)
    act = _activity(cfg, ticket, limit, since, all_commits)
    typer.echo(f"{act.repo.name} · branch {act.repo.branch or '(detached)'} · ticket {act.ticket} · "
               f"{len(act.commits)} commit(s) · {len(act.files)} file path(s)")
    if dry_run:
        for c in act.commits:
            typer.echo(f"  {c.short} {c.subject} ({c.author})")
        for f in act.files:
            typer.echo(f"  file {f}")
        if act.excluded_files:
            typer.echo(f"  ({act.excluded_files} sensitive path(s) would be excluded)")
        return
    res = record_activity(md, act.ticket, act, pr_links=pr)
    _print_update(res)
    rebuild_index(md)


# ---------------------------------------------------------------- ingest
@app.command()
@guarded
def ingest(
    ctx: typer.Context,
    ticket: str = typer.Argument(..., help="Ticket id."),
    ticket_file: List[Path] = typer.Option([], "--ticket", "-t", help="Ticket export/text (repeatable)."),
    docs: List[Path] = typer.Option([], "--docs", help="Confluence/doc page content (repeatable)."),
    mr: List[Path] = typer.Option([], "--mr", help="Merge request description/comments/diff (repeatable)."),
    note: List[Path] = typer.Option([], "--note", help="Any other written note (repeatable)."),
    no_brief: bool = typer.Option(False, "--no-brief", help="Do not print the agent extraction brief."),
    ai: bool = typer.Option(False, "--ai", help="Also run the configured AI provider (memory update --ai)."),
) -> None:
    """Store sanitised sources (ticket, docs, MR), extract links/people, and prepare the agent brief."""
    cfg, md = _env(ctx)
    sources = (
        [SourceFile("ticket", p) for p in ticket_file] + [SourceFile("docs", p) for p in docs]
        + [SourceFile("mr", p) for p in mr] + [SourceFile("note", p) for p in note]
    )
    res = ingest_sources(md, ticket, sources, cfg.extra_sensitive_patterns)
    ok(f"Ingested {len(res.saved)} source file(s) for {res.ticket}")
    for p in res.saved:
        typer.echo(f"  saved {p}")
    if res.title_found:
        typer.echo(f"  title: {res.title_found}")
    for name, role in res.people_found:
        typer.echo(f"  person: {name} ({role}) [from explicit field]")
    for url, kind in res.links_found:
        typer.echo(f"  link: {kind} {url}")
    for w in res.warnings:
        warn(w)
    if res.update:
        _print_update(res.update)
    rebuild_index(md)
    if ai:
        _run_ai(md, cfg, res.ticket)
    elif not no_brief:
        typer.echo("\n" + "─" * 60)
        typer.echo(build_brief(md, res.ticket, res.saved))


# ---------------------------------------------------------------- update
def _run_ai(md: Path, cfg: Config, ticket: str) -> UpdateResult:
    prompt, warns = build_extraction_prompt(md, ticket)
    for w in warns:
        warn(w)
    raw = get_provider(cfg).complete(prompt)
    entries, people, links, set_meta, aw = to_requests(extract_json(raw))
    for w in aw:
        warn(w)
    res = update_ticket(md, ticket, entries=entries, people=people, links=links, set_meta=set_meta)
    typer.echo("AI-assisted update:")
    _print_update(res)
    rebuild_index(md)
    return res


@app.command()
@guarded
def update(
    ctx: typer.Context,
    ticket: str = typer.Argument(..., help="Ticket id."),
    section: Optional[str] = typer.Option(None, "--section", "-s", help=f"One of: {', '.join(SECTIONS)}"),
    text: Optional[str] = typer.Option(None, "--text", help="Entry text ('-' reads stdin)."),
    file: Optional[Path] = typer.Option(None, "--file", help="Read entry text from a file."),
    confidence: str = typer.Option("unconfirmed", "--confidence", "-c", help=f"One of: {', '.join(CONFIDENCE)}"),
    source: Optional[str] = typer.Option(None, "--source", help="Where this came from (required for verified)."),
    when: Optional[str] = typer.Option(None, "--when", help="Event date/time (ISO). Default: now."),
    person: Optional[str] = typer.Option(None, "--person", help="Add a team member."),
    role: str = typer.Option("unknown", "--role", help="Role of --person (only if a source states it)."),
    link: List[str] = typer.Option([], "--link", help="Add a link (repeatable)."),
    link_label: str = typer.Option("", "--link-label"),
    link_kind: str = typer.Option("docs", "--link-kind", help="docs | ticket | mr | other"),
    set_: List[str] = typer.Option([], "--set", help=f"Set title=, status= ({', '.join(STATUSES)}) or type=."),
    tag: List[str] = typer.Option([], "--tag"),
    json_file: Optional[Path] = typer.Option(None, "--json", help="Apply a JSON batch ('-' = stdin)."),
    ai: bool = typer.Option(False, "--ai", help="Use the configured AI provider on the stored sources."),
) -> None:
    """Append verified/inferred/unknown information to a ticket memory (history is never overwritten)."""
    cfg, md = _env(ctx)
    ticket = normalize_ticket(ticket)
    if ai:
        _run_ai(md, cfg, ticket)
        return
    entries: list[EntryRequest] = []
    people: list[PersonRequest] = []
    links: list[LinkRequest] = [LinkRequest(u, link_label, link_kind) for u in link]
    meta: dict[str, str] = {}
    for item in set_:
        if "=" not in item:
            raise MemoryError_(f"--set expects key=value, got '{item}'")
        k, v = item.split("=", 1)
        meta[k.strip()] = v
    if json_file:
        raw = sys.stdin.read() if str(json_file) == "-" else json_file.read_text(encoding="utf-8")
        e2, p2, l2, m2, aw = to_requests(extract_json(raw))
        for w in aw:
            warn(w)
        entries += e2; people += p2; links += l2; meta.update(m2)
    body = text
    if body == "-":
        body = sys.stdin.read()
    if file:
        body = file.read_text(encoding="utf-8")
    if body:
        if not section:
            raise MemoryError_(f"--section is required with --text/--file. Choose from: {', '.join(SECTIONS)}")
        entries.append(EntryRequest(section, body, confidence, source, when))
    if person:
        people.append(PersonRequest(person, role, source, confidence if confidence != "unconfirmed" else None))
    if not (entries or people or links or meta or tag):
        raise MemoryError_("Nothing to update. Use --section/--text, --person, --link, --set, --tag, --json or --ai.")
    res = update_ticket(md, ticket, entries=entries, people=people, links=links, set_meta=meta, add_tags=tag)
    ok(f"Updated {ticket}" if res.changed else f"No changes for {ticket}")
    _print_update(res)
    rebuild_index(md)


# ---------------------------------------------------------------- search / show / timeline / people
@app.command()
@guarded
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help='Words or "exact phrases", e.g. "payment retry" or PAY-1234.'),
    project: Optional[str] = typer.Option(None, "--project"),
    person: Optional[str] = typer.Option(None, "--person", help="Only tickets involving this person."),
    since: Optional[str] = typer.Option(None, "--since", help="Only memories updated since YYYY-MM-DD."),
    limit: int = typer.Option(10, "--limit"),
    no_fts: bool = typer.Option(False, "--no-fts", help="Force plain text scan."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Search all Markdown memories (no AI needed)."""
    cfg, md = _env(ctx)
    hits, strategy = search_memory(md, query, project=project, person=person, since=since, limit=limit,
                                   use_fts=cfg.search.use_fts and not no_fts)
    if as_json:
        typer.echo(json.dumps([h.__dict__ for h in hits], indent=2))
        return
    if not hits:
        typer.echo("No matches.")
        return
    typer.secho(f"{len(hits)} result(s) [{strategy}]", dim=True)
    for h in hits:
        label = f"{h.ticket} " if h.ticket else ""
        typer.secho(f"\n{h.path}", bold=True)
        typer.echo(f"  {label}{h.title or ''}  (score {h.score}, {h.matched} term(s))")
        for ln, snip in h.snippets:
            typer.echo(f"  {ln:>4}: {snip}")


@app.command()
@guarded
def show(ctx: typer.Context, ticket: str = typer.Argument(...), raw: bool = typer.Option(False, "--raw", help="Include frontmatter and ids.")) -> None:
    """Display the complete memory of a ticket."""
    _, md = _env(ctx)
    tm = require_ticket(md, ticket)
    for name, text in tm.files.items():
        typer.secho(f"\n{'═' * 8} {tm.rel(md)}/{name} {'═' * 8}", fg=typer.colors.CYAN, bold=True)
        typer.echo(text.rstrip() if raw else _clean(parse_frontmatter(text)[1]))


@app.command()
@guarded
def timeline(ctx: typer.Context, ticket: str = typer.Argument(...)) -> None:
    """Display the chronological history of a ticket."""
    _, md = _env(ctx)
    tm = require_ticket(md, ticket)
    entries = timeline_entries(tm)
    if not entries:
        typer.echo("No timeline entries yet.")
    for ts, text in entries:
        typer.echo(f"{ts}  {text}")


@app.command()
@guarded
def people(ctx: typer.Context, query: Optional[str] = typer.Argument(None, help="Name, role or ticket filter.")) -> None:
    """List or search team members recorded in the memory."""
    _, md = _env(ctx)
    found = find_people(md, query)
    if not found:
        typer.echo("No matching people. (People are recorded only when a source names them.)")
    for p in found:
        typer.secho(p["name"], bold=True)
        for e in p["entries"]:
            src = f"  (source: {e['source']})" if e["source"] else ""
            typer.echo(f"  {e['ticket']:12} {e['role']:14} [{e['confidence']}]{src}")


# ---------------------------------------------------------------- index / validate / status
@app.command()
@guarded
def index(ctx: typer.Context, fts: bool = typer.Option(False, "--fts", help="Also (re)build the optional SQLite FTS5 search index.")) -> None:
    """Rebuild INDEX.md, PROJECTS.md, PEOPLE.md (and optionally the FTS index)."""
    _, md = _env(ctx)
    changed = rebuild_index(md)
    ok("Updated: " + ", ".join(changed) if changed else "Indexes already up to date.")
    if fts:
        if not fts_available():
            warn("SQLite FTS5 is unavailable in this Python; plain text search keeps working.")
        else:
            n = build_fts(md)
            ok(f"FTS index built ({n} documents) -> {FTS_FILE} (rebuildable; not part of your memory)")


@app.command()
@guarded
def validate(
    ctx: typer.Context,
    strict: bool = typer.Option(False, "--strict", help="Treat warnings as errors."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Validate memory files: schema, links, timestamps, duplicates, evidence rules and secrets."""
    _, md = _env(ctx, need_init=False)
    issues = validate_memory(md)
    errors = [i for i in issues if i.level == "error"]
    warns = [i for i in issues if i.level == "warning"]
    if as_json:
        typer.echo(json.dumps([i.__dict__ for i in issues], indent=2))
    else:
        for i in issues:
            typer.secho(str(i), fg=typer.colors.RED if i.level == "error" else typer.colors.YELLOW)
        if not issues:
            ok("Memory is valid.")
        else:
            typer.echo(f"\n{len(errors)} error(s), {len(warns)} warning(s)")
    if errors or (strict and warns):
        raise typer.Exit(1)


@app.command()
@guarded
def status(ctx: typer.Context) -> None:
    """Show the current repository, branch, detected ticket and its memory."""
    cfg, md = _env(ctx, need_init=False)
    typer.echo(f"memory directory: {md} {'(initialised)' if (md / '.engineering-memory.yaml').exists() else '(NOT initialised: run `memory init`)'}")
    try:
        info = repo_info(Path.cwd())
    except GitError:
        typer.echo("repository: (not inside a Git repository)")
        return
    ticket = detect_ticket(info.branch, cfg.ticket_pattern)
    typer.echo(f"repository: {info.name} ({info.root})")
    typer.echo(f"branch: {info.branch or '(detached HEAD)'}   ticket: {ticket or 'none detected'}")
    if not ticket or not (md / ".engineering-memory.yaml").exists():
        return
    proj = resolve_project(md, repo_name=info.name, remote=info.remote, ticket=ticket)
    typer.echo(f"project: {proj.slug if proj else 'not registered (memory project add ...)'}")
    tm = load_ticket(md, ticket)
    if not tm:
        typer.echo(f"memory: none yet -> run `memory start {ticket}`")
        return
    typer.echo(f"memory: {tm.rel(md)}/MEMORY.md  [{tm.meta.get('status')}] updated {tm.meta.get('updated')}")
    try:
        act, _ = restrict_to_ticket(collect_activity(Path.cwd(), cfg, ticket=ticket))
        known = set(tm.meta.get("commits") or [])
        new = [c for c in act.commits if c.hash not in known]
        typer.echo(f"uncaptured commits: {len(new)}" + (" -> run `memory capture`" if new else ""))
    except GitError:
        pass


# ---------------------------------------------------------------- export / commit / install
@app.command("export")
@guarded
def export_cmd(
    ctx: typer.Context,
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Archive path (.zip or .tar.gz)."),
    include_git: bool = typer.Option(False, "--include-git", help="Include the .git directory (full history)."),
) -> None:
    """Package the memory directory into a portable archive (derived indexes/locks excluded)."""
    _, md = _env(ctx)
    out = output or Path.cwd() / f"engineering-memory-{datetime.now():%Y%m%d}.zip"
    out = out.resolve()
    skip_names = {".em.lock", FTS_FILE, FTS_FILE + "-journal", FTS_FILE + ".tmp"}
    files = []
    for p in sorted(md.rglob("*")):
        rel = p.relative_to(md)
        if p.is_dir() or p == out or rel.name in skip_names or rel.name.endswith(".tmp"):
            continue
        if rel.parts and rel.parts[0] == ".git" and not include_git:
            continue
        files.append((p, rel))
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(out, "w:gz") as tf:
            for p, rel in files:
                tf.add(p, arcname=f"engineering-memory-data/{rel.as_posix()}")
    else:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for p, rel in files:
                zf.write(p, f"engineering-memory-data/{rel.as_posix()}")
    ok(f"Exported {len(files)} file(s) to {out}")


@app.command()
@guarded
def commit(ctx: typer.Context, message: str = typer.Option("memory: update", "--message", "-m")) -> None:
    """Commit the memory directory with Git (version history and recovery)."""
    import subprocess

    _, md = _env(ctx)
    if not (md / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=md, check=True)
    subprocess.run(["git", "add", "-A"], cwd=md, check=True)
    if not subprocess.run(["git", "status", "--porcelain"], cwd=md, capture_output=True, text=True).stdout.strip():
        typer.echo("Nothing to commit.")
        return
    proc = subprocess.run(["git", "commit", "-q", "-m", message], cwd=md, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MemoryError_(f"git commit failed: {proc.stderr.strip() or proc.stdout.strip()}")
    ok("Committed memory changes.")


_BLOCK = re.compile(r"<!-- engineering-memory:start -->.*?<!-- engineering-memory:end -->\n?", re.S)


@app.command("install-instructions")
@guarded
def install_instructions(
    agent: str = typer.Argument(..., help="claude | cursor | codex | generic"),
    target: Path = typer.Option(Path("."), "--target", help="Repository (or directory) to install into."),
    memory_dir: Optional[Path] = typer.Option(None, "--memory-path", help="Absolute path to your memory directory to embed."),
) -> None:
    """Install agent instructions into a repo so the agent uses the memory (idempotent)."""
    names = {
        "claude": ("CLAUDE.md", "CLAUDE.md"),
        "cursor": (".cursor/rules/engineering-memory.mdc", "CURSOR.md"),
        "codex": ("AGENTS.md", "CODEX.md"),
        "generic": ("ENGINEERING_MEMORY.md", "GENERIC_AGENT.md"),
    }
    if agent not in names:
        raise MemoryError_(f"agent must be one of: {', '.join(names)}")
    dest_rel, src = names[agent]
    cfg = load_config()
    mem_path = (memory_dir or cfg.resolved_memory_dir()).expanduser().resolve()
    body = (instructions_dir() / src).read_text(encoding="utf-8").replace("{{MEMORY_DIR}}", str(mem_path))
    dest = (target / dest_rel).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    block = f"<!-- engineering-memory:start -->\n{body.rstrip()}\n<!-- engineering-memory:end -->\n"
    if agent == "cursor":
        content = "---\ndescription: Use Engineering Memory for ticket history\nalwaysApply: true\n---\n" + block
    elif dest.exists():
        old = dest.read_text(encoding="utf-8")
        content = _BLOCK.sub(block, old) if _BLOCK.search(old) else old.rstrip("\n") + "\n\n" + block
    else:
        content = block
    from .store import atomic_write

    atomic_write(dest, content)
    ok(f"Installed {agent} instructions into {dest}")


if __name__ == "__main__":  # pragma: no cover
    app()

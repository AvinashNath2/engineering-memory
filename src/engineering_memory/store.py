"""Low-level, safe file access: frontmatter, atomic writes, locking, conflict detection."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

_FM = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)


class StoreError(Exception):
    pass


class ConflictError(StoreError):
    """The file changed on disk between read and write."""


class FrontmatterError(StoreError):
    pass


# ---- time ---------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(now_utc())


def parse_ts(value: Any) -> datetime:
    """Parse ISO-8601 (with Z or offset) or a plain date into an aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    elif hasattr(value, "year") and hasattr(value, "month") and not isinstance(value, str):
        dt = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---- frontmatter --------------------------------------------------------
def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = _FM.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise FrontmatterError("frontmatter must be a mapping")
    return meta, text[m.end():]


def dump_frontmatter(meta: dict[str, Any], body: str) -> str:
    dumped = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100)
    return f"---\n{dumped}---\n{body.lstrip(chr(10)) if body else ''}"


# ---- hashing / files ----------------------------------------------------
def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str | None:
    try:
        return sha1(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def atomic_write(path: Path, content: str) -> None:
    """Write via temp file + fsync + rename so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_if_unchanged(path: Path, content: str, expected_hash: str | None) -> None:
    """Conflict protection: refuse to write if the file changed since it was read."""
    current = file_hash(path)
    if current != expected_hash:
        raise ConflictError(
            f"{path} was modified by someone else while updating; re-run the command."
        )
    atomic_write(path, content)


@contextlib.contextmanager
def memory_lock(memory_dir: Path, timeout: float = 10.0, stale_after: float = 120.0) -> Iterator[None]:
    """Cross-process advisory lock (exclusive file creation) with stale-lock recovery."""
    lock = memory_dir / ".em.lock"
    memory_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time()}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_after:
                    lock.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise StoreError(f"Memory is locked by another process ({lock}).")
            time.sleep(0.05)
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock.unlink()


# ---- misc ---------------------------------------------------------------
def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "unknown"


def template_dir() -> Path:
    return Path(__file__).parent / "templates"


def instructions_dir() -> Path:
    return Path(__file__).parent / "instructions"

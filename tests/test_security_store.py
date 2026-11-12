from __future__ import annotations

import os
import time

import pytest

from engineering_memory.security import find_secrets, is_sensitive_path, redact_secrets, sanitize_url
from engineering_memory.store import (
    ConflictError,
    FrontmatterError,
    StoreError,
    atomic_write,
    dump_frontmatter,
    file_hash,
    memory_lock,
    parse_frontmatter,
    parse_ts,
    write_if_unchanged,
)


# ---- secrets ------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "AKIAIOSFODNN7EXAMPLE",
        "token ghp_" + "a" * 36,
        "xoxb-1234567890-abcdefghij",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
        "postgres://admin:hunter2pass@db.internal/app",
        "password = 'correcthorsebattery'",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_redacts_known_secret_shapes(text):
    clean, n = redact_secrets(text)
    assert n >= 1
    assert "[REDACTED" in clean
    assert "hunter2pass" not in clean and "correcthorsebattery" not in clean and "AKIAIOSFODNN7EXAMPLE" not in clean


def test_plain_text_is_untouched():
    text = "We chose exponential backoff. The token bucket limits rate."
    assert redact_secrets(text) == (text, 0)


def test_placeholders_are_not_secrets():
    text = "password: ${DB_PASSWORD}\nsecret: <your-secret>\napi_key = xxxxxxxx\ntoken: [REDACTED:secret]"
    assert redact_secrets(text)[1] == 0
    assert find_secrets(text) == []


def test_find_secrets_reports_severity_and_line():
    found = find_secrets("line one\nAKIAIOSFODNN7EXAMPLE\npassword = 'abcdefgh1'")
    kinds = {(f.kind, f.severity, f.line) for f in found}
    assert ("aws-access-key", "error", 2) in kinds
    assert ("secret-assignment", "warning", 3) in kinds


@pytest.mark.parametrize(
    "path",
    [".env", "config/.env.production", "certs/server.pem", "id_rsa", "keys/deploy.key", ".npmrc",
     "infra/terraform.tfvars", "secrets.yaml", ".aws/credentials", "a/b/service-account-prod.json"],
)
def test_sensitive_paths_detected(path):
    assert is_sensitive_path(path)


@pytest.mark.parametrize("path", ["src/app.py", "README.md", "docs/environment.md", "src/keyboard.py"])
def test_normal_paths_allowed(path):
    assert not is_sensitive_path(path)


def test_extra_sensitive_patterns():
    assert is_sensitive_path("config/internal.cfg", ["*.cfg"])


def test_sanitize_url_strips_credentials():
    assert sanitize_url("https://user:tok@github.com/a/b.git") == "https://github.com/a/b.git"
    assert sanitize_url("git@github.com:a/b.git") == "git@github.com:a/b.git"


# ---- store --------------------------------------------------------------
def test_frontmatter_roundtrip():
    meta = {"ticket": "PAY-1", "created": "2026-11-07T10:00:00Z", "list": ["a", "b"], "nested": {"k": "v"}}
    text = dump_frontmatter(meta, "# Body\n")
    parsed, body = parse_frontmatter(text)
    assert parsed == meta
    assert body == "# Body\n"


def test_frontmatter_invalid_yaml_raises():
    with pytest.raises(FrontmatterError):
        parse_frontmatter("---\nkey: [unclosed\n---\nbody")


def test_no_frontmatter_is_fine():
    assert parse_frontmatter("# just text\n") == ({}, "# just text\n")


def test_atomic_write_creates_dirs_and_leaves_no_temp_files(tmp_path):
    target = tmp_path / "a" / "b" / "file.md"
    atomic_write(target, "hello")
    assert target.read_text() == "hello\n"
    assert [p.name for p in target.parent.iterdir()] == ["file.md"]


def test_atomic_write_failure_keeps_original(tmp_path, monkeypatch):
    target = tmp_path / "f.md"
    atomic_write(target, "original")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(target, "new")
    monkeypatch.undo()
    assert target.read_text() == "original\n"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_write_if_unchanged_detects_conflict(tmp_path):
    f = tmp_path / "x.md"
    atomic_write(f, "v1")
    h = file_hash(f)
    atomic_write(f, "edited by someone else")
    with pytest.raises(ConflictError):
        write_if_unchanged(f, "v2", h)
    assert f.read_text() == "edited by someone else\n"


def test_lock_is_exclusive_and_released(tmp_path):
    with memory_lock(tmp_path):
        with pytest.raises(StoreError):
            with memory_lock(tmp_path, timeout=0.2):
                pass
    with memory_lock(tmp_path, timeout=0.2):  # released
        pass


def test_stale_lock_is_recovered(tmp_path):
    lock = tmp_path / ".em.lock"
    lock.write_text("999 0")
    old = time.time() - 10_000
    os.utime(lock, (old, old))
    with memory_lock(tmp_path, timeout=1):
        pass


def test_parse_ts_variants():
    assert parse_ts("2026-11-07T10:00:00Z").hour == 10
    assert parse_ts("2026-11-07T10:00:00+05:30").hour == 4
    assert parse_ts("2026-11-07").day == 7
    with pytest.raises(ValueError):
        parse_ts("not a date")

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from engineering_memory.memory.creator import add_project, create_ticket, init_memory_dir


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test gets its own config file and memory dir; the real home is never touched."""
    monkeypatch.setenv("EM_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("EM_MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (tmp_path / "gitconfig").write_text("[user]\n\tname = Test Dev\n\temail = dev@example.com\n[init]\n\tdefaultBranch = main\n")
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def memdir(tmp_path) -> Path:
    path, _ = init_memory_dir(tmp_path / "mem", git=False)
    return path


@pytest.fixture
def project(memdir) -> str:
    add_project(memdir, "payment-service", repo_name="payment-service", ticket_prefixes=["PAY"])
    return "payment-service"


@pytest.fixture
def ticket(memdir, project) -> str:
    create_ticket(memdir, project, "PAY-1234", title="Payment retry")
    return "PAY-1234"


@pytest.fixture
def repo(tmp_path) -> Path:
    """A real temporary Git repo: main with one commit, plus a PAY-1234 feature branch with two."""
    r = tmp_path / "payment-service"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("print('hi')\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "initial commit")
    git(r, "checkout", "-qb", "feature/pay-1234-payment-retry")
    (r / "src").mkdir()
    (r / "src" / "retry.py").write_text("def retry(): ...\n")
    (r / ".env").write_text("DB_PASSWORD=supersecret123\n")
    (r / "deploy.pem").write_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n")
    git(r, "add", "-f", ".")
    git(r, "commit", "-qm", "PAY-1234: add retry with backoff (#42)")
    (r / "src" / "retry.py").write_text("def retry(n=5): ...\n")
    git(r, "commit", "-qam", "PAY-1234: cap attempts")
    return r

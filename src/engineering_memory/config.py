"""Machine-local configuration (YAML). The memory itself never depends on this file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

DEFAULT_TICKET_PATTERN = r"[A-Za-z][A-Za-z0-9]+-\d+"
DEFAULT_BASE_BRANCHES = ["main", "master", "develop", "origin/main", "origin/master", "origin/develop"]


class ConfigError(Exception):
    pass


def config_path() -> Path:
    env = os.environ.get("EM_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "engineering-memory" / "config.yaml"


@dataclass
class AIConfig:
    provider: str = "none"  # none | command
    command: str = ""
    timeout_seconds: int = 300


@dataclass
class SearchConfig:
    use_fts: bool = True


@dataclass
class Config:
    memory_dir: str = "~/engineering-memory-data"
    ticket_pattern: str = DEFAULT_TICKET_PATTERN
    base_branches: list[str] = field(default_factory=lambda: list(DEFAULT_BASE_BRANCHES))
    max_commits: int = 50
    extra_sensitive_patterns: list[str] = field(default_factory=list)
    ai: AIConfig = field(default_factory=AIConfig)
    search: SearchConfig = field(default_factory=SearchConfig)

    # ---- helpers -------------------------------------------------------
    def resolved_memory_dir(self, override: str | Path | None = None) -> Path:
        env = os.environ.get("EM_MEMORY_DIR")
        raw = override or env or self.memory_dir
        return Path(str(raw)).expanduser().resolve()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _from_dict(data: dict[str, Any]) -> Config:
    cfg = Config()
    for key in ("memory_dir", "ticket_pattern", "max_commits"):
        if key in data and data[key] is not None:
            setattr(cfg, key, data[key])
    for key in ("base_branches", "extra_sensitive_patterns"):
        if key in data and data[key] is not None:
            setattr(cfg, key, list(data[key]))
    if isinstance(data.get("ai"), dict):
        for k, v in data["ai"].items():
            if hasattr(cfg.ai, k):
                setattr(cfg.ai, k, v)
    if isinstance(data.get("search"), dict):
        for k, v in data["search"].items():
            if hasattr(cfg.search, k):
                setattr(cfg.search, k, v)
    return cfg


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return _from_dict(data)


def save_config(cfg: Config, path: Path | None = None) -> Path:
    from .store import atomic_write

    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, yaml.safe_dump(cfg.to_dict(), sort_keys=False, allow_unicode=True))
    return path


def set_value(cfg: Config, dotted_key: str, value: str) -> None:
    """Set a config value from the CLI (`memory config --set ai.provider=command`)."""
    parts = dotted_key.split(".")
    target: Any = cfg
    for part in parts[:-1]:
        if not hasattr(target, part):
            raise ConfigError(f"Unknown config key: {dotted_key}")
        target = getattr(target, part)
    leaf = parts[-1]
    if not hasattr(target, leaf):
        raise ConfigError(f"Unknown config key: {dotted_key}")
    current = getattr(target, leaf)
    if isinstance(current, bool):
        coerced: Any = value.strip().lower() in {"1", "true", "yes", "on"}
    elif isinstance(current, int):
        try:
            coerced = int(value)
        except ValueError as exc:
            raise ConfigError(f"{dotted_key} expects an integer") from exc
    elif isinstance(current, list):
        coerced = [v.strip() for v in value.split(",") if v.strip()]
    else:
        coerced = value
    setattr(target, leaf, coerced)

"""Provider-independent AI adapter.

The memory engine never imports a vendor SDK. An AI is optional and is reached through
``AIProvider.complete(prompt) -> str``. Shipped providers:

* ``none``    - AI disabled; the calling agent (Claude Code, Cursor, ...) writes memory itself
* ``command`` - run any local command, prompt on stdin, answer on stdout (e.g. ``claude -p``)
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import Config
from ..memory.entries import CONFIDENCE
from ..memory.updater import SECTIONS, EntryRequest, LinkRequest, PersonRequest


class ProviderError(Exception):
    pass


class AIProvider(Protocol):
    def complete(self, prompt: str) -> str: ...


class NullProvider:
    def complete(self, prompt: str) -> str:
        raise ProviderError(
            "No AI provider configured. Either let your coding agent run `memory update` itself, or configure one:\n"
            "  memory config --set ai.provider=command --set ai.command='claude -p'"
        )


@dataclass
class CommandProvider:
    command: str
    timeout: int = 300

    def complete(self, prompt: str) -> str:
        if not self.command.strip():
            raise ProviderError("ai.command is empty")
        try:
            proc = subprocess.run(
                shlex.split(self.command), input=prompt, capture_output=True, text=True, timeout=self.timeout
            )
        except FileNotFoundError as exc:
            raise ProviderError(f"AI command not found: {self.command!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"AI command timed out after {self.timeout}s") from exc
        if proc.returncode != 0:
            raise ProviderError(f"AI command failed ({proc.returncode}): {proc.stderr.strip()[:500]}")
        return proc.stdout


def get_provider(cfg: Config) -> AIProvider:
    if cfg.ai.provider == "command":
        return CommandProvider(cfg.ai.command, cfg.ai.timeout_seconds)
    return NullProvider()


# ---- response handling --------------------------------------------------
def extract_json(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ProviderError("AI response contained no JSON object")
        candidate = text[start:end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"AI response was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProviderError("AI response JSON must be an object")
    return data


def to_requests(data: dict[str, Any]) -> tuple[list[EntryRequest], list[PersonRequest], list[LinkRequest], dict[str, str], list[str]]:
    """Validate an AI answer. Unverifiable claims are downgraded, never trusted."""
    warnings: list[str] = []
    entries: list[EntryRequest] = []
    for item in data.get("entries") or []:
        if not isinstance(item, dict):
            continue
        section, text = str(item.get("section", "")), str(item.get("text", "")).strip()
        if section not in SECTIONS or section == "sources" or not text:
            warnings.append(f"dropped entry with bad section/text: {section!r}")
            continue
        conf = str(item.get("confidence", "unconfirmed")).lower()
        if conf not in CONFIDENCE:
            conf = "unconfirmed"
        source = (str(item["source"]).strip() if item.get("source") else None)
        if conf == "verified" and not source:
            conf = "unconfirmed"
            warnings.append(f"downgraded to unconfirmed (no source): {text[:60]}")
        entries.append(EntryRequest(section, text, conf, source))
    people = []
    for p in data.get("people") or []:
        if isinstance(p, dict) and p.get("name"):
            src = str(p["source"]) if p.get("source") else None
            people.append(PersonRequest(str(p["name"]), str(p.get("role") or "unknown"), src, None))
    links = [
        LinkRequest(str(x["url"]), str(x.get("label", "")), str(x.get("kind", "docs")))
        for x in data.get("links") or []
        if isinstance(x, dict) and x.get("url")
    ]
    set_meta = {k: str(v) for k, v in (data.get("set") or {}).items() if k in ("title", "status", "type")}
    return entries, people, links, set_meta, warnings

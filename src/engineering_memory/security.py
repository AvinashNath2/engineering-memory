"""Privacy and safety: secret redaction and sensitive-path exclusion.

Everything that is written into the memory passes through ``redact_secrets``.
Everything captured from Git passes through ``is_sensitive_path``.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

# (kind, compiled regex, severity). "error" = near-certain secret; "warning" = heuristic.
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S
)
_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("private-key", _PRIVATE_KEY, "error"),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "error"),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"), "error"),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}\b"), "error"),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"), "error"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), "error"),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{20,}"), "error"),
    ("url-credentials", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"), "error"),
]
_GENERIC = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|private[_-]?key|token)\b(\s*[:=]\s*)(['\"]?)([^\s'\"]{6,})"
)
_PLACEHOLDER = re.compile(r"^(\[REDACTED|<.*>|\$\{.*\}|\$[A-Z_]+|x{3,}|\*{3,}|\.{3,}|null|none|true|false)", re.I)

SENSITIVE_PATH_PATTERNS = [
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa", "id_rsa.*", "id_dsa", "id_ecdsa", "id_ed25519", "*.ppk",
    ".npmrc", ".pypirc", ".netrc", ".htpasswd", ".git-credentials",
    "credentials", "credentials.*", "secrets", "secrets.*", "*.secret", "*.secrets",
    "secret.*", "service-account*.json", "*serviceaccount*.json",
    "terraform.tfvars", "*.tfstate", "*.tfstate.backup",
    ".aws/*", ".ssh/*", ".gnupg/*", ".docker/config.json",
]


@dataclass(frozen=True)
class SecretFinding:
    kind: str
    severity: str  # "error" | "warning"
    line: int


def redact_secrets(text: str) -> tuple[str, int]:
    """Replace secrets with ``[REDACTED:<kind>]``. Returns (clean_text, count)."""
    count = 0

    def sub_factory(kind: str):
        def _sub(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            return f"[REDACTED:{kind}]"

        return _sub

    for kind, pattern, _sev in _PATTERNS:
        text = pattern.sub(sub_factory(kind), text)

    def _generic(match: re.Match[str]) -> str:
        nonlocal count
        value = match.group(4)
        if _PLACEHOLDER.match(value):
            return match.group(0)
        count += 1
        return f"{match.group(1)}{match.group(2)}{match.group(3)}[REDACTED:secret]"

    text = _GENERIC.sub(_generic, text)
    return text, count


def find_secrets(text: str) -> list[SecretFinding]:
    """Locate (without modifying) probable secrets, for the validator."""
    findings: list[SecretFinding] = []
    for kind, pattern, sev in _PATTERNS:
        for m in pattern.finditer(text):
            if m.group(0).startswith("[REDACTED"):
                continue
            findings.append(SecretFinding(kind, sev, text.count("\n", 0, m.start()) + 1))
    for m in _GENERIC.finditer(text):
        if _PLACEHOLDER.match(m.group(4)):
            continue
        findings.append(SecretFinding("secret-assignment", "warning", text.count("\n", 0, m.start()) + 1))
    return findings


def is_sensitive_path(path: str, extra_patterns: list[str] | None = None) -> bool:
    """True if a repository path must never be captured (names, directories)."""
    patterns = list(SENSITIVE_PATH_PATTERNS) + list(extra_patterns or [])
    posix = PurePosixPath(path.replace("\\", "/"))
    name = posix.name.lower()
    parts = [p.lower() for p in posix.parts]
    for pat in patterns:
        pat_l = pat.lower()
        if fnmatch.fnmatch(name, pat_l) or fnmatch.fnmatch(str(posix).lower(), pat_l):
            return True
        if "/" in pat_l:
            # directory style pattern like ".aws/*" matches anywhere in the path
            head = pat_l.split("/")[0]
            if head in parts[:-1]:
                return True
    return False


def sanitize_url(url: str) -> str:
    """Strip embedded credentials from a URL (https://user:token@host/x -> https://host/x)."""
    return re.sub(r"(?<=://)[^/\s@]+@", "", url)

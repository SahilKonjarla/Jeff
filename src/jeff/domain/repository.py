"""Repository metadata values and conservative path-disclosure policy."""

import json
from dataclasses import dataclass
from fnmatch import fnmatchcase

# Metadata policy, not .gitignore rules. Even environment templates stay hidden.
PROTECTED_PATTERNS = (
    ".env",
    ".env.*",
    "jeff.local.yaml",
    ".git",
    ".jeff",
    ".ssh",
    ".aws",
    ".azure",
    ".kube",
    ".gnupg",
    "secrets",
    ".secrets",
    "credentials",
    ".credentials",
    "secrets.*",
    "credentials.*",
    "id_rsa*",
    "id_ed25519*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
)


def protected_path(path: str) -> bool:
    """Reject protected names at any depth, without touching the filesystem."""
    return any(
        fnmatchcase(part.casefold(), pattern)
        for part in path.split("/")
        for pattern in PROTECTED_PATTERNS
    )


def git_exclusions() -> tuple[str, ...]:
    """Apply the same policy before status scans, not only after parsing."""
    return tuple(
        f":(glob,icase,exclude)**/{pattern}{suffix}"
        for pattern in PROTECTED_PATTERNS
        for suffix in ("", "/**")
    )


def escape_metadata(value: str) -> str:
    """Reversible ASCII/JSON string-body encoding for untrusted display metadata."""
    return json.dumps(value, ensure_ascii=True)[1:-1]


@dataclass(frozen=True)
class BranchState:
    name: str | None
    commit: str | None
    detached: bool
    unborn: bool


@dataclass(frozen=True)
class FileChange:
    path: str
    original_path: str | None
    index_status: str
    worktree_status: str
    untracked: bool = False
    conflicted: bool = False

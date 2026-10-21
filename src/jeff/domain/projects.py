"""Project identity independent of filesystem or framework behavior."""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


class ProjectError(Exception):
    """An actionable, public-safe error; never include raw adapter output."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Project:
    """Stable identity bound to an absolute canonical repository root."""

    project_id: UUID
    root: Path

    def __post_init__(self) -> None:
        if not self.root.is_absolute():
            raise ProjectError("invalid_root", "The project root must be an absolute path.")

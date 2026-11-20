"""Pure trusted path inputs, layout filenames and fixed metadata observation roles."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, StrictInt, field_validator, model_validator

from .. import runtime as _runtime
from .base import _RuntimeModel
from .failures import RuntimeIssueCode

RuntimePathRole = Literal[
    "data_dir",
    "runtime_dir",
    "database",
    "settings",
    "credential",
    "socket",
    "lock",
]


RUNTIME_PATH_ROLES: tuple[RuntimePathRole, ...] = (
    "data_dir",
    "runtime_dir",
    "database",
    "settings",
    "credential",
    "socket",
    "lock",
)


@dataclass(frozen=True)
class RuntimePathInputs:
    """Trusted host inputs supplied explicitly, not client authority or an env dump."""

    platform: str
    home: Path
    temp_dir: Path
    uid: int
    data_dir: str | None = None
    runtime_dir: str | None = None


class RuntimeLayout(_RuntimeModel):
    data_dir: Path
    runtime_dir: Path
    uid: StrictInt = Field(ge=0)

    @field_validator("data_dir", "runtime_dir")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        text = str(value)
        if not value.is_absolute() or "\0" in text or ".." in value.parts or text.startswith("//"):
            raise ValueError("Runtime locations must be valid absolute paths.")
        try:
            text.encode("utf-8")
        except UnicodeError:
            raise ValueError("Runtime locations require valid UTF-8 paths.") from None
        return value

    @property
    def database(self) -> Path:
        return self.data_dir / "state.sqlite3"

    @property
    def settings(self) -> Path:
        return self.data_dir / "settings.yaml"

    @property
    def credential(self) -> Path:
        return self.data_dir / "ipc.key"

    @property
    def socket(self) -> Path:
        return self.runtime_dir / "daemon.sock"

    @property
    def lock(self) -> Path:
        return self.runtime_dir / "daemon.lock"

    @property
    def endpoint_receipt(self) -> Path:
        return self.runtime_dir / "endpoint.json"

    @property
    def recovery_permit(self) -> Path:
        return self.data_dir / "recovery.json"


class PathObservation(_RuntimeModel):
    role: RuntimePathRole
    state: Literal["missing", "present", "unsafe", "unknown"]
    issue: RuntimeIssueCode | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.state in ("unsafe", "unknown")) != (self.issue is not None):
            raise ValueError("Only unsafe or unknown observations require an issue.")
        return self


class RuntimePathInspection(_RuntimeModel):
    entries: tuple[PathObservation, ...] = Field(min_length=7, max_length=7)

    @model_validator(mode="after")
    def fixed_roles(self) -> Self:
        if {entry.role for entry in self.entries} != set(_runtime.RUNTIME_PATH_ROLES):
            raise ValueError("Inspection requires each declared role exactly once.")
        return self

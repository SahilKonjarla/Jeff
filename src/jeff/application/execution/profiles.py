"""Declared project-bound execution profiles and settings."""

from pathlib import Path, PurePosixPath
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictInt, field_validator, model_validator

from jeff.domain.execution import fingerprint, public_text

from ._model import _Model, absolute_path


class ExecutionProfile(_Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    project_id: UUID
    canonical_root: Path
    environment: Literal["development"] = "development"
    engine_id: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=8, max_length=1023)
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    platform: Literal["linux/arm64", "linux/amd64"]
    toolchain: str = Field(min_length=1, max_length=128)
    executable: str = Field(min_length=1, max_length=255)
    container_cwd: Literal["/project"] = "/project"
    project_view: Literal["sanitized_read_only"] = "sanitized_read_only"
    scratch_target: Literal["/scratch"] = "/scratch"
    network: Literal["none"] = "none"
    user: Literal["65532:65532"] = "65532:65532"
    root_filesystem: Literal["read_only"] = "read_only"
    privileges: Literal["drop_all_no_new_privileges"] = "drop_all_no_new_privileges"
    log_driver: Literal["none"] = "none"
    cpu_millis: StrictInt = Field(default=1000, ge=1, le=1000)
    memory_bytes: StrictInt = Field(default=268435456, ge=16777216, le=268435456)
    pids: StrictInt = Field(default=64, ge=2, le=64)
    scratch_bytes: StrictInt = Field(default=67108864, ge=4096, le=67108864)
    timeout_seconds: StrictInt = Field(default=300, ge=1, le=3600)
    output_bytes: StrictInt = Field(default=1048576, ge=1, le=1048576)
    public_environment: tuple[tuple[str, str], ...] = ()

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

    @field_validator("engine_id", "toolchain", "executable", "endpoint")
    @classmethod
    def public(cls, value: str) -> str:
        return public_text(value, 1023)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if not self.endpoint.startswith("unix:///"):
            raise ValueError("Only an explicitly declared local Unix endpoint is supported.")
        absolute_path(Path(self.endpoint[7:]))
        executable = PurePosixPath(self.executable)
        if (
            not executable.is_absolute()
            or ".." in executable.parts
            or self.executable.startswith("//")
        ):
            raise ValueError("The declared executable must be an absolute container path.")
        allowed = {
            "LANG": {"C", "C.UTF-8", "en_US.UTF-8"},
            "LC_ALL": {"C", "C.UTF-8"},
            "PYTHONDONTWRITEBYTECODE": {"1"},
        }
        keys = [key for key, _ in self.public_environment]
        if len(keys) != len(set(keys)) or any(
            key not in allowed or value not in allowed[key]
            for key, value in self.public_environment
        ):
            raise ValueError("Only fixed public environment values are supported.")
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class ExecutionSettings(_Model):
    profiles: tuple[ExecutionProfile, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def unique(self) -> Self:
        identities = [(p.project_id, p.name) for p in self.profiles]
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicate project/profile binding.")
        return self

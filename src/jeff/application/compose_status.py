"""Read-only Compose target declaration and public observation contract."""

from pathlib import Path
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from jeff.application.execution import DockerCLIIdentity
from jeff.application.projects import ErrorData, Failure
from jeff.domain.projects import Project

ComposeName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")]
ContainerState = Literal["created", "running", "paused", "restarting", "removing", "exited", "dead"]


class _ComposeModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class ComposeStatusTarget(_ComposeModel):
    """Explicit installation-private target; independent of execution enablement."""

    project_id: UUID
    root: Path
    project: ComposeName
    service: ComposeName
    endpoint: str = Field(pattern=r"^unix:///[^\r\n\x00]+$", max_length=1023)
    binary: DockerCLIIdentity
    config: Path
    home: Path

    @field_validator("root", "config", "home")
    @classmethod
    def absolute(cls, path: Path) -> Path:
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("Compose inspection requires absolute paths without parent steps.")
        return path


class ComposeStatusData(_ComposeModel):
    compose_project: ComposeName
    service: ComposeName
    states: tuple[ContainerState, ...] = Field(max_length=64)
    ambiguous: bool


class ComposeStatusSuccess(_ComposeModel):
    ok: Literal[True] = True
    data: ComposeStatusData
    error: None = None


class ComposeStatusReader(Protocol):
    def __call__(self, project: Project, deadline: float) -> ComposeStatusSuccess | Failure: ...


def compose_status_failure(
    code: Literal[
        "compose_status_not_configured", "docker_unavailable", "compose_status_unconfirmed"
    ],
) -> Failure:
    messages = {
        "compose_status_not_configured": "No Compose status target is configured for this project.",
        "docker_unavailable": "Docker is unavailable at the declared endpoint.",
        "compose_status_unconfirmed": "Could not confirm Compose status at the declared endpoint.",
    }
    return Failure(error=ErrorData(code=code, message=messages[code]))

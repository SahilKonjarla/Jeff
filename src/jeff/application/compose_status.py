"""Read-only Compose target declaration and public observation contract."""

from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from jeff.application.execution import DockerCLIIdentity
from jeff.application.projects import ErrorData, Failure
from jeff.domain.projects import Project

ComposeName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")]
ContainerState = Literal["created", "running", "paused", "restarting", "removing", "exited", "dead"]
ComposeHealth = Literal["starting", "healthy", "unhealthy", "not_configured", "unknown"]
COMPOSE_LOG_LIMIT = 16_384
COMPOSE_LOG_TAIL = 40


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
    allow_logs: StrictBool = False

    @field_validator("root", "config", "home")
    @classmethod
    def absolute(cls, path: Path) -> Path:
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("Compose inspection requires absolute paths without parent steps.")
        return path


class PublishedPort(_ComposeModel):
    host_ip: str = Field(max_length=45)
    host_port: StrictInt = Field(ge=1, le=65535)
    container_port: StrictInt = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp", "sctp"]

    @field_validator("host_ip")
    @classmethod
    def canonical_ip(cls, value: str) -> str:
        if "%" in value:
            raise ValueError("Scoped addresses are not supported.")
        return str(ip_address(value))


PublishedPorts = Annotated[tuple[PublishedPort, ...], Field(max_length=64)]


class ComposeStatusData(_ComposeModel):
    compose_project: ComposeName
    service: ComposeName
    states: tuple[ContainerState, ...] = Field(max_length=64)
    ambiguous: bool
    # Older daemon replies may omit health; omission is not a readiness signal.
    health: tuple[ComposeHealth, ...] | None = Field(default=None, max_length=64)
    published_ports: tuple[PublishedPorts | None, ...] | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def aligned_observations(self) -> Self:
        if any(
            detail is not None and len(detail) != len(self.states)
            for detail in (self.health, self.published_ports)
        ):
            raise ValueError("Inspection details must align with container states.")
        return self


class ComposeStatusSuccess(_ComposeModel):
    ok: Literal[True] = True
    data: ComposeStatusData
    error: None = None


class ComposeStatusReader(Protocol):
    def __call__(self, project: Project, deadline: float) -> ComposeStatusSuccess | Failure: ...


class ComposeLogsData(_ComposeModel):
    compose_project: ComposeName
    service: ComposeName
    tail_lines: Literal[40] = COMPOSE_LOG_TAIL
    stdout: str = Field(default="", max_length=COMPOSE_LOG_LIMIT)
    stderr: str = Field(default="", max_length=COMPOSE_LOG_LIMIT)
    withheld: Literal["output_limit", "sanitizer"] | None = None

    @model_validator(mode="after")
    def public_output(self) -> Self:
        size = len(self.stdout) + len(self.stderr)
        if (
            size > COMPOSE_LOG_LIMIT
            or (self.withheld is not None and size)
            or any(
                char != "\n" and not 32 <= ord(char) <= 126
                for text in (self.stdout, self.stderr)
                for char in text
            )
        ):
            raise ValueError("Compose logs require bounded sanitized text or a withholding reason.")
        return self


class ComposeLogsSuccess(_ComposeModel):
    ok: Literal[True] = True
    data: ComposeLogsData
    error: None = None


class ComposeLogsReader(Protocol):
    def __call__(
        self, project: Project, service: str, deadline: float
    ) -> ComposeLogsSuccess | Failure: ...


def compose_logs_failure(
    code: Literal[
        "compose_logs_not_configured", "compose_logs_target_unconfirmed",
        "docker_unavailable", "compose_logs_unconfirmed",
    ],
) -> Failure:
    messages = {
        "compose_logs_not_configured": "Compose log reads are not enabled for this project and service.",
        "compose_logs_target_unconfirmed": "Compose logs require exactly one confirmed container.",
        "docker_unavailable": "Docker is unavailable at the declared endpoint.",
        "compose_logs_unconfirmed": "Could not confirm Compose logs at the declared endpoint.",
    }
    return Failure(error=ErrorData(code=code, message=messages[code]))


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

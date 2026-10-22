"""Pure runtime observations, not evidence of process ownership or authentication."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class RuntimeFault(ValueError):
    """Fixed public-safe failure; no supplied identity/path/payload is echoed."""

    def __init__(self, code: str) -> None:
        self.code = code
        message = (
            "Runtime identity must contain a UUID and a positive integer PID."
            if code == "invalid_runtime_identity"
            else "Runtime state and identity are inconsistent."
        )
        super().__init__(message)


class RuntimeState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RuntimeIdentity:
    instance_id: UUID
    pid: int

    def __post_init__(self) -> None:
        if not isinstance(self.instance_id, UUID) or type(self.pid) is not int or self.pid <= 0:
            raise RuntimeFault("invalid_runtime_identity")


@dataclass(frozen=True)
class RuntimeSnapshot:
    state: RuntimeState
    identity: RuntimeIdentity | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, RuntimeState) or (
            self.identity is not None and not isinstance(self.identity, RuntimeIdentity)
        ):
            raise RuntimeFault("invalid_runtime_state")
        if (self.state == RuntimeState.STOPPED and self.identity is not None) or (
            self.state in (RuntimeState.RUNNING, RuntimeState.STOPPING) and self.identity is None
        ):
            raise RuntimeFault("invalid_runtime_state")

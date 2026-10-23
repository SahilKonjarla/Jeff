"""Runtime value, location and metadata contracts; no lifecycle or IPC implementation."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, Self, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from jeff.application.projects import ErrorData, Failure
from jeff.application.storage import StorageIssueCode, StorageReadiness, storage_failure
from jeff.domain.runtime import RuntimeIdentity, RuntimeSnapshot, RuntimeState

PROTOCOL_REVISION = 3
MAC_SOCKET_PATH_BYTES = 103
MAC_PATH_BYTES = 1023  # Darwin PATH_MAX=1024, reserving the terminator.
MAC_NAME_BYTES = 255
RuntimeIssueCode = Literal[
    "invalid_runtime_path",
    "unsafe_runtime_path",
    "runtime_path_too_long",
    "runtime_inspection_failed",
    "unsupported_runtime_platform",
]
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
_MESSAGES: dict[RuntimeIssueCode, str] = {
    "invalid_runtime_path": "Runtime locations must be valid absolute paths with a valid owner.",
    "unsafe_runtime_path": "Runtime paths have unsafe links, types, ownership or permissions.",
    "runtime_path_too_long": "Runtime paths exceed platform byte limits; choose shorter locations.",
    "runtime_inspection_failed": "Runtime metadata is unconfirmed; no state was changed.",
    "unsupported_runtime_platform": "Runtime locations are not validated for this platform.",
}


def runtime_failure(code: RuntimeIssueCode) -> Failure:
    """Map only fixed public-safe descriptions, never raw validation/OS exceptions."""
    return Failure(error=ErrorData(code=code, message=_MESSAGES[code]))


class _RuntimeModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class RuntimeIdentityData(_RuntimeModel):
    instance_id: UUID
    pid: StrictInt = Field(gt=0)


class RuntimeStatusData(_RuntimeModel):
    state: RuntimeState
    identity: RuntimeIdentityData | None = None
    issue: RuntimeIssueCode | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        identity = (
            RuntimeIdentity(self.identity.instance_id, self.identity.pid) if self.identity else None
        )
        RuntimeSnapshot(self.state, identity)
        uncertain = self.state in (RuntimeState.UNKNOWN, RuntimeState.UNHEALTHY)
        if uncertain != (self.issue is not None):
            raise ValueError("Only uncertain or unhealthy status requires a bounded issue code.")
        return self


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
        if {entry.role for entry in self.entries} != set(RUNTIME_PATH_ROLES):
            raise ValueError("Inspection requires each declared role exactly once.")
        return self


class RuntimeLimits(_RuntimeModel):
    """Reviewed budgets only; future adapters must actually enforce them."""

    auth_bytes: StrictInt = Field(default=4096, gt=0, le=4294967295)
    request_bytes: StrictInt = Field(default=65536, gt=0, le=4294967295)
    response_bytes: StrictInt = Field(default=16777216, gt=0, le=4294967295)
    event_bytes: StrictInt = Field(default=65536, gt=0, le=4294967295)
    connections: StrictInt = Field(default=16, gt=0)
    active_requests: StrictInt = Field(default=4, gt=0)
    pending_requests: StrictInt = Field(default=16, gt=0)
    subscriptions: StrictInt = Field(default=8, gt=0)
    queued_events: StrictInt = Field(default=64, gt=0)
    auth_seconds: StrictInt = Field(default=5, gt=0)
    frame_seconds: StrictInt = Field(default=5, gt=0)
    request_seconds: StrictInt = Field(default=30, gt=0)
    sqlite_busy_seconds: StrictInt = Field(default=2, gt=0)
    start_seconds: StrictInt = Field(default=10, gt=0)
    stop_seconds: StrictInt = Field(default=35, gt=0)
    history_page_size: StrictInt = Field(default=50, gt=0)
    history_page_max: StrictInt = Field(default=100, gt=0)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if not (
            self.auth_bytes <= self.request_bytes <= self.response_bytes
            and self.event_bytes <= self.response_bytes
            and self.active_requests <= self.connections
            and self.subscriptions <= self.connections
            and self.history_page_size <= self.history_page_max
            and self.stop_seconds >= self.request_seconds
        ):
            raise ValueError("Runtime budgets are inconsistent.")
        return self


DEFAULT_RUNTIME_LIMITS = RuntimeLimits()


ProjectCapability = Literal["ask.branch", "ask.changes", "init", "config", "receipt.inspect"]


class ControlObservation(_RuntimeModel):
    """Authenticated control/storage/dispatcher facts, with coherent full readiness."""

    state: RuntimeState
    identity: RuntimeIdentityData | None = None
    control_ready: StrictBool = False
    application_ready: StrictBool = False
    available_operations: tuple[ProjectCapability, ...] = ()
    storage: StorageReadiness = Field(default_factory=StorageReadiness)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        identity = (
            RuntimeIdentity(self.identity.instance_id, self.identity.pid) if self.identity else None
        )
        RuntimeSnapshot(self.state, identity)
        if self.state not in (RuntimeState.RUNNING, RuntimeState.STOPPING, RuntimeState.STOPPED):
            raise ValueError("Unconfirmed lifecycle observations require a failure result.")
        if self.control_ready != (self.state == RuntimeState.RUNNING):
            raise ValueError("Only running control state may be control-ready.")
        if len(set(self.available_operations)) != len(self.available_operations):
            raise ValueError("Capabilities cannot be duplicated.")
        if self.application_ready and not (
            self.control_ready
            and self.storage.state == "ready"
            and set(self.available_operations)
            == {"ask.branch", "ask.changes", "init", "config", "receipt.inspect"}
        ):
            raise ValueError(
                "Full application readiness requires all supported handlers and storage."
            )
        if self.available_operations and (not self.control_ready or self.storage.state != "ready"):
            raise ValueError("Project operations require live control and storage.")
        if self.state == RuntimeState.STOPPED and self.storage.state != "not_checked":
            raise ValueError("Stopped status does not establish storage readiness.")
        return self


class ControlSuccess(_RuntimeModel):
    ok: Literal[True] = True
    data: ControlObservation
    error: None = None


class RuntimeControl(Protocol):
    def start(self) -> ControlSuccess | Failure: ...
    def status(self) -> ControlSuccess | Failure: ...
    def stop(self) -> ControlSuccess | Failure: ...


ControlIssueCode = (
    RuntimeIssueCode
    | StorageIssueCode
    | Literal[
        "runtime_absent",
        "daemon_not_running",
        "runtime_credentials_invalid",
        "runtime_ownership_unconfirmed",
        "runtime_endpoint_collision",
        "runtime_protocol_error",
        "runtime_protocol_incompatible",
        "runtime_authentication_failed",
        "runtime_handshake_unconfirmed",
        "runtime_peer_unverified",
        "runtime_unreachable",
        "runtime_busy",
        "runtime_start_failed",
        "runtime_start_timeout",
        "runtime_stop_unconfirmed",
    ]
)
_CONTROL_MESSAGES: dict[ControlIssueCode, str] = {
    code: message for code, message in _MESSAGES.items()
}
_CONTROL_MESSAGES.update(
    {
        "runtime_absent": "No control runtime ownership or endpoint was found.",
        "daemon_not_running": (
            "The daemon is stopped. Run jeff start explicitly; no action was executed."
        ),
        "runtime_credentials_invalid": "The private installation credential is missing or unsafe.",
        "runtime_ownership_unconfirmed": "Runtime ownership could not be established safely.",
        "runtime_endpoint_collision": "An existing endpoint cannot be adopted or removed safely.",
        "runtime_protocol_error": "The control message is invalid or exceeds its bounds.",
        "runtime_protocol_incompatible": "The control protocol revision is incompatible.",
        "runtime_authentication_failed": "Control authentication was not established.",
        "runtime_handshake_unconfirmed": (
            "Control handshake unconfirmed; compatibility/authentication was not established."
        ),
        "runtime_peer_unverified": "Native control peer identity could not be verified.",
        "runtime_unreachable": "The control runtime is not reachable.",
        "runtime_busy": "Control connection capacity is exhausted.",
        "runtime_start_failed": "The owned control child could not start safely.",
        "runtime_start_timeout": "Control readiness was not established within its deadline.",
        "runtime_stop_unconfirmed": "Runtime shutdown could not be confirmed within its bounds.",
    }
)


class ControlFault(Exception):
    """Fixed descriptions only, never credential/frame/OS exception content."""

    def __init__(self, code: ControlIssueCode) -> None:
        self.code = code
        super().__init__(control_failure(code).error.message)


def control_failure(code: ControlIssueCode) -> Failure:
    if code not in _CONTROL_MESSAGES:
        return storage_failure(cast(StorageIssueCode, code))
    return Failure(error=ErrorData(code=code, message=_CONTROL_MESSAGES[code]))


class AuthRequest(_RuntimeModel):
    revision: StrictInt = Field(ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION)
    token: SecretStr

    @field_validator("token", mode="before")
    @classmethod
    def credential_format(cls, value: object) -> SecretStr:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValueError("Invalid authentication format.")
        return SecretStr(value)


class AuthAccepted(_RuntimeModel):
    revision: StrictInt = Field(ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION)
    authenticated: StrictBool
    identity: RuntimeIdentityData | None = None
    error: Literal["runtime_authentication_failed", "runtime_protocol_incompatible"] | None = None

    @model_validator(mode="after")
    def accepted(self) -> Self:
        if self.authenticated != (self.identity is not None):
            raise ValueError("Only authenticated peers receive instance identity.")
        if self.authenticated != (self.error is None):
            raise ValueError("Authentication requires exactly one bounded outcome.")
        return self


ControlOperation = Literal["hello", "status", "stop"]


class ControlRequest(_RuntimeModel):
    kind: Literal["control"] = "control"
    revision: StrictInt = Field(ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION)
    request_id: UUID
    operation: ControlOperation
    expected_instance_id: UUID | None = None

    @model_validator(mode="after")
    def targeted_stop(self) -> Self:
        if self.operation == "stop" and self.expected_instance_id is None:
            raise ValueError("Shutdown must target an authenticated instance.")
        return self


class ControlResponse(_RuntimeModel):
    revision: StrictInt = Field(ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION)
    request_id: UUID
    observation: ControlObservation | None = None
    error: ControlIssueCode | None = None

    @model_validator(mode="after")
    def one_outcome(self) -> Self:
        if (self.observation is None) == (self.error is None):
            raise ValueError("Exactly one bounded control outcome is required.")
        return self


class StartupResult(_RuntimeModel):
    """Transient child diagnostic only; never authority for control readiness."""

    revision: StrictInt = Field(ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION)
    instance_id: UUID
    error: StorageIssueCode | None = None

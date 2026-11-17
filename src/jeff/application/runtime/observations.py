"""Runtime identity/status and authenticated readiness facts, not lifecycle authority."""

from typing import Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, model_validator

from jeff.application.projects import Failure
from jeff.application.storage import StorageReadiness
from jeff.domain.runtime import RuntimeState

from .. import runtime as _runtime
from .base import _RuntimeModel
from .failures import RuntimeIssueCode


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
            _runtime.RuntimeIdentity(self.identity.instance_id, self.identity.pid)
            if self.identity
            else None
        )
        _runtime.RuntimeSnapshot(self.state, identity)
        uncertain = self.state in (_runtime.RuntimeState.UNKNOWN, _runtime.RuntimeState.UNHEALTHY)
        if uncertain != (self.issue is not None):
            raise ValueError("Only uncertain or unhealthy status requires a bounded issue code.")
        return self


ProjectCapability = Literal[
    "ask.branch",
    "ask.changes",
    "init",
    "config",
    "receipt.inspect",
    "activity.trace",
    "activity.replay",
    "debug.last",
    "history.list",
    "activity.subscribe",
    "execution.review",
    "execution.v1",
    "execution.cancel.v1",
]


class ControlObservation(_RuntimeModel):
    """Authenticated control/storage/dispatcher facts, with coherent full readiness."""

    state: RuntimeState
    identity: RuntimeIdentityData | None = None
    control_ready: StrictBool = False
    execution_control_ready: StrictBool = False
    application_ready: StrictBool = False
    available_operations: tuple[ProjectCapability, ...] = ()
    storage: StorageReadiness = Field(default_factory=StorageReadiness)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        identity = (
            _runtime.RuntimeIdentity(self.identity.instance_id, self.identity.pid)
            if self.identity
            else None
        )
        _runtime.RuntimeSnapshot(self.state, identity)
        if self.state not in (
            _runtime.RuntimeState.RUNNING,
            _runtime.RuntimeState.STOPPING,
            _runtime.RuntimeState.STOPPED,
        ):
            raise ValueError("Unconfirmed lifecycle observations require a failure result.")
        if self.control_ready != (self.state == _runtime.RuntimeState.RUNNING):
            raise ValueError("Only running control state may be control-ready.")
        if len(set(self.available_operations)) != len(self.available_operations):
            raise ValueError("Capabilities cannot be duplicated.")
        if self.application_ready and not (
            self.control_ready
            and self.storage.state == "ready"
            and set(self.available_operations)
            >= {
                "ask.branch",
                "ask.changes",
                "init",
                "config",
                "receipt.inspect",
                "activity.trace",
                "activity.replay",
                "debug.last",
                "history.list",
            }
        ):
            raise ValueError(
                "Full application readiness requires all supported handlers and storage."
            )
        if self.available_operations and (not self.control_ready or self.storage.state != "ready"):
            raise ValueError("Project operations require live control and storage.")
        if self.state == _runtime.RuntimeState.STOPPED and self.storage.state != "not_checked":
            raise ValueError("Stopped status does not establish storage readiness.")
        return self


class ControlSuccess(_RuntimeModel):
    ok: Literal[True] = True
    data: ControlObservation
    error: None = None


class RuntimeControl(Protocol):
    """Explicit installation lifecycle; acknowledgement alone does not confirm exit."""

    def start(self) -> ControlSuccess | Failure: ...
    def status(self) -> ControlSuccess | Failure: ...
    def stop(self) -> ControlSuccess | Failure: ...

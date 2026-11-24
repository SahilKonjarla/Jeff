"""Finite project, execution, receipt and evidence request contracts."""

from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, StrictInt, model_validator

from jeff.application.compose_service_commands import ServiceCommand
from jeff.application.compose_status import ComposeName
from jeff.application.execution import ExecutionCall, ExecutionCancelCall, FreshExecutionCall
from jeff.application.runtime import PROTOCOL_REVISION, ProjectCapability

from .scope import ProjectScope, _DispatchModel

PROJECT_OPERATIONS: tuple[ProjectCapability, ...] = (
    "ask.branch",
    "ask.changes",
    "init",
    "config",
    "receipt.inspect",
    "activity.trace",
    "activity.replay",
    "debug.last",
    "history.list",
)


class ProjectCall(ProjectScope):
    request_id: UUID = Field(default_factory=uuid4)
    operation: Literal[
        "init", "config", "ask", "docker.status", "docker.logs", "process.ports", "process.list",
        "service.lifecycle",
    ]
    question: str | None = Field(default=None, max_length=65536)
    compose_service: ComposeName | None = Field(default=None, exclude_if=lambda v: v is None)
    service_command: ServiceCommand | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def question_only_for_ask(self) -> Self:
        if (self.operation == "ask") != (self.question is not None):
            raise ValueError("Only ask carries a transient question.")
        if (self.operation == "docker.logs") != (self.compose_service is not None):
            raise ValueError("Only Docker logs requires a service name.")
        if (self.operation == "service.lifecycle") != (self.service_command is not None):
            raise ValueError("Only service lifecycle carries a service operation.")
        return self


class ExecutionRequest(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    kind: Literal["execution"] = "execution"
    expected_instance_id: UUID
    remaining_ms: StrictInt = Field(default=30000, gt=0, le=30000)
    call: ExecutionCall


class FreshExecutionRequest(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    kind: Literal["execution.v1"] = "execution.v1"
    expected_instance_id: UUID
    remaining_ms: StrictInt = Field(default=30000, gt=0, le=30000)
    call: FreshExecutionCall


class ExecutionCancelRequest(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    kind: Literal["execution.cancel.v1"] = "execution.cancel.v1"
    expected_instance_id: UUID
    remaining_ms: StrictInt = Field(default=30000, gt=0, le=30000)
    call: ExecutionCancelCall


class ReceiptQuery(ProjectScope):
    query_id: UUID = Field(default_factory=uuid4)
    request_id: UUID


class EvidenceQuery(ProjectScope):
    query_id: UUID = Field(default_factory=uuid4)
    operation: Literal["trace", "replay", "debug", "history"]
    request_id: UUID | None = None
    after: StrictInt = Field(default=0, ge=0, le=9223372036854775807)
    limit: StrictInt = Field(default=64, gt=0, le=100)
    expected_epoch: UUID | None = None
    cursor: str | None = Field(default=None, max_length=8192)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.operation != "history" and self.cursor is not None:
            raise ValueError("Only history accepts a history cursor.")
        if self.operation == "history" and (
            self.request_id is not None or self.after != 0 or self.expected_epoch is not None
        ):
            raise ValueError("History accepts only a limit and history cursor.")
        if self.operation == "replay" and self.limit > 64:
            raise ValueError("Event replay remains bounded to 64 events.")
        if self.operation == "trace":
            if (
                self.request_id is None
                or self.after != 0
                or self.limit != 64
                or self.expected_epoch is not None
            ):
                raise ValueError("Trace accepts one scoped request ID only.")
        elif self.operation == "replay" and self.request_id is not None:
            raise ValueError("Replay uses an epoch/sequence, not a request ID.")
        if self.operation == "debug" and (
            self.after != 0 or self.limit != 64 or self.expected_epoch is not None
        ):
            raise ValueError("Debug accepts only an optional exact request ID.")
        return self


class DebugQuery(EvidenceQuery):
    operation: Literal["debug"] = "debug"


class HistoryQuery(EvidenceQuery):
    operation: Literal["history"] = "history"
    limit: StrictInt = Field(default=50, gt=0, le=100)


class EvidenceRequest(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    kind: Literal["evidence"] = "evidence"
    expected_instance_id: UUID
    remaining_ms: StrictInt = Field(default=30000, gt=0, le=30000)
    query: DebugQuery | HistoryQuery | EvidenceQuery


class ProjectRequest(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    kind: Literal["project"] = "project"
    expected_instance_id: UUID
    remaining_ms: StrictInt = Field(default=30000, gt=0, le=30000)
    call: ProjectCall


class ReceiptRequest(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    kind: Literal["receipt"] = "receipt"
    expected_instance_id: UUID
    remaining_ms: StrictInt = Field(default=30000, gt=0, le=30000)
    query: ReceiptQuery

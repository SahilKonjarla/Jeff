"""Fresh execution and cancellation replies with fixed safe public refusals."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, model_validator

from jeff.application.projects import ErrorData, Failure
from jeff.domain.execution import AuthorityState

from ._model import _Model
from .admission import ExecutionAdmission
from .output import DockerOutputFacts, DockerOutputSnapshot
from .requests import ExecutionLivePhase, FreshExecutionOperation
from .stop_views import ExecutionStopView
from .views import FreshExecutionCompletion, FreshExecutionView


class ExecutionCancelReply(_Model):
    request_id: UUID
    approval_id: UUID
    reviewed_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity: UUID | None = None
    ok: StrictBool
    stop: ExecutionStopView | None = None
    error: Failure | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.ok != (self.error is None) or self.ok != (self.stop is not None):
            raise ValueError("Cancellation accepts durable intent only, not invented termination.")
        if self.ok and self.identity is None:
            raise ValueError("Accepted stop requires authenticated instance correlation.")
        return self


class FreshExecutionReply(_Model):
    authority_kind: Literal["execution_v1"] = "execution_v1"
    request_id: UUID
    operation: FreshExecutionOperation
    identity: UUID | None = None
    ok: StrictBool
    view: FreshExecutionView | None = None
    admission: ExecutionAdmission | None = None
    completion: FreshExecutionCompletion | None = None
    completion_state: (
        Literal["not_claimed", "claim_only", "launch_without_result", "recorded", "stop_recorded"]
        | None
    ) = None
    stop: ExecutionStopView | None = None
    execution_phase: ExecutionLivePhase | Literal["finished", "unconfirmed"] | None = None
    output: DockerOutputSnapshot | None = None
    error: Failure | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.ok != (self.error is None):
            raise ValueError("Success and failure disagree.")
        if not self.ok:
            if any(
                value is not None
                for value in (
                    self.view,
                    self.admission,
                    self.completion,
                    self.completion_state,
                    self.stop,
                    self.execution_phase,
                    self.output,
                )
            ):
                raise ValueError("Refusals cannot carry success facts.")
        elif self.identity is None:
            raise ValueError("Success requires authenticated instance correlation.")
        elif self.operation == "start":
            if (
                self.admission is None
                or self.admission.request_id != self.request_id
                or any(
                    value is not None
                    for value in (
                        self.view,
                        self.completion,
                        self.completion_state,
                        self.stop,
                        self.execution_phase,
                        self.output,
                    )
                )
            ):
                raise ValueError("Start acknowledges only the matching committed claim.")
        elif self.view is None or self.admission is not None:
            raise ValueError("Non-start success requires original scoped facts.")
        if (
            self.completion is not None
            or self.completion_state is not None
            or self.stop is not None
            or self.execution_phase is not None
        ) and self.operation not in {"show", "logs"}:
            raise ValueError("Completion is inspection only.")
        if self.ok and self.operation in {"show", "logs"}:
            if self.completion_state is None or (self.completion_state == "recorded") != (
                self.completion is not None
            ):
                raise ValueError("Inspection requires explicit consistent completion availability.")
            assert self.view is not None
            if self.completion is not None:
                expected = "finished" if self.completion.disposition == "completed" else "unknown"
                if self.view.claim_state != expected:
                    raise ValueError("Inspection authority and completion disagree.")
            elif self.completion_state == "stop_recorded":
                if self.stop is None or self.stop.outcome in {None, "completed"}:
                    raise ValueError("Stop inspection requires a distinct terminal.")
                if self.view.claim_state is None:
                    if (
                        self.stop.evidence != "not_started"
                        or self.view.state != AuthorityState.INVALIDATED
                    ):
                        raise ValueError("Unclaimed cancellation requires positive revocation.")
                elif self.view.claim_state != (
                    "unknown" if self.stop.outcome == "unknown" else "finished"
                ):
                    raise ValueError("Stop terminal and authority disagree.")
            elif self.view.claim_state == "finished" or (
                self.completion_state == "not_claimed"
            ) != (self.view.claim_state is None):
                raise ValueError("Inconsistent absent completion facts.")
            payload = self.completion if self.completion is not None else self.stop
            if self.operation == "logs" and (
                (payload is not None and payload.payload_state == "retained")
                != (self.output is not None)
            ):
                raise ValueError("Log text must agree with explicit retention state.")
        if self.output is not None:
            payload = self.completion if self.completion is not None else self.stop
            if self.operation != "logs" or payload is None or payload.payload_state != "retained":
                raise ValueError("Only logs may carry retained guarded text.")
            if (
                DockerOutputFacts.model_validate(
                    self.output.model_dump(exclude={"stdout", "stderr"})
                )
                != payload.output_facts
            ):
                raise ValueError("Guarded text disagrees with capture facts.")
        return self


def fresh_execution_failure(code: str) -> Failure:
    messages = {
        "execution_unavailable": "Fresh execution is not enabled for this runtime or scope.",
        "execution_busy": "An owned execution or serialized action is still active.",
        "execution_backend_unavailable": (
            "Configured fresh backend failed identity/private-resource checks; it is disabled."
        ),
        "execution_backend_capacity": (
            "Private backend generation capacity is full; no cleanup or reuse was attempted."
        ),
        "execution_conflict": "Request identity or reviewed action conflicts with stored facts.",
        "execution_expired": "Approval expired or was invalidated; a fresh review is required.",
        "execution_not_found": "No fresh execution record exists in this validated scope.",
        "request_outcome_unknown": (
            "Request outcome is unconfirmed; inspect original facts before another action."
        ),
        "execution_unconfirmed": (
            "Fresh execution facts or outcome are unconfirmed; inspect before further action."
        ),
    }
    aliases = {
        "docker_busy": "execution_busy",
        "docker_authority_required": "execution_unavailable",
        "runtime_capability_unavailable": "execution_unavailable",
        "storage_outcome_unconfirmed": "request_outcome_unknown",
    }
    safe = aliases.get(code, code)
    if safe not in messages:
        safe = "execution_unconfirmed"
    return Failure(error=ErrorData(code=safe, message=messages[safe]))

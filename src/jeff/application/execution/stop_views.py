"""Public stop-intent and termination projections, separate from log completeness."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, model_validator

from ._model import _Model
from .output import DockerOutputFacts
from .stop_records import ExecutionStopRecord


class ExecutionStopView(_Model):
    """Wire12 stop facts only: never a controller identity or complete-log promise."""

    request_id: UUID
    reason: Literal["cancel", "timeout", "shutdown"]
    requested_at_ms: StrictInt = Field(ge=0)
    clock_confirmed: StrictBool
    outcome: Literal["cancelled", "timed_out", "completed", "unknown"] | None = None
    evidence: Literal["not_started", "owned", "uncertain"] | None = None
    cleanup_verified: StrictBool | None = None
    exit_code: StrictInt | None = Field(default=None, ge=0, le=255)
    output_facts: DockerOutputFacts | None = None
    payload_state: Literal[
        "not_inspected", "missing", "retained", "retired", "expired", "clock_unconfirmed"
    ] = "not_inspected"
    retirement_reason: Literal["age", "capacity"] | None = None
    recorded_at_ms: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.outcome is None) != (self.evidence is None) or (
            (self.outcome is None) != (self.cleanup_verified is None)
        ):
            raise ValueError("Incomplete stop observation.")
        if self.outcome is None and (self.exit_code is not None or self.output_facts is not None):
            raise ValueError("Pending intent cannot assert terminal capture facts.")
        if self.outcome not in {None, "unknown"} and self.cleanup_verified is not True:
            raise ValueError("Known termination requires confirmed cleanup.")
        if self.evidence == "uncertain" and self.outcome != "unknown":
            raise ValueError("Uncertain evidence cannot confirm termination.")
        if self.evidence == "not_started" and (
            self.outcome not in {"cancelled", "timed_out"}
            or self.exit_code is not None
            or self.output_facts is not None
        ):
            raise ValueError("No-launch proof cannot invent execution facts.")
        if self.outcome in {"cancelled", "timed_out"} and self.outcome != (
            "timed_out" if self.reason == "timeout" else "cancelled"
        ):
            raise ValueError("Termination differs from the first stop reason.")
        if self.outcome == "completed" and (
            self.evidence != "owned"
            or self.exit_code is None
            or self.output_facts is None
            or self.output_facts.aborted
            or not (self.output_facts.stdout_eof and self.output_facts.stderr_eof)
        ):
            raise ValueError("Ordinary completion needs complete, confirmed capture facts.")
        if (self.payload_state == "retired") != (self.retirement_reason is not None):
            raise ValueError("Inconsistent stop payload retirement.")
        if self.payload_state not in {"missing", "not_inspected"} and (
            self.output_facts is None or self.recorded_at_ms is None
        ):
            raise ValueError("Stop payload availability requires capture facts.")
        return self

    @classmethod
    def from_record(cls, record: "ExecutionStopRecord", **payload: object) -> Self:
        fields: dict[str, object] = dict(
            request_id=record.request_id,
            reason=record.reason,
            requested_at_ms=record.requested_at_ms,
            clock_confirmed=record.clock_confirmed,
        )
        terminal = record.terminal
        if terminal is not None:
            fields.update(
                outcome=terminal.outcome,
                evidence=terminal.evidence,
                cleanup_verified=terminal.cleanup_verified,
                exit_code=terminal.exit_code,
                output_facts=terminal.output_facts,
            )
        return cls.model_validate({**fields, **payload})

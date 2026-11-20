"""Durable stop intent and owned evidence, never restartable live timers."""

from hashlib import sha256
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, model_validator

from jeff.domain.execution import fingerprint

from ._model import _Model
from .launch import DockerOwnedContainer
from .output import DockerOutputFacts, DockerOutputSnapshot
from .requests import ExecutionInspectionScope


class ExecutionStopRequest(ExecutionInspectionScope):
    """Internal stop intent, not a signal, completion proof or public launch grant."""

    request_id: UUID
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: Literal["cancel", "timeout", "shutdown"] = "cancel"


class ExecutionStopTerminal(_Model):
    """Versioned terminal facts; known termination does not imply complete logs."""

    outcome: Literal["cancelled", "timed_out", "completed", "unknown"]
    evidence: Literal["not_started", "owned", "uncertain"]
    cleanup_verified: StrictBool
    exit_code: StrictInt | None = Field(default=None, ge=0, le=255)
    attempt_id: UUID | None = None
    ownership_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_facts: DockerOutputFacts | None = None
    output_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def consistent(self) -> Self:
        owned = self.evidence == "owned"
        if owned != (self.attempt_id is not None and self.ownership_digest is not None):
            raise ValueError("Owned stop evidence requires exact identity facts.")
        if not owned and (self.attempt_id is not None or self.ownership_digest is not None):
            raise ValueError("Unowned stop evidence cannot invent a container identity.")
        if (self.output_facts is None) != (self.output_digest is None):
            raise ValueError("Output facts and digest must agree.")
        if self.outcome != "unknown" and not self.cleanup_verified:
            raise ValueError("Known stop requires verified cleanup.")
        if self.evidence == "not_started" and (
            self.outcome not in {"cancelled", "timed_out"}
            or self.exit_code is not None
            or self.output_facts is not None
        ):
            raise ValueError("A prelaunch stop cannot invent execution or output.")
        if self.evidence == "uncertain" and self.outcome != "unknown":
            raise ValueError("Uncertain evidence cannot confirm termination.")
        if self.outcome == "completed" and (not owned or self.exit_code is None):
            raise ValueError("Ordinary completion requires observed owned exit.")
        return self


class ExecutionStopRecord(_Model):
    """Durable stop facts, never a restartable timer or reusable control handle.

    The first request fixes UTC observation and policy budgets. A live owner must
    cap its monotonic deadlines by the original job and shutdown deadlines; loading
    these facts never starts another timer or reconciles unknown work.
    """

    version: Literal[1] = 1
    scope: ExecutionInspectionScope
    request_id: UUID
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: Literal["cancel", "timeout", "shutdown"]
    requested_at_ms: StrictInt = Field(ge=0, le=9223372036854760807)
    clock_confirmed: StrictBool
    grace_seconds: Literal[5] = 5
    cleanup_seconds: Literal[10] = 10
    completion_seconds: Literal[5] = 5
    terminal: ExecutionStopTerminal | None = None

    @model_validator(mode="after")
    def reason_matches(self) -> Self:
        if self.terminal is not None and self.terminal.outcome in {"cancelled", "timed_out"}:
            expected = "timed_out" if self.reason == "timeout" else "cancelled"
            if self.terminal.outcome != expected:
                raise ValueError("Terminal observation disagrees with first stop reason.")
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class ExecutionStopObservation(_Model):
    """Trusted owner-to-SQL facts; constructed models alone do not prove no launch."""

    scope: ExecutionInspectionScope
    request_id: UUID
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminal: ExecutionStopTerminal
    owned: DockerOwnedContainer | None = None

    @model_validator(mode="after")
    def bound(self) -> Self:
        if (self.terminal.evidence == "owned") != (self.owned is not None):
            raise ValueError("Stop observation requires matching ownership evidence.")
        if self.owned is not None:
            intent = self.owned.intent
            expected = fingerprint(
                {
                    "intent": intent.fingerprint,
                    "container": self.owned.container_id,
                    "birth": self.owned.created_at,
                }
            )
            if (
                self.terminal.attempt_id != intent.attempt_id
                or self.terminal.ownership_digest != expected
                or self.manifest_digest != intent.record.manifest_digest
                or self.scope.approval_id != intent.record.approval_id
                or self.scope.project_id != intent.record.project_id
                or self.scope.canonical_root != intent.record.canonical_root
                or self.scope.owner_instance_id != intent.record.owner_instance_id
            ):
                raise ValueError("Stop observation belongs to different owned work.")
        return self


class ExecutionStopCapture(_Model):
    """One atomic owner-to-SQL stop plus already sealed, guarded capture."""

    observation: ExecutionStopObservation
    output: DockerOutputSnapshot

    @model_validator(mode="after")
    def bound(self) -> Self:
        terminal = self.observation.terminal
        facts = DockerOutputFacts.model_validate(
            self.output.model_dump(exclude={"stdout", "stderr"})
        )
        if (
            terminal.outcome == "completed"
            or terminal.output_facts != facts
            or (
                terminal.output_digest
                != sha256(self.output.model_dump_json().encode("utf-8")).hexdigest()
            )
        ):
            raise ValueError("Stop capture differs from immutable terminal facts.")
        return self

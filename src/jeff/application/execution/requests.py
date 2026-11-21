"""Fresh public intents, live-phase vocabulary and immutable inspection scope."""

from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictInt, field_validator, model_validator

from jeff.domain.execution import (
    ARGV_BYTES,
    ARGV_COUNT,
    PREPARED_FILE_LIMIT,
    fingerprint,
    public_text,
)

from ._model import _Model, absolute_path
from .prepared_inputs import PreparedInputFault, canonical_prepared_paths

ExecutionLivePhase = Literal["claim_pending", "admitted", "running_observed", "stop_requested"]
JobClaimFilter = Literal["claimed", "finished", "unknown"]
JobKindFilter = Literal["pytest", "command"]


FreshExecutionOperation = Literal[
    "propose",
    "approve",
    "deny",
    "start",
    "show",
    "logs",
    "approvals.list",
    "jobs.list",
    "tests.results",
    "tests.latest",
    "tests.retry-failed",
    "jobs.logs",
    "watch.job",
    "watch.pending",
    "watch.ack",
]


def parse_execution_cursor(value: str) -> tuple[int, str]:
    """An immutable ordering boundary, never authority or a database row identifier."""
    stamp, identifier = value.split(":", 1)
    number = int(stamp)
    canonical = str(UUID(identifier))
    if not 0 <= number <= 9223372036854775807 or stamp != str(number) or identifier != canonical:
        raise ValueError("Invalid execution page cursor.")
    return number, canonical


class FreshExecutionCall(_Model):
    """Public intent only; no current proof, backend settings or launch authority."""

    authority_kind: Literal["execution_v1"] = "execution_v1"
    request_id: UUID
    directory: Path
    operation: FreshExecutionOperation
    profile_name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    argv: tuple[str, ...] = Field(default=(), max_length=ARGV_COUNT)
    paths: tuple[str, ...] = Field(default=(), max_length=PREPARED_FILE_LIMIT)
    approval_id: UUID | None = None
    job_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    watch_target: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    claim_filter: JobClaimFilter | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    kind_filter: JobKindFilter | None = Field(default=None, exclude_if=lambda value: value is None)
    log_after: StrictInt | None = Field(
        default=None, ge=0, le=9223372036854775807, exclude_if=lambda value: value is None
    )
    reviewed_manifest_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    after: str | None = Field(default=None, max_length=56)
    limit: StrictInt = Field(default=16, ge=1, le=32)

    @field_validator("after")
    @classmethod
    def cursor(cls, value: str | None) -> str | None:
        if value is not None:
            parse_execution_cursor(value)
        return value

    @field_validator("directory")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

    @model_validator(mode="after")
    def shape(self) -> Self:
        if self.operation != "jobs.list" and (
            self.claim_filter is not None or self.kind_filter is not None
        ):
            raise ValueError("Only fresh job listings accept job filters.")
        if self.operation in {"watch.job", "watch.pending", "watch.ack"}:
            if (
                (self.watch_target is None) != (self.operation == "watch.pending")
                or self.approval_id is not None
                or self.job_id is not None
                or self.profile_name is not None
                or self.argv
                or self.paths
                or self.after is not None
                or self.limit != 16
                or self.reviewed_manifest_digest is not None
                or self.log_after is not None
            ):
                raise ValueError("Invalid watch request shape.")
            return self
        if self.watch_target is not None:
            raise ValueError("Only watch requests accept a watch target.")
        if self.log_after is not None and self.operation != "jobs.logs":
            raise ValueError("Only job logs accept a log cursor.")
        if self.operation in {"tests.results", "tests.latest", "tests.retry-failed", "jobs.logs"}:
            if (
                (self.job_id is None) != (self.operation == "tests.latest")
                or self.approval_id is not None
                or self.profile_name is not None
                or self.argv
                or self.paths
                or self.after is not None
                or self.limit != 16
                or self.reviewed_manifest_digest is not None
            ):
                raise ValueError(
                    "Test inspection or retry accepts only the expected project and job scope."
                )
            return self
        if self.job_id is not None:
            raise ValueError("Only test results accept a job ID.")
        if self.operation in {"approvals.list", "jobs.list"}:
            if (
                self.profile_name is not None
                or self.argv
                or self.paths
                or self.approval_id is not None
                or self.reviewed_manifest_digest is not None
            ):
                raise ValueError("Listings accept scope and pagination only.")
            fingerprint(self.model_dump(mode="json"))
            return self
        if self.after is not None or self.limit != 16:
            raise ValueError("Only listings accept pagination.")
        if self.operation == "propose":
            if (
                self.profile_name is None
                or not self.argv
                or self.approval_id is not None
                or self.reviewed_manifest_digest is not None
            ):
                raise ValueError("Proposals require only a profile, literal argv and inputs.")
            for argument in self.argv:
                public_text(argument, ARGV_BYTES)
            try:
                canonical_prepared_paths(self.paths)
            except PreparedInputFault:
                raise ValueError("Invalid literal input inventory.") from None
        elif self.profile_name is not None or self.argv or self.paths or self.approval_id is None:
            raise ValueError(
                "Targeted operations require approval identity without new action inputs."
            )
        if (self.operation in {"approve", "deny", "start"}) != (
            self.reviewed_manifest_digest is not None
        ):
            raise ValueError("Decisions and starts require the exact reviewed digest only.")
        fingerprint(self.model_dump(mode="json"))
        return self


class ExecutionListScope(_Model):
    """Current authenticated reader and exact registered project; no mutation authority."""

    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    claim_filter: JobClaimFilter | None = None
    kind_filter: JobKindFilter | None = None
    after: str | None = Field(default=None, max_length=56)
    limit: StrictInt = Field(default=16, ge=1, le=32)

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

    @field_validator("after")
    @classmethod
    def cursor(cls, value: str | None) -> str | None:
        if value is not None:
            parse_execution_cursor(value)
        return value


class ExecutionCancelCall(_Model):
    """Authenticated stop intent targets immutable reviewed identity, never live paths."""

    request_id: UUID
    approval_id: UUID
    reviewed_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutionInspectionScope(_Model):
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    approval_id: UUID | None = None
    job_id: UUID | None = None
    latest_pytest: bool = False

    @model_validator(mode="after")
    def exactly_one_identity(self) -> Self:
        if sum((self.approval_id is not None, self.job_id is not None, self.latest_pytest)) != 1:
            raise ValueError("Inspection must target one approval or one job.")
        return self

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

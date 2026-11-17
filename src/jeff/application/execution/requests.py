"""Fresh public intents, live-phase vocabulary and immutable inspection scope."""

from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

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


FreshExecutionOperation = Literal["propose", "approve", "deny", "start", "show", "logs"]


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
    reviewed_manifest_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("directory")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

    @model_validator(mode="after")
    def shape(self) -> Self:
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


class ExecutionCancelCall(_Model):
    """Authenticated stop intent targets immutable reviewed identity, never live paths."""

    request_id: UUID
    approval_id: UUID
    reviewed_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutionInspectionScope(_Model):
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    approval_id: UUID

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

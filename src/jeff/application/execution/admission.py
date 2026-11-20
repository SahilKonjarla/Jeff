"""Confirmed admission acknowledgements, never proof a guest started."""

from pathlib import Path
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, StrictBool, field_validator, model_validator

from jeff.application.projects import Failure

from ._model import _Model, absolute_path


class ExecutionAdmission(_Model):
    """Confirmed claim acknowledgement, never a token or proof a guest started.

    Replays describe current persisted facts only. Delivery of this observation is
    independent of the lifetime of the worker that owns a first confirmed claim.
    """

    authority_kind: Literal["execution_v1"] = "execution_v1"
    request_id: UUID
    project_id: UUID
    canonical_root: Path
    approval_id: UUID
    job_id: UUID
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_state: Literal["claimed", "unknown", "finished"]
    replayed: StrictBool

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

    @model_validator(mode="after")
    def first_claim(self) -> Self:
        if not self.replayed and self.claim_state != "claimed":
            raise ValueError("A first admission acknowledges a confirmed claim only.")
        return self


class ExecutionStartCall(_Model):
    """Explicit fresh start identity; reviewed digest is not a current-proof override."""

    authority_kind: Literal["execution_v1"] = "execution_v1"
    request_id: UUID = Field(default_factory=uuid4)
    directory: Path
    approval_id: UUID
    reviewed_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("directory")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)


class ExecutionStartReply(_Model):
    """Admission or refusal only; actual completion is inspected through durable facts."""

    request_id: UUID
    identity: UUID | None = None
    admission: ExecutionAdmission | None = None
    error: Failure | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.admission is None) == (self.error is None):
            raise ValueError("Start replies require exactly one admission or failure.")
        if self.admission is not None and (
            self.identity is None or self.admission.request_id != self.request_id
        ):
            raise ValueError("Admission replies require matching identity and correlation.")
        return self

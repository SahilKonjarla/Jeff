"""Legacy review-only requests and durable facts; never launch authority."""

from pathlib import Path
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, StrictInt, field_validator, model_validator

from jeff.application.projects import Failure
from jeff.domain.execution import ARGV_COUNT, ReviewState, public_text

from ._model import _Model, absolute_path
from .manifests import ActionManifest

ExecutionOperation = Literal[
    "propose", "approve", "deny", "approval.show", "approval.list", "job.show", "job.list"
]


class ExecutionCall(_Model):
    request_id: UUID = Field(default_factory=uuid4)
    directory: Path
    expected_root: Path | None = None
    expected_project_id: UUID | None = None
    operation: ExecutionOperation
    profile_name: str = Field(default="default", pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    argv: tuple[str, ...] = Field(default=(), max_length=ARGV_COUNT)
    object_id: UUID | None = None
    after: StrictInt = Field(default=0, ge=0, le=9223372036854775807)
    limit: StrictInt = Field(default=16, ge=1, le=32)

    @field_validator("directory", "expected_root")
    @classmethod
    def path(cls, value: Path | None) -> Path | None:
        return absolute_path(value) if value is not None else None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.operation == "propose") != bool(self.argv):
            raise ValueError("Only proposals carry argv.")
        if (self.operation in {"approve", "deny", "approval.show", "job.show"}) != (
            self.object_id is not None
        ):
            raise ValueError("Only targeted operations carry an object ID.")
        if not self.operation.endswith(".list") and (self.after != 0 or self.limit != 16):
            raise ValueError("Only list operations carry pagination.")
        for value in self.argv:
            public_text(value)
        return self


class ReviewRecord(_Model):
    sequence: StrictInt = Field(gt=0)
    proposal_id: UUID
    approval_id: UUID
    job_id: UUID
    manifest: ActionManifest
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: ReviewState
    created_at_ms: StrictInt = Field(ge=0)
    expires_at_ms: StrictInt = Field(ge=0)
    owner_instance_id: UUID
    job_state: Literal["waiting_for_approval", "blocked"]
    reason: Literal[
        "approval_required",
        "execution_unavailable",
        "approval_denied",
        "approval_expired",
        "approval_invalidated",
    ]
    execution_available: Literal[False] = False
    launch_authority_issued: Literal[False] = False

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.manifest_digest != self.manifest.fingerprint:
            raise ValueError("Stored manifest fingerprint does not match.")
        waiting = self.state == ReviewState.PENDING
        if waiting != (self.job_state == "waiting_for_approval"):
            raise ValueError("Review state does not match job disposition.")
        reasons = {
            ReviewState.PENDING: "approval_required",
            ReviewState.APPROVED: "execution_unavailable",
            ReviewState.DENIED: "approval_denied",
            ReviewState.EXPIRED: "approval_expired",
            ReviewState.INVALIDATED: "approval_invalidated",
        }
        if self.reason != reasons[self.state] or self.expires_at_ms < self.created_at_ms:
            raise ValueError("Review facts are inconsistent.")
        return self


class ExecutionData(_Model):
    items: tuple[ReviewRecord, ...] = Field(default=(), max_length=32)
    next_after: StrictInt | None = Field(default=None, gt=0)
    execution_available: Literal[False] = False
    message: Literal[
        "Review only: execution is unavailable. Fresh Phase3 approval is required."
    ] = "Review only: execution is unavailable. Fresh Phase3 approval is required."


class ExecutionSuccess(_Model):
    ok: Literal[True] = True
    data: ExecutionData
    error: None = None


ExecutionResult = ExecutionSuccess | Failure


class ExecutionReply(_Model):
    request_id: UUID
    identity: UUID | None = None
    result: ExecutionResult


class ReviewOperation(_Model):
    call: ExecutionCall
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    manifest: ActionManifest | None = None
    configuration_fingerprint: str | None = None


class ReviewFault(Exception):
    """Fixed review refusal, not a raw storage or validation exception."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

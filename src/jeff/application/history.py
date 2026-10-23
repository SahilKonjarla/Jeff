"""Allowlisted durable evidence and scoped reads; no raw log or dispatch dependencies."""

from pathlib import Path
from typing import Literal, Protocol, Self, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

ACTIVITY_EVENT_LIMIT = 32
INT64_MAX = 9223372036854775807
ActivityCode = Literal[
    "ok",
    "operation_failed",
    "timeout",
    "interrupted",
    "partial",
    "target_mismatch",
    "git_failed",
    "git_timeout",
    "git_output_limit",
    "git_unavailable",
    "git_invalid_output",
    "git_unsafe_config",
    "git_filters_unsupported",
    "project_target_mismatch",
    "storage_io",
    "unsafe_path",
    "config_invalid",
    "project_moved",
    "project_not_initialized",
    "registry_conflict",
]
_CODES = frozenset(
    (
        "ok",
        "operation_failed",
        "timeout",
        "interrupted",
        "partial",
        "target_mismatch",
        "git_failed",
        "git_timeout",
        "git_output_limit",
        "git_unavailable",
        "git_invalid_output",
        "git_unsafe_config",
        "git_filters_unsupported",
        "project_target_mismatch",
        "storage_io",
        "unsafe_path",
        "config_invalid",
        "project_moved",
        "project_not_initialized",
        "registry_conflict",
    )
)
GitCategory = Literal["filters.inspect", "status.branch", "status.changes"]


def activity_code(code: str) -> ActivityCode:
    return cast(ActivityCode, code if code in _CODES else "operation_failed")


class _ActivityModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class GitObservation(_ActivityModel):
    process_started: StrictBool
    category: GitCategory
    code: Literal["ok", "git_failed", "git_timeout", "git_output_limit", "git_unavailable"]
    elapsed_ms: StrictInt = Field(ge=0, le=INT64_MAX)
    output_bytes: StrictInt = Field(ge=0, le=2147483647)


class GitObserver(Protocol):
    def record(self, observation: GitObservation) -> None: ...


class ActivityRecord(_ActivityModel):
    request_id: UUID
    owner_instance_id: UUID
    git: GitObservation | None = None
    registration_created: StrictBool | None = None

    @model_validator(mode="after")
    def one_observation(self) -> Self:
        if (self.git is None) == (self.registration_created is None):
            raise ValueError("Exactly one fixed observation is required.")
        return self


class ActivityEvent(_ActivityModel):
    sequence: StrictInt = Field(gt=0, le=INT64_MAX)
    request_id: UUID
    owner_instance_id: UUID
    canonical_root: Path
    project_id: UUID | None
    kind: Literal[
        "request.accepted",
        "request.running",
        "identity.established",
        "registration.observed",
        "git.finished",
        "request.succeeded",
        "request.failed",
        "request.unknown",
        "request.interrupted",
    ]
    recorded_at_ms: StrictInt = Field(ge=0, le=INT64_MAX)
    code: ActivityCode | None = None
    git: GitObservation | None = None
    registration_created: StrictBool | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.kind == "git.finished") != (self.git is not None):
            raise ValueError("Only a Git completion carries a Git observation.")
        if self.git is not None and self.code != self.git.code:
            raise ValueError("A Git event must preserve its fixed observed outcome.")
        if (self.kind == "registration.observed") != (self.registration_created is not None):
            raise ValueError("Only registration observations carry creation facts.")
        return self


class ActivityMeta(_ActivityModel):
    database_epoch: UUID
    coverage_started_at_ms: StrictInt = Field(ge=0, le=INT64_MAX)
    high_water: StrictInt = Field(ge=0, le=INT64_MAX)
    retention_generation: StrictInt = Field(ge=0, le=INT64_MAX)


class ActivityTrace(ActivityMeta):
    legacy_metadata_only: StrictBool = False
    events: tuple[ActivityEvent, ...] = Field(default=(), max_length=ACTIVITY_EVENT_LIMIT)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.legacy_metadata_only != (not self.events):
            raise ValueError("Only legacy metadata can omit its actual accepted event.")
        if self.events and (
            self.events[0].kind != "request.accepted"
            or any(
                left.sequence >= right.sequence
                for left, right in zip(self.events, self.events[1:], strict=False)
            )
            or self.events[-1].sequence > self.high_water
        ):
            raise ValueError("Trace events require actual monotonic committed order.")
        return self


class ReplayScope(_ActivityModel):
    project_id: UUID
    canonical_root: Path
    after: StrictInt = Field(default=0, ge=0, le=INT64_MAX)
    limit: StrictInt = Field(default=64, gt=0, le=64)
    expected_epoch: UUID | None = None

    @field_validator("canonical_root")
    @classmethod
    def absolute(cls, root: Path) -> Path:
        if not root.is_absolute() or "\0" in str(root) or ".." in root.parts:
            raise ValueError("A validated canonical root is required.")
        return root


class ActivityPage(ActivityMeta):
    events: tuple[ActivityEvent, ...] = Field(max_length=64)
    next_after: StrictInt = Field(ge=0, le=INT64_MAX)
    has_more: StrictBool

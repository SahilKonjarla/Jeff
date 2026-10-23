"""Allowlisted durable evidence and scoped reads; no raw log or dispatch dependencies."""

import base64
import binascii
import json
from pathlib import Path
from typing import Any, Literal, Protocol, Self, cast
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
    replay_floor: StrictInt = Field(default=0, ge=0, le=INT64_MAX)

    @model_validator(mode="after")
    def floor_ordered(self) -> Self:
        if self.replay_floor > self.high_water:
            raise ValueError("Replay floor cannot exceed the committed high-water mark.")
        return self


class RetentionPosition(_ActivityModel):
    """Committed invalidation facts only; no event payload or alternate journal."""

    database_epoch: UUID
    retention_generation: StrictInt = Field(ge=0, le=INT64_MAX)
    replay_floor: StrictInt = Field(ge=0, le=INT64_MAX)


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


class HistoryScope(_ActivityModel):
    project_id: UUID
    canonical_root: Path

    @field_validator("canonical_root")
    @classmethod
    def absolute(cls, root: Path) -> Path:
        if not root.is_absolute() or "\0" in str(root) or ".." in root.parts:
            raise ValueError("A validated canonical root is required.")
        return root


class HistoryRead(HistoryScope):
    limit: StrictInt = Field(default=50, gt=0, le=100)
    cursor: str | None = Field(default=None, max_length=8192)


class HistoryCursor(HistoryScope):
    kind: Literal["history.v1"] = "history.v1"
    database_epoch: UUID
    retention_generation: StrictInt = Field(ge=0, le=INT64_MAX)
    snapshot_high_water: StrictInt = Field(gt=0, le=INT64_MAX)
    before: StrictInt = Field(gt=0, le=INT64_MAX)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.before > self.snapshot_high_water:
            raise ValueError("Invalid cursor position.")
        return self

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> Self:
        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Invalid history cursor.")
                result[key] = value
            return result

        def invalid(value: str) -> None:
            raise ValueError("Invalid history cursor.")

        try:
            if not token or len(token) > 8192:
                raise ValueError("Invalid history cursor.")
            data = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            raw = json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
            result = cls.model_validate(raw)
            if result.encode() != token:
                raise ValueError("Invalid history cursor.")
            return result
        except (ValueError, TypeError, binascii.Error, RecursionError):
            raise ValueError("Invalid history cursor.") from None


class HistoryItem(_ActivityModel):
    request_id: UUID
    canonical_root: Path
    project_id: UUID
    operation: Literal["init", "ask.branch", "ask.changes"]
    state: Literal["accepted", "running", "succeeded", "failed", "unknown"]
    accepted_sequence: StrictInt = Field(gt=0, le=INT64_MAX)
    accepted_at_ms: StrictInt = Field(ge=0, le=INT64_MAX)
    finished_at_ms: StrictInt | None = Field(default=None, ge=0, le=INT64_MAX)
    outcome_code: (
        Literal["ok", "operation_failed", "timeout", "interrupted", "partial", "target_mismatch"]
        | None
    ) = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        terminal = self.state in ("succeeded", "failed", "unknown")
        if terminal != (self.finished_at_ms is not None and self.outcome_code is not None):
            raise ValueError("Only terminal summaries have complete outcome facts.")
        if not terminal and (self.finished_at_ms is not None or self.outcome_code is not None):
            raise ValueError("Unfinished summaries cannot claim an outcome.")
        return self


class HistoryPage(ActivityMeta):
    snapshot_high_water: StrictInt = Field(ge=0, le=INT64_MAX)
    items: tuple[HistoryItem, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=8192)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.snapshot_high_water > self.high_water:
            raise ValueError("A history snapshot cannot exceed committed coverage.")
        if (not self.items and self.next_cursor is not None) or any(
            item.accepted_sequence > self.snapshot_high_water for item in self.items
        ):
            raise ValueError("A history page requires actual bounded membership.")
        if any(
            left.accepted_sequence <= right.accepted_sequence
            for left, right in zip(self.items, self.items[1:], strict=False)
        ):
            raise ValueError("A history page requires descending acceptance order.")
        if self.items and any(
            (item.project_id, item.canonical_root)
            != (self.items[0].project_id, self.items[0].canonical_root)
            for item in self.items
        ):
            raise ValueError("A history page cannot mix project scopes.")
        return self


class ActivityPage(ActivityMeta):
    events: tuple[ActivityEvent, ...] = Field(max_length=64)
    next_after: StrictInt = Field(ge=0, le=INT64_MAX)
    has_more: StrictBool


class StreamCursor(HistoryScope):
    kind: Literal["activity.v1"] = "activity.v1"
    database_epoch: UUID
    retention_generation: StrictInt = Field(ge=0, le=INT64_MAX)
    after: StrictInt = Field(ge=0, le=INT64_MAX)

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> Self:
        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Invalid activity cursor.")
                result[key] = value
            return result

        def invalid(value: str) -> None:
            raise ValueError("Invalid activity cursor.")

        try:
            if not token or len(token) > 8192:
                raise ValueError("Invalid activity cursor.")
            data = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            result = cls.model_validate(
                json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
            )
            if result.encode() != token:
                raise ValueError("Invalid activity cursor.")
            return result
        except (ValueError, TypeError, binascii.Error, RecursionError):
            raise ValueError("Invalid activity cursor.") from None


class StreamRead(HistoryScope):
    cursor: str | None = Field(default=None, max_length=8192)
    limit: StrictInt = Field(default=64, gt=0, le=64)


class StreamPage(ActivityMeta, HistoryScope):
    start_after: StrictInt = Field(ge=0, le=INT64_MAX)
    scanned_through: StrictInt = Field(ge=0, le=INT64_MAX)
    events: tuple[ActivityEvent, ...] = Field(max_length=64)
    has_more: StrictBool

    def position(self, after: int) -> StreamCursor:
        return StreamCursor(
            project_id=self.project_id,
            canonical_root=self.canonical_root,
            database_epoch=self.database_epoch,
            retention_generation=self.retention_generation,
            after=after,
        )

    @model_validator(mode="after")
    def coherent(self) -> Self:
        positions = [self.start_after, *(event.sequence for event in self.events)]
        if not self.start_after <= self.scanned_through <= self.high_water or any(
            left >= right for left, right in zip(positions, positions[1:], strict=False)
        ):
            raise ValueError("Stream pages require increasing committed positions.")
        if self.events and self.events[-1].sequence > self.scanned_through:
            raise ValueError("Cannot advance behind a returned event.")
        if any(
            (e.project_id, e.canonical_root) != (self.project_id, self.canonical_root)
            for e in self.events
        ):
            raise ValueError("Stream pages require observed project identity.")
        if self.has_more and (not self.events or self.scanned_through != self.events[-1].sequence):
            raise ValueError("Cannot advance beyond an unreturned eligible event.")
        if not self.has_more and self.scanned_through != self.high_water:
            raise ValueError("Exhausted pages advance through committed coverage.")
        return self

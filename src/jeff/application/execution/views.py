"""Public review and completion projections without private ownership handles."""

from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictInt, field_validator, model_validator

from jeff.domain.execution import AuthorityState

from ._model import _Model, absolute_path
from .authority import AuthorityRecord
from .manifests import ExecutionManifest
from .output import DockerJobOutcome, DockerOutputFacts
from .requests import parse_execution_cursor


class FreshExecutionView(_Model):
    """Original reviewable facts, excluding private claim/owner/controller identities."""

    authority_kind: Literal["execution_v1"] = "execution_v1"
    proposal_id: UUID
    approval_id: UUID
    job_id: UUID
    project_id: UUID
    canonical_root: Path
    manifest: ExecutionManifest
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: AuthorityState
    created_at_ms: StrictInt = Field(ge=0)
    expires_at_ms: StrictInt = Field(ge=0)
    claim_state: Literal["claimed", "unknown", "finished"] | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (
            self.manifest_digest != self.manifest.fingerprint
            or self.project_id != self.manifest.profile.project_id
            or self.canonical_root != self.manifest.profile.canonical_root
            or self.expires_at_ms < self.created_at_ms
            or (self.state == AuthorityState.CONSUMED) != (self.claim_state is not None)
        ):
            raise ValueError("Inconsistent original execution facts.")
        return self

    @classmethod
    def from_record(cls, record: AuthorityRecord) -> Self:
        return cls.model_validate(
            {
                name: getattr(record, name)
                for name in (
                    "proposal_id",
                    "approval_id",
                    "job_id",
                    "project_id",
                    "canonical_root",
                    "manifest",
                    "manifest_digest",
                    "state",
                    "created_at_ms",
                    "expires_at_ms",
                    "claim_state",
                )
            }
        )


class FreshExecutionPage(_Model):
    """Bounded public review/job facts, newest first; concurrent scans are not snapshots."""

    project_id: UUID
    canonical_root: Path
    items: tuple[FreshExecutionView, ...] = Field(default=(), max_length=32)
    next_after: str | None = Field(default=None, max_length=56)

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

    def within(self, *, after: str | None, limit: int) -> bool:
        """Check query bounds; project and authenticated identity binding remain mandatory."""
        boundary = parse_execution_cursor(after) if after is not None else None
        return len(self.items) <= limit and (
            boundary is None
            or all((item.created_at_ms, str(item.proposal_id)) < boundary for item in self.items)
        )

    @model_validator(mode="after")
    def coherent(self) -> Self:
        keys = [(item.created_at_ms, str(item.proposal_id)) for item in self.items]
        if len(set(keys)) != len(keys) or keys != sorted(keys, reverse=True):
            raise ValueError("Listing order or identities disagree.")
        if any(
            (item.project_id, item.canonical_root) != (self.project_id, self.canonical_root)
            for item in self.items
        ):
            raise ValueError("Listing crosses project scope.")
        if self.next_after is not None and (
            not keys or parse_execution_cursor(self.next_after) != keys[-1]
        ):
            raise ValueError("Cursor does not bind the final listed record.")
        return self


class FreshExecutionCompletion(_Model):
    """Durable result projection without owned handles or host/container identifiers."""

    disposition: Literal["completed", "quarantined"]
    outcome: DockerJobOutcome
    output_facts: DockerOutputFacts
    payload_state: Literal["retained", "retired", "expired", "clock_unconfirmed"]
    retirement_reason: Literal["age", "capacity"] | None = None
    recorded_at_ms: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.payload_state == "retired") != (self.retirement_reason is not None):
            raise ValueError("Inconsistent payload retirement facts.")
        if self.disposition == "completed" and (
            self.outcome.outcome_unknown
            or self.outcome.timed_out
            or not self.outcome.cleanup_verified
            or self.outcome.exit_code is None
            or self.output_facts.aborted
            or not (self.output_facts.stdout_eof and self.output_facts.stderr_eof)
        ):
            raise ValueError("Unconfirmed completion cannot be called completed.")
        return self

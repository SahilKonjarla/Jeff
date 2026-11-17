"""Private fresh authority operations and facts-only proposal lookup scope."""

from pathlib import Path
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, StrictInt, field_validator, model_validator

from jeff.domain.execution import AuthorityState

from ._model import _Model, absolute_path
from .manifests import ExecutionManifest


class AuthorityOperation(_Model):
    """Trusted installation-internal port; current digest is a verifier obligation.

    Digest equality alone does not establish actual engine or input verification.
    The future verifier must check these before propose, approve and worker claim.
    """

    operation: Literal["propose", "approve", "deny", "show", "claim"]
    request_id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    approval_id: UUID | None = None
    manifest: ExecutionManifest | None = None
    current_manifest_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

    @model_validator(mode="after")
    def shape(self) -> Self:
        if (self.operation == "propose") != (self.manifest is not None):
            raise ValueError("Only a fresh proposal carries a manifest.")
        if (self.operation != "propose") != (self.approval_id is not None):
            raise ValueError("Only targeted operations carry approval identity.")
        if (self.operation in {"propose", "approve", "claim"}) != (
            self.current_manifest_digest is not None
        ):
            raise ValueError("Authority changes require a current verifier binding.")
        return self


class AuthorityRecord(_Model):
    proposal_id: UUID
    approval_id: UUID
    job_id: UUID
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    manifest: ExecutionManifest
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: AuthorityState
    created_at_ms: StrictInt = Field(ge=0)
    expires_at_ms: StrictInt = Field(ge=0)
    claim_id: UUID | None = None
    claim_state: Literal["claimed", "unknown", "finished"] | None = None
    claimed_at_ms: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (
            self.manifest_digest != self.manifest.fingerprint
            or self.project_id != self.manifest.profile.project_id
            or self.canonical_root != self.manifest.profile.canonical_root
            or self.expires_at_ms < self.created_at_ms
        ):
            raise ValueError("Authority identity or manifest facts disagree.")
        consumed = self.state == AuthorityState.CONSUMED
        if any(
            (item is not None) != consumed
            for item in (self.claim_id, self.claim_state, self.claimed_at_ms)
        ):
            raise ValueError("Only consumed authority carries a complete launch claim.")
        if self.claimed_at_ms is not None and not (
            self.created_at_ms <= self.claimed_at_ms < self.expires_at_ms
        ):
            raise ValueError("Claim time must be within the original approval lifetime.")
        return self


class AuthorityResult(_Model):
    record: AuthorityRecord
    # First confirmed claim response only. Never a replayable public bearer token.
    launch_claim: UUID | None = None

    @model_validator(mode="after")
    def fresh_claim(self) -> Self:
        if self.launch_claim is not None and (
            self.launch_claim != self.record.claim_id or self.record.claim_state != "claimed"
        ):
            raise ValueError("Fresh claim does not match consumed authority.")
        return self


class ProposalRequestScope(_Model):
    """Facts lookup identity, never preparation or launch permission."""

    request_id: UUID
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

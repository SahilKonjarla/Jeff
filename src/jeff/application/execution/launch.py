"""Owned launch identities and durable lifecycle observations, not live handles."""

from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictInt, field_validator, model_validator

from jeff.domain.execution import AuthorityState, fingerprint

from ._model import _Model, absolute_path
from .authority import AuthorityRecord
from .docker_target import VerifiedDockerTarget
from .prepared_inputs import PreparedInputBinding


class DockerLaunchIntent(_Model):
    """Durable facts only: deliberately excludes transient AuthorityResult.launch_claim."""

    attempt_id: UUID
    record: AuthorityRecord
    target: VerifiedDockerTarget
    inputs: PreparedInputBinding
    nonce: str = Field(pattern=r"^[0-9a-f]{32}$")

    @model_validator(mode="after")
    def bound(self) -> Self:
        profile = self.record.manifest.profile
        if (
            self.record.state != AuthorityState.CONSUMED
            or self.record.claim_state != "claimed"
            or self.target.profile_digest != profile.fingerprint
            or self.target.engine_id != profile.engine_id
            or self.target.endpoint != profile.endpoint
            or self.target.image_id != profile.image_id
            or self.target.platform != profile.platform
            or self.inputs.snapshot_id != self.record.manifest.inputs.snapshot_id
            or self.inputs.inventory_digest != self.record.manifest.inputs.digest
        ):
            raise ValueError("Launch intent is not bound to consumed fresh authority.")
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(
            {
                "attempt": str(self.attempt_id),
                "claim": str(self.record.claim_id),
                "job": str(self.record.job_id),
                "owner": str(self.record.owner_instance_id),
                "manifest": self.record.manifest_digest,
                "target": self.target.fingerprint,
                "inputs": self.inputs.model_dump(mode="json"),
                "nonce": self.nonce,
            }
        )


class DockerOwnedContainer(_Model):
    """Observation only. Controller-minted object identity is required for live control.

    Deserializing this model never adopts a container or reconstructs launch authority.
    """

    intent: DockerLaunchIntent
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(min_length=20, max_length=64, pattern=r"^[0-9TZ:.+\-]+$")


class DockerLaunchEvent(_Model):
    intent: DockerLaunchIntent
    phase: Literal["create_intent", "created", "start_intent", "exited", "cleaned", "unknown"]
    container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: str | None = Field(
        default=None, min_length=20, max_length=64, pattern=r"^[0-9TZ:.+\-]+$"
    )
    # A captured create reply without verified birth/ownership is evidence only.
    observed_container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    exit_code: StrictInt | None = Field(default=None, ge=0, le=255)

    @model_validator(mode="after")
    def shape(self) -> Self:
        if (self.container_id is None) != (self.created_at is None):
            raise ValueError("Owned identity and birth must be paired.")
        if self.phase == "create_intent" and (
            self.container_id is not None or self.observed_container_id is not None
        ):
            raise ValueError("Create intent cannot assert a container already exists.")
        if self.observed_container_id is not None and (
            self.phase != "unknown" or self.container_id is not None
        ):
            raise ValueError("Partial create replies are uncertainty observations only.")
        if self.phase not in {"create_intent", "unknown"} and self.container_id is None:
            raise ValueError("Observed lifecycle facts require exact container identity.")
        if self.phase not in {"create_intent", "unknown"} and self.created_at is None:
            raise ValueError("Observed lifecycle facts require creation identity.")
        if (self.phase == "exited") != (self.exit_code is not None):
            raise ValueError("Only observed exit carries an exit code.")
        return self


class DockerLaunchScope(_Model):
    project_id: UUID
    canonical_root: Path
    claim_id: UUID

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)


class DockerLaunchSnapshot(_Model):
    """Historical observations only; never a controller handle or replay permission."""

    intent: DockerLaunchIntent
    phases: tuple[
        Literal["create_intent", "created", "start_intent", "exited", "cleaned", "unknown"], ...
    ] = Field(max_length=6)
    container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: str | None = Field(
        default=None, min_length=20, max_length=64, pattern=r"^[0-9TZ:.+\-]+$"
    )
    observed_container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    exit_code: StrictInt | None = Field(default=None, ge=0, le=255)
    claim_state: Literal["claimed", "unknown", "finished"]
    uncertain: bool

    @model_validator(mode="after")
    def shape(self) -> Self:
        if (
            (self.container_id is None) != (self.created_at is None)
            or len(self.phases) != len(set(self.phases))
            or self.uncertain != (self.claim_state == "unknown" or "unknown" in self.phases)
            or (("exited" in self.phases) != (self.exit_code is not None))
        ):
            raise ValueError("Inconsistent durable lifecycle observations.")
        return self

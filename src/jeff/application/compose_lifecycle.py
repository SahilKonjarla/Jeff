"""Slice 3 service manifests and journal facts, never mutation authority."""

from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from jeff.application.compose_status import ComposeName
from jeff.application.execution import DockerCLIIdentity
from jeff.domain.execution import AuthorityState, fingerprint, public_text

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ImageID = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Phase = Literal["intent", "created", "running", "stopped", "unknown"]


class _Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ServiceTarget(_Fact):
    project_id: UUID
    root: Path
    project: ComposeName
    service: ComposeName
    engine_id: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(pattern=r"^unix:///[^\r\n\x00]+$", max_length=1023)

    @field_validator("root")
    @classmethod
    def absolute_root(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("Service target requires an absolute canonical project root.")
        public_text(str(value), 1023)
        return value

    @field_validator("engine_id", "endpoint")
    @classmethod
    def nonsecret(cls, value: str) -> str:
        return public_text(value, 1023)


class HealthRelayGrant(_Fact):
    """Fixed health-only relay on a daemon-owned, dynamically allocated Mac port."""

    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: Literal[0] = 0
    method: Literal["GET"] = "GET"
    path: Literal["/health"] = "/health"
    container_port: Literal[8000] = 8000


class ServiceStart(_Fact):
    """Exact resolved start proposal. A later adapter must verify enforcement."""

    action: Literal["start"] = "start"
    target: ServiceTarget
    configuration_digest: Digest
    image_id: ImageID
    platform: Literal["linux/arm64", "linux/amd64"]
    network: Literal["internal_bridge"] = "internal_bridge"
    docker_published_ports: Literal[False] = False
    external_egress: Literal[False] = False
    health_relay: HealthRelayGrant = HealthRelayGrant()
    memory_bytes: StrictInt = Field(gt=0, le=2**40)
    nano_cpus: StrictInt = Field(gt=0, le=64_000_000_000)
    pids_limit: StrictInt = Field(gt=0, le=4096)

    @property
    def digest(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class ServiceLifecycleSettings(_Fact):
    """Private opt-in for one reviewed synthetic fixture; not a launch grant."""

    start: ServiceStart
    binary: DockerCLIIdentity
    config: Path
    home: Path
    fixture_source: str = Field(min_length=1, max_length=8192, repr=False)

    @field_validator("config", "home")
    @classmethod
    def absolute(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("Private Docker paths must be absolute.")
        return value


class ResourceIdentity(_Fact):
    id: Digest
    created_at: str = Field(pattern=r"^[0-9TZ:.+\-]{20,64}$")


class ServiceResources(_Fact):
    """Observed identities from the creating worker; deserialization is not adoption."""

    container: ResourceIdentity
    network: ResourceIdentity


class _ExistingService(_Fact):
    target: ServiceTarget
    start_job_id: UUID
    start_manifest_digest: Digest
    resources: ServiceResources

    @property
    def digest(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class ServiceStop(_ExistingService):
    """A separate proposal for stopping the exact original resources."""

    action: Literal["stop"] = "stop"


class ServiceRestart(_ExistingService):
    """In-place restart of the same running container with the original policy."""

    action: Literal["restart"] = "restart"
    original: ServiceStart

    @model_validator(mode="after")
    def original_review(self) -> Self:
        if self.original.target != self.target or self.original.digest != self.start_manifest_digest:
            raise ValueError("Restart requires the original reviewed start manifest.")
        return self


ExistingServiceAction = ServiceStop | ServiceRestart
ServiceAction = Annotated[ServiceStart | ServiceStop | ServiceRestart, Field(discriminator="action")]


class ServiceAuthorityOperation(_Fact):
    """Internal request to the same writer as guarded execution approvals."""

    operation: Literal["propose", "approve", "deny", "show", "claim"]
    request_id: UUID
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    approval_id: UUID | None = None
    manifest: ServiceAction | None = None
    current_manifest_digest: Digest | None = None

    @model_validator(mode="after")
    def exact_operation(self) -> Self:
        if (
            (self.operation == "propose") != (self.manifest is not None)
            or (self.operation != "propose") != (self.approval_id is not None)
            or (self.operation in {"propose", "approve", "claim"})
            != (self.current_manifest_digest is not None)
        ):
            raise ValueError("Service authority operation is incomplete.")
        return self


class ServiceAuthorityRecord(_Fact):
    kind: Literal["compose_service_v1"] = "compose_service_v1"
    proposal_id: UUID
    approval_id: UUID
    job_id: UUID
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    manifest: ServiceAction
    manifest_digest: Digest
    state: AuthorityState
    created_at_ms: StrictInt = Field(ge=0)
    expires_at_ms: StrictInt = Field(ge=0)
    claim_id: UUID | None = None
    claim_state: Literal["claimed", "unknown", "finished"] | None = None
    claimed_at_ms: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (
            self.manifest_digest != self.manifest.digest
            or self.project_id != self.manifest.target.project_id
            or self.canonical_root != self.manifest.target.root
            or self.expires_at_ms <= self.created_at_ms
            or any(
                (value is not None) != (self.state == AuthorityState.CONSUMED)
                for value in (self.claim_id, self.claim_state, self.claimed_at_ms)
            )
            or (
                self.claimed_at_ms is not None
                and not self.created_at_ms <= self.claimed_at_ms < self.expires_at_ms
            )
        ):
            raise ValueError("Service authority disagrees with its target or claim.")
        return self


class ServiceAuthorityResult(_Fact):
    record: ServiceAuthorityRecord
    launch_claim: UUID | None = None

    @model_validator(mode="after")
    def fresh_claim(self) -> Self:
        if self.launch_claim is not None and (
            self.launch_claim != self.record.claim_id or self.record.claim_state != "claimed"
        ):
            raise ValueError("Fresh claim does not match the record.")
        return self


class ServiceAttempt(_Fact):
    """Immutable binding to a reviewed job; the real claim is checked elsewhere."""

    job_id: UUID
    claim_id: UUID
    worker_instance_id: UUID
    manifest: ServiceAction
    reviewed_digest: Digest

    def matches_review(self) -> bool:
        return self.manifest.digest == self.reviewed_digest

    @property
    def relay_start(self) -> ServiceStart | None:
        if isinstance(self.manifest, ServiceRestart):
            return self.manifest.original
        return self.manifest if isinstance(self.manifest, ServiceStart) else None

    @property
    def relay_job_id(self) -> UUID:
        return self.job_id if isinstance(self.manifest, ServiceStart) else self.manifest.start_job_id


class ServiceEvent(_Fact):
    phase: Phase
    resources: ServiceResources | None = None


def append_service_event(
    attempt: ServiceAttempt, history: tuple[ServiceEvent, ...], event: ServiceEvent
) -> tuple[ServiceEvent, ...]:
    """Validate a bounded journal including prior facts; never dispatch a mutation."""
    if not attempt.matches_review() or len(history) >= 3:
        raise ValueError("Service attempt does not match its review or is already complete.")
    previous: Phase | None = None
    manifest = attempt.manifest
    resources = None if isinstance(manifest, ServiceStart) else manifest.resources
    for observation in (*history, event):
        phase = observation.phase
        if previous is None:
            allowed: tuple[Phase, ...] = ("intent",)
        elif previous == "intent":
            next_phase: Phase = (
                "created" if isinstance(manifest, ServiceStart)
                else "running" if isinstance(manifest, ServiceRestart) else "stopped"
            )
            allowed = (next_phase, "unknown")
        elif previous == "created" and isinstance(manifest, ServiceStart):
            allowed = ("running", "unknown")
        else:
            allowed = ()
        if phase not in allowed:
            raise ValueError("Invalid service journal transition; do not replay an attempt.")
        if phase == "created":
            if observation.resources is None:
                raise ValueError("Creation requires exact observed resource identities.")
            resources = observation.resources
        elif phase in ("running", "stopped"):
            if resources is None or observation.resources != resources:
                raise ValueError("Completion must corroborate the exact resource identities.")
        elif observation.resources is not None:
            raise ValueError("Intent and unknown events cannot assert new ownership facts.")
        previous = phase
    return (*history, event)


def stop_matches_start(
    stop: ExistingServiceAction, start: ServiceAttempt, history: tuple[ServiceEvent, ...]
) -> bool:
    """Check durable provenance only; caller must still claim fresh action authority."""
    if not isinstance(start.manifest, ServiceStart) or len(history) != 3:
        return False
    try:
        checked = append_service_event(start, history[:-1], history[-1])
    except ValueError:
        return False
    return (
        checked[-1].phase == "running"
        and stop.resources == checked[-1].resources
        and stop.start_job_id == start.job_id
        and stop.start_manifest_digest == start.reviewed_digest
        and stop.target == start.manifest.target
    )

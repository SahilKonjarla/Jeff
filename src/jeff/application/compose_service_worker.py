"""Internal service job orchestration; no public route or concrete Docker backend."""

import time
from pathlib import Path
from typing import Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, StrictInt, model_validator

from jeff.application.compose_lifecycle import (
    Digest,
    ServiceAction,
    ServiceAttempt,
    ServiceAuthorityOperation,
    ServiceAuthorityResult,
    ServiceEvent,
    ServiceResources,
    ServiceRestart,
    ServiceStart,
    ServiceStop,
    _Fact,
)
from jeff.domain.execution import AuthorityState


class ServiceWork(_Fact):
    request_id: UUID
    project_id: UUID
    canonical_root: Path
    approval_id: UUID
    reviewed_digest: Digest


class HealthRelayEndpoint(_Fact):
    """Live daemon-local observation; never part of a journal event."""

    owner_instance_id: UUID
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: StrictInt = Field(ge=1, le=65535)
    path: Literal["/health"] = "/health"


class ServiceWorkResult(_Fact):
    request_id: UUID
    outcome: Literal["refused", "running", "stopped", "unknown"]
    # An acknowledged unknown is distinct from an unconfirmed journal write.
    terminal_recorded: bool = False
    health_relay: HealthRelayEndpoint | None = None

    @model_validator(mode="after")
    def confirmed_start_only(self) -> Self:
        if self.health_relay is not None and (self.outcome != "running" or not self.terminal_recorded):
            raise ValueError("Only a confirmed start can report a live relay.")
        return self


class ServiceRelays(Protocol):
    """Daemon-lifetime owner: activation must be bounded and clean up failed opens."""

    def open(
        self, attempt: ServiceAttempt, resources: ServiceResources, *, deadline: float
    ) -> HealthRelayEndpoint: ...

    def close(self, start_job_id: UUID, *, deadline: float) -> None: ...


class ServiceJournal(Protocol):
    def service_authority(
        self, operation: ServiceAuthorityOperation, *, deadline: float
    ) -> ServiceAuthorityResult: ...

    def record_service(
        self, attempt: ServiceAttempt, event: ServiceEvent, *, deadline: float
    ) -> None: ...


class ServiceBackend(Protocol):
    """Implementations must enforce the manifest and reverify identity at mutation time."""

    def verify(self, manifest: ServiceAction, *, deadline: float) -> Digest:
        """Read-only: verify target, limits and collision/ownership evidence or refuse."""
        ...

    def create(self, attempt: ServiceAttempt, *, deadline: float) -> ServiceResources:
        """Return exact created identities or raise without an automatic retry."""
        ...

    def start(
        self, attempt: ServiceAttempt, resources: ServiceResources, *, deadline: float
    ) -> ServiceResources:
        """Confirm running, isolation, and no Docker publication for the same resources."""
        ...

    def restart(self, attempt: ServiceAttempt, *, deadline: float) -> ServiceResources:
        """Restart only the running original container; verify it running afterward."""
        ...

    def stop(self, attempt: ServiceAttempt, *, deadline: float) -> ServiceResources:
        """Stop only the verified original container, confirming stopped without removal."""
        ...


class ServiceWorker:
    def __init__(
        self, journal: ServiceJournal, backend: ServiceBackend, owner: UUID,
        *, relays: ServiceRelays | None = None,
    ) -> None:
        self._journal, self._backend, self._owner = journal, backend, owner
        self._relays = relays

    def execute(self, call: ServiceWork, deadline: float) -> ServiceWorkResult:
        def reply(
            outcome: Literal["refused", "running", "stopped", "unknown"],
            *, recorded: bool = False, relay: HealthRelayEndpoint | None = None,
        ) -> ServiceWorkResult:
            return ServiceWorkResult(
                request_id=call.request_id, outcome=outcome, terminal_recorded=recorded,
                health_relay=relay,
            )

        def operation(kind: Literal["show", "claim"]) -> ServiceAuthorityOperation:
            return ServiceAuthorityOperation(
                operation=kind,
                request_id=call.request_id,
                project_id=call.project_id,
                canonical_root=call.canonical_root,
                owner_instance_id=self._owner,
                approval_id=call.approval_id,
                current_manifest_digest=call.reviewed_digest if kind == "claim" else None,
            )

        try:
            reviewed = self._journal.service_authority(operation("show"), deadline=deadline).record
            if (
                reviewed.state != AuthorityState.APPROVED
                or reviewed.owner_instance_id != self._owner
                or reviewed.approval_id != call.approval_id
                or (reviewed.project_id, reviewed.canonical_root)
                != (call.project_id, call.canonical_root)
                or reviewed.manifest_digest != call.reviewed_digest
                or time.monotonic() >= deadline
                or self._backend.verify(reviewed.manifest, deadline=deadline) != call.reviewed_digest
                or time.monotonic() >= deadline
            ):
                return reply("refused")
        except Exception:
            return reply("refused")  # No claim or mutation requested.

        try:
            claimed = self._journal.service_authority(operation("claim"), deadline=deadline)
            record = claimed.record
            if claimed.launch_claim is None:
                return reply("refused")  # Facts-only replay grants no new work.
            if (
                record.state != AuthorityState.CONSUMED
                or record.claim_id != claimed.launch_claim
                or record.claim_state != "claimed"
                or record.model_dump(exclude={"state", "claim_id", "claim_state", "claimed_at_ms"})
                != reviewed.model_dump(exclude={"state", "claim_id", "claim_state", "claimed_at_ms"})
            ):
                return reply("unknown")
            attempt = ServiceAttempt(
                job_id=record.job_id,
                claim_id=claimed.launch_claim,
                worker_instance_id=self._owner,
                manifest=record.manifest,
                reviewed_digest=record.manifest_digest,
            )
            self._journal.record_service(attempt, ServiceEvent(phase="intent"), deadline=deadline)
        except Exception:
            return reply("unknown")  # An uncertain journal acknowledgement is never replayed.

        def uncertain() -> ServiceWorkResult:
            try:
                self._journal.record_service(
                    attempt, ServiceEvent(phase="unknown"), deadline=deadline
                )
            except Exception:
                return reply("unknown")
            return reply("unknown", recorded=True)

        manifest = attempt.manifest
        if isinstance(manifest, ServiceStart):
            try:
                if time.monotonic() >= deadline:
                    return uncertain()
                created = self._backend.create(attempt, deadline=deadline)
            except Exception:
                return uncertain()
            try:
                self._journal.record_service(
                    attempt, ServiceEvent(phase="created", resources=created), deadline=deadline
                )
            except Exception:
                return reply("unknown")  # Don't start after a lost Create journal reply.
            try:
                if time.monotonic() >= deadline:
                    return uncertain()
                resources = self._backend.start(attempt, created, deadline=deadline)
            except Exception:
                return uncertain()
            terminal: Literal["running", "stopped"] = "running"
        elif isinstance(manifest, (ServiceStop, ServiceRestart)):
            try:
                if time.monotonic() >= deadline:
                    return uncertain()
                if self._relays is not None:
                    self._relays.close(manifest.start_job_id, deadline=deadline)
                if time.monotonic() >= deadline:
                    return uncertain()
                resources = (
                    self._backend.restart(attempt, deadline=deadline)
                    if isinstance(manifest, ServiceRestart)
                    else self._backend.stop(attempt, deadline=deadline)
                )
            except Exception:
                return uncertain()
            terminal = "running" if isinstance(manifest, ServiceRestart) else "stopped"
        else:
            return uncertain()
        try:
            self._journal.record_service(
                attempt, ServiceEvent(phase=terminal, resources=resources), deadline=deadline
            )
        except Exception:
            return reply("unknown")
        relay = None
        if terminal == "running" and self._relays is not None and time.monotonic() < deadline:
            try:
                candidate = self._relays.open(attempt, resources, deadline=deadline)
                if candidate.owner_instance_id == self._owner:
                    relay = candidate
            except Exception:
                pass  # Relay failure does not change the acknowledged service outcome.
        return reply(terminal, recorded=True, relay=relay)

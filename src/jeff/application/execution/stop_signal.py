"""Private synchronized live-owner cancellation and fixed deadline latch."""

import math
import threading
import time
from typing import Literal
from uuid import UUID

from jeff.domain.execution import AuthorityState

from .authority import AuthorityRecord
from .completion import DockerExecutionResult
from .docker_faults import DockerBackendFault
from .requests import ExecutionInspectionScope, ExecutionLivePhase
from .stop_records import ExecutionStopRecord, ExecutionStopRequest


class ExecutionStopSignal:
    """Private live-owner latch. Stored observations cannot construct or restart its timers."""

    def __init__(self, authority: AuthorityRecord, *, job_deadline: float) -> None:
        if not math.isfinite(job_deadline):
            raise ValueError("A finite original deadline is required.")
        self.authority = authority
        self.scope = ExecutionInspectionScope(
            project_id=authority.project_id,
            canonical_root=authority.canonical_root,
            owner_instance_id=authority.owner_instance_id,
            approval_id=authority.approval_id,
        )
        self._lock = threading.Lock()
        self._request: ExecutionStopRequest | None = None
        self._confirmed: ExecutionStopRecord | None = None
        self._running = False
        self._claimed = authority.state == AuthorityState.CONSUMED
        self._completion_known = False
        self._grace = job_deadline + 5
        self._cleanup = job_deadline + 10
        self._recording = job_deadline + 15

    @property
    def completion_known(self) -> bool:
        with self._lock:
            return self._completion_known

    def finished(self, result: ExecutionStopRecord | DockerExecutionResult) -> None:
        if isinstance(result, DockerExecutionResult):
            self.require_scope(result.owned.intent.record)
            known = result.disposition == "completed"
        else:
            if (
                result.scope != self.scope
                or result.manifest_digest != self.authority.manifest_digest
            ):
                raise DockerBackendFault("docker_authority_required")
            known = (
                result.terminal is not None
                and result.terminal.outcome != "unknown"
                and result.terminal.cleanup_verified
            )
        with self._lock:
            self._completion_known = known

    def require_scope(self, authority: AuthorityRecord) -> None:
        if (
            authority.approval_id,
            authority.project_id,
            authority.canonical_root,
            authority.owner_instance_id,
            authority.manifest_digest,
        ) != (
            self.scope.approval_id,
            self.scope.project_id,
            self.scope.canonical_root,
            self.scope.owner_instance_id,
            self.authority.manifest_digest,
        ):
            raise DockerBackendFault("docker_authority_required")

    def request(
        self,
        request_id: UUID,
        reason: Literal["cancel", "timeout", "shutdown"],
        *,
        shutdown_deadline: float | None = None,
    ) -> None:
        request = ExecutionStopRequest(
            **self.scope.model_dump(),
            request_id=request_id,
            manifest_digest=self.authority.manifest_digest,
            reason=reason,
        )
        if shutdown_deadline is not None and not math.isfinite(shutdown_deadline):
            raise ValueError("A finite shutdown cap is required.")
        with self._lock:
            if self._request is None:
                now = time.monotonic()
                self._request = request
                self._grace = min(self._grace, now + 5)
                self._cleanup = min(self._cleanup, now + 10)
                self._recording = min(self._recording, now + 15)
            if shutdown_deadline is not None:
                self._recording = min(self._recording, shutdown_deadline)
                self._cleanup = min(self._cleanup, shutdown_deadline - 5)
            self._grace = min(self._grace, self._cleanup)

    @property
    def requested(self) -> bool:
        with self._lock:
            return self._request is not None

    @property
    def phase(self) -> ExecutionLivePhase:
        with self._lock:
            return (
                "stop_requested"
                if self._request is not None
                else (
                    "running_observed"
                    if self._running
                    else "admitted"
                    if self._claimed
                    else "claim_pending"
                )
            )

    def mark_claimed(self) -> None:
        with self._lock:
            self._claimed = True

    def mark_running(self) -> None:
        with self._lock:
            self._running = True

    def check(self) -> None:
        if self.requested:
            raise DockerBackendFault("docker_cancelled")

    def request_record(self) -> ExecutionStopRequest:
        with self._lock:
            if self._request is None:
                raise DockerBackendFault("docker_authority_required")
            if self._confirmed is not None:
                return ExecutionStopRequest(
                    **self.scope.model_dump(),
                    request_id=self._confirmed.request_id,
                    reason=self._confirmed.reason,
                    manifest_digest=self.authority.manifest_digest,
                )
            return self._request

    def confirm(self, record: ExecutionStopRecord) -> None:
        with self._lock:
            if (
                self._request is None
                or record.scope != self.scope
                or (record.manifest_digest != self.authority.manifest_digest)
            ):
                raise DockerBackendFault("docker_authority_required")
            self._confirmed = record  # Canonical first SQL facts; deadlines do not move.

    @property
    def confirmed(self) -> ExecutionStopRecord | None:
        with self._lock:
            return self._confirmed

    def cap_cleanup(self, deadline: float) -> None:
        with self._lock:
            self._cleanup = min(self._cleanup, deadline)
            self._grace = min(self._grace, self._cleanup)
            self._recording = min(self._recording, deadline + 5)

    @property
    def grace_deadline(self) -> float:
        with self._lock:
            return self._grace

    @property
    def cleanup_deadline(self) -> float:
        with self._lock:
            return self._cleanup

    @property
    def recording_deadline(self) -> float:
        with self._lock:
            return self._recording

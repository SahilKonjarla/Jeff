"""Synchronous owning-worker admission, replay and execution lifetime."""

import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from jeff.application.execution import (
    AuthorityRecord,
    DockerBackendFault,
    DockerExecutionResult,
    DockerOutputCapture,
    DockerTargetDeclaration,
    ExecutionAdmission,
    ExecutionStartRefused,
    ExecutionStopRecord,
    ExecutionStopSignal,
    GuardedExecutionManifest,
)
from jeff.application.execution.log_tail import LiveLogTail
from jeff.application.execution.pytest_report import pytest_run_token
from jeff.domain.execution import AuthorityState

from .binding import _active, _checked, _deadline, _load, _operation, _project
from .cancellation import _finish_without_handle, _persist_live_stop
from .lifecycle import _execute_claim

if TYPE_CHECKING:
    from .coordinator import ExecutionCoordinator


def _capture(self: "ExecutionCoordinator", record: AuthorityRecord) -> DockerOutputCapture:
    manifest = record.manifest
    limit = manifest.profile.output_bytes
    token = pytest_run_token(manifest.argv)
    if token is None:
        return self.capture(limit)
    if not isinstance(manifest, GuardedExecutionManifest) or self.pytest_capture is None:
        raise ExecutionStartRefused("docker_authority_required")
    return self.pytest_capture(limit, token, tuple(item.path for item in manifest.inputs.files))


def start(
    self: "ExecutionCoordinator",
    directory: Path,
    approval_id: UUID,
    expected_manifest_digest: str,
    *,
    request_id: UUID,
    admission_deadline: float,
    on_admitted: Callable[[ExecutionAdmission], None],
) -> DockerExecutionResult | AuthorityRecord | ExecutionStopRecord:
    """Owned-worker entry: acknowledge a committed claim, then continue locally.

    The observer receives no permission. Its failure cannot cancel admitted
    work. This method remains synchronous for its owning worker; no thread,
    queue, RPC handler or automatic retry is created here. Replays return facts.
    """
    with _active(self, admission_deadline):
        if not callable(on_admitted):
            raise DockerBackendFault("docker_authority_required")
        try:
            project = _project(self, directory, admission_deadline)
            record = _load(self, project, approval_id, expected_manifest_digest, admission_deadline)
        except DockerBackendFault as error:
            if error.code == "docker_authority_required":
                # This invocation has not entered claim. Do not classify later faults by code.
                raise ExecutionStartRefused("docker_authority_required") from None
            raise
        if record.state not in (AuthorityState.APPROVED, AuthorityState.CONSUMED):
            raise ExecutionStartRefused("docker_authority_required")
        capture: DockerOutputCapture | None = None
        job_deadline = time.monotonic() + record.manifest.profile.timeout_seconds
        stop = None
        if self.stop_store is not None and record.state == AuthorityState.APPROVED:
            if not self.cancellation_available:
                raise DockerBackendFault("docker_authority_required")
            stop = ExecutionStopSignal(record, job_deadline=job_deadline)
            with self._control_mutex:
                self._live = stop
                shutdown = self._shutdown_deadline
            if shutdown is not None:
                stop.request(uuid4(), "shutdown", shutdown_deadline=shutdown)
        try:
            if stop is not None and stop.requested:
                return _finish_without_handle(self, stop)
            claim_deadline = min(admission_deadline, job_deadline)
            declaration: DockerTargetDeclaration | None = None
            if record.state == AuthorityState.APPROVED:
                declaration = _checked(self, project, record, claim_deadline, stop)
                capture = _capture(self, record)
                if (
                    type(capture.retention_limit) is not int
                    or capture.retention_limit != record.manifest.profile.output_bytes
                ):
                    raise DockerBackendFault("docker_unsafe_configuration")
            if stop is not None and stop.requested:
                if capture is not None:
                    capture.seal(abort=True)
                return _finish_without_handle(self, stop)
            claim = self.authority.authority(
                _operation(
                    self,
                    project,
                    operation="claim",
                    request_id=request_id,
                    approval_id=approval_id,
                    current_manifest_digest=record.manifest.fingerprint,
                ),
                deadline=claim_deadline,
            )
            if (
                claim.record.approval_id != record.approval_id
                or claim.record.manifest != record.manifest
                or claim.record.owner_instance_id != self.instance_id
                or claim.record.state != AuthorityState.CONSUMED
            ):
                raise DockerBackendFault("docker_authority_required")
            replayed = claim.launch_claim is None
            if not replayed and (
                declaration is None
                or capture is None
                or claim.launch_claim != claim.record.claim_id
                or claim.record.claim_state != "claimed"
            ):
                raise DockerBackendFault("docker_authority_required")
            if not replayed and capture is not None and self.attach_log_tail is not None:
                tail = LiveLogTail()
                self.attach_log_tail(capture, tail)
                with self._control_mutex:
                    self._log_tail = (record.job_id, tail)
            if stop is not None and not replayed:
                stop.mark_claimed()
            observation = ExecutionAdmission.model_validate(
                {
                    "request_id": request_id,
                    "project_id": project.project_id,
                    "canonical_root": project.root,
                    "approval_id": claim.record.approval_id,
                    "job_id": claim.record.job_id,
                    "manifest_digest": claim.record.manifest_digest,
                    "claim_state": claim.record.claim_state,
                    "replayed": replayed,
                }
            )
            try:
                on_admitted(observation)
            except Exception:
                pass  # Observer delivery is not worker ownership or cancellation.
            if replayed:
                if capture is not None:
                    capture.seal(abort=True)
                return claim.record
            if capture is None or declaration is None:
                raise DockerBackendFault("docker_authority_required")
            return _execute_claim(
                self, claim, declaration, capture, job_deadline, job_deadline + 10, stop
            )
        except Exception as error:
            if capture is not None:
                try:
                    capture.seal(abort=True)
                except Exception:
                    pass
            if (
                stop is not None
                and isinstance(error, DockerBackendFault)
                and error.code == "docker_cancelled"
            ):
                return _finish_without_handle(self, stop)
            raise
        finally:
            # A native stop can arrive after natural exit but before this
            # worker returns. Persist that intent without relabelling v1 completion.
            if stop is not None and stop.requested and stop.confirmed is None:
                _persist_live_stop(self, stop)
            with self._control_mutex:
                if self._live is stop:
                    if stop is not None and (
                        not stop.completion_known or (stop.requested and stop.confirmed is None)
                    ):
                        self._execution_uncertain = True
                    self._live = None


def run(
    self: "ExecutionCoordinator",
    directory: Path,
    approval_id: UUID,
    expected_manifest_digest: str,
    *,
    deadline: float,
) -> DockerExecutionResult:
    """Run synchronously for internal callers without publishing admission early.

    Public starts use ``start`` so the owning worker outlives its admission
    observer. Neither entry point can reconstruct a lost first-claim token.
    """
    with _active(self, deadline):
        project = _project(self, directory, deadline)
        record = _load(self, project, approval_id, expected_manifest_digest, deadline)
        if record.state != AuthorityState.APPROVED:
            raise DockerBackendFault("docker_authority_required")
        declaration = _checked(self, project, record, deadline)
        job_deadline = min(deadline, time.monotonic() + record.manifest.profile.timeout_seconds)
        cleanup_deadline = job_deadline + 10
        capture = _capture(self, record)
        try:
            if (
                type(capture.retention_limit) is not int
                or capture.retention_limit != record.manifest.profile.output_bytes
            ):
                raise DockerBackendFault("docker_unsafe_configuration")
            _deadline(job_deadline)
            claim = self.authority.authority(
                _operation(
                    self,
                    project,
                    operation="claim",
                    approval_id=approval_id,
                    current_manifest_digest=record.manifest.fingerprint,
                ),
                deadline=job_deadline,
            )
            if (
                claim.record.approval_id != record.approval_id
                or claim.record.manifest != record.manifest
                or claim.record.owner_instance_id != self.instance_id
            ):
                raise DockerBackendFault("docker_authority_required")
            if claim.launch_claim is None:
                raise DockerBackendFault("docker_authority_required")
            # Never cache/return the ephemeral first-response launch permission.
            result = _execute_claim(
                self, claim, declaration, capture, job_deadline, cleanup_deadline
            )
            if not isinstance(result, DockerExecutionResult):
                raise DockerBackendFault("docker_journal_unconfirmed")
            return result
        except Exception:
            try:
                capture.seal(abort=True)
            except Exception:
                pass
            raise

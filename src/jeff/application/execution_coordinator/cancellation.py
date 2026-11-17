"""Live stop registration and durable intent; never a second execution owner."""

import time
from concurrent.futures import Future
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from jeff.application.execution import (
    CancellableDockerController,
    CancellableDockerVerifier,
    DockerBackendFault,
    ExecutionCancelCall,
    ExecutionCancellationStore,
    ExecutionLivePhase,
    ExecutionStopObservation,
    ExecutionStopRecord,
    ExecutionStopSignal,
    ExecutionStopTerminal,
    ReviewFault,
)
from jeff.application.storage import StorageFault

if TYPE_CHECKING:
    from .coordinator import ExecutionCoordinator


def shutdown_confirmed(self: "ExecutionCoordinator") -> bool:
    with self._control_mutex:
        return not self._execution_uncertain and (self._live is None or self._live.completion_known)


def cancellation_available(self: "ExecutionCoordinator") -> bool:
    return (
        isinstance(self.stop_store, ExecutionCancellationStore)
        and isinstance(self.controller, CancellableDockerController)
        and isinstance(self.verifier, CancellableDockerVerifier)
    )


def submit_cancel(
    self: "ExecutionCoordinator", call: ExecutionCancelCall, *, deadline: float
) -> Future[ExecutionStopRecord]:
    if not self.cancellation_available or not isinstance(
        self.stop_store, ExecutionCancellationStore
    ):
        raise DockerBackendFault("docker_authority_required")
    with self._control_mutex:
        live = self._live
    if live is not None and (
        live.scope.approval_id != call.approval_id
        or live.authority.manifest_digest != call.reviewed_manifest_digest
    ):
        live = None
    if live is not None:
        live.request(call.request_id, "cancel")  # Provisional fence, never durable acceptance.
    future = self.stop_store.submit_cancel(call, deadline=deadline)
    if live is not None:

        def committed(done: Future[ExecutionStopRecord]) -> None:
            try:
                assert live is not None
                live.confirm(done.result())
            except Exception:
                pass  # SQL failure never clears the provisional fence or restarts work.

        future.add_done_callback(committed)
    return future


def begin_shutdown(self: "ExecutionCoordinator", deadline: float) -> None:
    with self._control_mutex:
        self._shutdown_deadline = min(self._shutdown_deadline or deadline, deadline)
        live = self._live
        cap = self._shutdown_deadline
    if live is not None:
        live.request(uuid4(), "shutdown", shutdown_deadline=cap)


def execution_phase(self: "ExecutionCoordinator", approval_id: UUID) -> ExecutionLivePhase | None:
    with self._control_mutex:
        live = self._live
    return live.phase if live is not None and live.scope.approval_id == approval_id else None


def _persist_live_stop(
    self: "ExecutionCoordinator", stop: ExecutionStopSignal
) -> ExecutionStopRecord | None:
    assert self.stop_store is not None
    if stop.confirmed is not None:
        return stop.confirmed
    try:
        record = self.stop_store.request_stop(
            stop.request_record(), deadline=min(stop.recording_deadline, time.monotonic() + 1)
        )
        stop.confirm(record)
        return record
    except (DockerBackendFault, ReviewFault, StorageFault):
        return None  # Owned cleanup still runs; no invented durable acceptance.


def _finish_without_handle(
    self: "ExecutionCoordinator", stop: ExecutionStopSignal
) -> ExecutionStopRecord:
    assert self.stop_store is not None
    record = _persist_live_stop(self, stop)
    if record is None:
        raise DockerBackendFault("docker_journal_unconfirmed")
    if record.terminal is not None:
        stop.finished(record)
        return record
    observation = ExecutionStopObservation(
        scope=stop.scope,
        request_id=record.request_id,
        manifest_digest=record.manifest_digest,
        terminal=ExecutionStopTerminal(
            outcome="timed_out" if record.reason == "timeout" else "cancelled",
            evidence="not_started",
            cleanup_verified=True,
        ),
    )
    try:
        result = self.stop_store.finish_stop(observation, deadline=stop.recording_deadline)
        stop.finished(result)
        return result
    except ReviewFault as error:
        if error.code != "execution_outcome_unknown":
            raise
        # SQL positively rejected no-launch proof; this is not an ACK-loss retry.
        return self.stop_store.finish_stop(
            ExecutionStopObservation(
                scope=stop.scope,
                request_id=record.request_id,
                manifest_digest=record.manifest_digest,
                terminal=ExecutionStopTerminal(
                    outcome="unknown", evidence="uncertain", cleanup_verified=False
                ),
            ),
            deadline=stop.recording_deadline,
        )

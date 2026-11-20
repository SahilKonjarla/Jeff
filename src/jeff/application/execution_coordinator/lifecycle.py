"""First-confirmed-claim execution and exact-owned completion or stop finalization."""

import time
from typing import TYPE_CHECKING

from jeff.application.execution import (
    AuthorityResult,
    CancellableDockerController,
    DockerBackendFault,
    DockerExecutionResult,
    DockerOutputCapture,
    DockerTargetDeclaration,
    ExecutionStopRecord,
    ExecutionStopSignal,
)

from .cancellation import _finish_without_handle, _persist_live_stop

if TYPE_CHECKING:
    from .coordinator import ExecutionCoordinator


def _execute_claim(
    self: "ExecutionCoordinator",
    claim: AuthorityResult,
    declaration: DockerTargetDeclaration,
    capture: DockerOutputCapture,
    job_deadline: float,
    cleanup_deadline: float,
    stop: ExecutionStopSignal | None = None,
) -> DockerExecutionResult | ExecutionStopRecord:
    if stop is not None:
        if self.stop_store is None or not isinstance(self.controller, CancellableDockerController):
            raise DockerBackendFault("docker_authority_required")
        handle = None
        try:
            stop.check()
            handle = self.controller.create(
                claim,
                declaration,
                deadline=job_deadline,
                cleanup_deadline=cleanup_deadline,
                stop=stop,
            )
            self.controller.run(
                handle,
                capture,
                deadline=job_deadline,
                cleanup_deadline=cleanup_deadline,
                stop=stop,
            )
        except DockerBackendFault:
            if not stop.requested:
                raise
            _persist_live_stop(self, stop)
            if handle is not None:
                stopped = self.controller.finish_stop_owned(handle, capture, stop, self.stop_store)
                stop.finished(stopped)
                return stopped
            capture.seal(abort=True)
            return _finish_without_handle(self, stop)
        completed = self.controller.complete(
            handle,
            capture,
            self.results,
            deadline=min(time.monotonic() + 5, stop.recording_deadline),
        )
        stop.finished(completed)
        return completed
    # Only the local first confirmed response reaches this mutation path.
    handle = self.controller.create(
        claim, declaration, deadline=job_deadline, cleanup_deadline=cleanup_deadline
    )
    try:
        self.controller.run(
            handle, capture, deadline=job_deadline, cleanup_deadline=cleanup_deadline
        )
    except Exception:
        if not self.controller.terminate_remove_owned(handle, deadline=cleanup_deadline):
            raise DockerBackendFault("docker_mutation_unconfirmed") from None
        raise
    return self.controller.complete(
        handle,
        capture,
        self.results,
        deadline=min(time.monotonic() + 5, job_deadline + 15),
    )

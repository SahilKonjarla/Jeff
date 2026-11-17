"""One-shot attach/run observation and cleanup under the original operation guard."""

import time
from typing import TYPE_CHECKING
from uuid import uuid4

from jeff.application.execution import (
    CancellableDockerBackend,
    DockerBackendFault,
    DockerJobOutcome,
    DockerOutputSink,
    DockerOwnedContainer,
    DockerTargetDeclaration,
    ExecutionStopSignal,
)

from .policy import _mapping

if TYPE_CHECKING:
    from .controller import DockerJobController


def run(
    self: "DockerJobController",
    handle: DockerOwnedContainer,
    sink: DockerOutputSink,
    *,
    deadline: float,
    cleanup_deadline: float,
    stop: ExecutionStopSignal | None = None,
) -> DockerJobOutcome:
    with self._operation():
        deadline = min(
            deadline, time.monotonic() + handle.intent.record.manifest.profile.timeout_seconds
        )
        cleanup_deadline = min(cleanup_deadline, deadline + 10)
        self._budgets(deadline, cleanup_deadline)
        session = self._session(handle)
        if stop is not None:
            stop.require_scope(handle.intent.record)
        if session.start_attempted or session.removed or self._quarantined:
            raise DockerBackendFault("docker_authority_required")
        session.start_attempted = True  # Before journal/transport: uncertainty never reopens start.
        session.sink = sink
        if stop is not None:
            stop.cap_cleanup(cleanup_deadline)
        exit_code: int | None = None
        timed_out = unknown = False
        try:
            if stop is not None:
                stop.check()
            if (
                type(sink.retention_limit) is not int
                or sink.retention_limit != handle.intent.record.manifest.profile.output_bytes
            ):
                raise DockerBackendFault("docker_unsafe_configuration")
            target = self._verify(
                DockerTargetDeclaration.model_validate(self._declaration(handle)),
                deadline,
                stop,
            )
            if target.fingerprint != handle.intent.target.fingerprint:
                raise DockerBackendFault("docker_target_mismatch")
            self.inputs.check(
                handle.intent.inputs, handle.intent.record.manifest, deadline=deadline
            )
            raw = self._read(handle.intent, handle.container_id, deadline, handle.created_at)
            self._safe_config(handle, raw)
            before_start = _mapping(raw.get("State"))
            if before_start.get("Status") != "created" or before_start.get("Running") is not False:
                raise DockerBackendFault("docker_policy_mismatch")
            if stop is not None:
                stop.check()
            self._record(handle.intent, "start_intent", deadline=deadline, handle=handle)
            session.start_recorded = True
            if stop is not None:
                stop.check()
                if not isinstance(self.backend, CancellableDockerBackend):
                    raise DockerBackendFault("docker_unsafe_configuration")
                transport_status = self.backend.attach_cancellable(
                    handle.intent.target.endpoint,
                    handle.container_id,
                    sink,
                    stop,
                    lambda: self._observe_running(handle, stop, deadline),
                    deadline=deadline,
                )
            else:
                transport_status = self.backend.attach(
                    handle.intent.target.endpoint, handle.container_id, sink, deadline=deadline
                )
            raw = self._read(handle.intent, handle.container_id, deadline, handle.created_at)
            state = _mapping(raw.get("State"))
            code = state.get("ExitCode")
            if (
                state.get("Status") != "exited"
                or state.get("Running") is not False
                or type(code) is not int
                or not 0 <= code <= 255
            ):
                raise DockerBackendFault("docker_mutation_unconfirmed")
            exit_code = session.exit_code = code
            self._record(handle.intent, "exited", deadline=deadline, handle=handle, exit_code=code)
            if type(transport_status) is not int or transport_status not in (0, code):
                unknown = True  # Guest exit observed, but attached output/transport is unconfirmed.
                self._unknown(handle.intent, cleanup_deadline, handle)
        except Exception as error:
            if (
                stop is not None
                and isinstance(error, DockerBackendFault)
                and (
                    error.code == "docker_cancelled"
                    or (error.code == "docker_timeout" and time.monotonic() >= deadline)
                )
            ):
                if not stop.requested:
                    stop.request(uuid4(), "timeout")
                # Attach has returned/reaped before the owning coordinator persists
                # intent and re-enters this controller for cleanup. No reentrant mutations.
                raise DockerBackendFault("docker_cancelled") from None
            unknown = True
            timed_out = isinstance(error, DockerBackendFault) and error.code == "docker_timeout"
            self._unknown(handle.intent, cleanup_deadline, handle)
        cleaned = self._cleanup(session, cleanup_deadline)
        session.outcome = DockerJobOutcome(
            exit_code=exit_code,
            timed_out=timed_out,
            outcome_unknown=unknown or self._journal_lost or not cleaned,
            cleanup_verified=cleaned,
        )
        return session.outcome

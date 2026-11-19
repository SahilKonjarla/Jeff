"""JFG2 run within the existing controller/claim/journal; no independent executor."""

from typing import TYPE_CHECKING
from uuid import uuid4

from jeff.application.execution import (
    DockerBackendFault,
    DockerJobOutcome,
    DockerOutputSink,
    DockerOwnedContainer,
    DockerTargetDeclaration,
    ExecutionStopSignal,
    GuardedDockerBackend,
    GuardedExecutionManifest,
    workload,
)

from .policy import _mapping

if TYPE_CHECKING:
    from .controller import DockerJobController


def run_guarded(
    self: "DockerJobController",
    handle: DockerOwnedContainer,
    sink: DockerOutputSink,
    *,
    deadline: float,
    cleanup_deadline: float,
    stop: ExecutionStopSignal | None,
) -> DockerJobOutcome:
    with self._operation():
        self._budgets(deadline, cleanup_deadline)
        session = self._session(handle)
        action = handle.intent.record.manifest
        assert isinstance(action, GuardedExecutionManifest)
        if stop is not None:
            stop.require_scope(handle.intent.record)
            stop.cap_cleanup(cleanup_deadline)
        if session.start_attempted or session.removed or self._quarantined:
            raise DockerBackendFault("docker_authority_required")
        session.start_attempted, session.sink = True, sink
        exit_code = None
        timed_out = False
        try:
            if stop is not None:
                stop.check()
            if (
                self.guards is None
                or not isinstance(self.backend, GuardedDockerBackend)
                or type(sink.retention_limit) is not int
                or sink.retention_limit != action.profile.output_bytes
            ):
                raise DockerBackendFault("docker_unsafe_configuration")
            self.guards.check(action, deadline=deadline)
            target = self._verify(
                DockerTargetDeclaration.model_validate(self._declaration(handle)), deadline, stop
            )
            if target.fingerprint != handle.intent.target.fingerprint:
                raise DockerBackendFault("docker_target_mismatch")
            self.inputs.check(handle.intent.inputs, workload(action), deadline=deadline)
            raw = self._read(handle.intent, handle.container_id, deadline, handle.created_at)
            self._safe_config(handle, raw)
            state = _mapping(raw.get("State"))
            if state.get("Status") != "created" or state.get("Running") is not False:
                raise DockerBackendFault("docker_policy_mismatch")
            if stop is not None:
                stop.check()
            self._record(handle.intent, "start_intent", deadline=deadline, handle=handle)
            session.start_recorded = True
            status, terminal = self.backend.attach_guarded(
                handle,
                sink,
                stop,
                (lambda: self._observe_running(handle, stop, deadline)) if stop else None,
                deadline=deadline,
            )
            raw = self._read(
                handle.intent, handle.container_id, cleanup_deadline, handle.created_at
            )
            self._safe_config(handle, raw)
            state = _mapping(raw.get("State"))
            expected = (
                terminal.exit_code
                if terminal.cause == "workload_exit"
                else 128 + terminal.signal_number
                if terminal.cause == "workload_signal" and terminal.signal_number is not None
                else 124
            )
            code = state.get("ExitCode")
            if (
                terminal.manifest_digest != action.fingerprint
                or terminal.cause == "guard_fault"
                or type(code) is not int
                or code != expected
                or type(status) is not int
                or status != code
                or state.get("Status") != "exited"
                or state.get("Running") is not False
                or state.get("Pid") != 0
                or state.get("OOMKilled") is not False
                or state.get("Error")
            ):
                raise DockerBackendFault("docker_mutation_unconfirmed")
            exit_code = session.exit_code = code
            self._record(
                handle.intent, "exited", deadline=cleanup_deadline, handle=handle, exit_code=code
            )
            timed_out = terminal.cause == "deadline_expired"
            if timed_out and stop is not None:
                stop.request(uuid4(), "timeout")
                raise DockerBackendFault("docker_cancelled")
        except Exception as error:
            if (
                isinstance(error, DockerBackendFault)
                and error.code == "docker_cancelled"
                and stop is not None
                and stop.requested
            ):
                raise  # Original-owner explicit cancellation/confirmed guard deadline only.
            session.guard_uncertain = True
            self._unknown(handle.intent, cleanup_deadline, handle)
            session.outcome = DockerJobOutcome(
                exit_code=exit_code, outcome_unknown=True, cleanup_verified=False
            )
            return session.outcome
        cleaned = self._cleanup(session, cleanup_deadline)
        session.outcome = DockerJobOutcome(
            exit_code=exit_code,
            timed_out=timed_out,
            outcome_unknown=self._journal_lost or not cleaned,
            cleanup_verified=cleaned,
        )
        return session.outcome

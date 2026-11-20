"""Exact-owned stop cleanup and atomic guarded partial-output finalization."""

from typing import TYPE_CHECKING

from jeff.application.execution import (
    DockerBackendFault,
    DockerCompletion,
    DockerJobOutcome,
    DockerOutputCapture,
    DockerOwnedContainer,
    ExecutionStopObservation,
    ExecutionStopRecord,
    ExecutionStopSignal,
    ExecutionStopStore,
    ExecutionStopTerminal,
)
from jeff.domain.execution import fingerprint

if TYPE_CHECKING:
    from .controller import DockerJobController


def finish_stop_owned(
    self: "DockerJobController",
    handle: DockerOwnedContainer,
    capture: DockerOutputCapture,
    stop: ExecutionStopSignal,
    store: ExecutionStopStore,
) -> ExecutionStopRecord:
    with self._operation():
        session = self._session(handle)
        stop.require_scope(handle.intent.record)
        if session.sink is not capture or session.completion_attempted or not stop.requested:
            raise DockerBackendFault("docker_authority_required")
        session.completion_attempted = True
        try:
            output = capture.seal(abort=True)
            cleaned = self._cleanup(
                session, stop.cleanup_deadline, grace_deadline=stop.grace_deadline
            )
            record = stop.confirmed
            known = (
                record is not None
                and not self._journal_lost
                and not self._quarantined
                and cleaned
                and (not session.start_recorded or session.exit_code is not None)
            )
            if record is None:
                self._unknown(handle.intent, stop.recording_deadline, handle)
                raise DockerBackendFault("docker_journal_unconfirmed")
            facts = DockerCompletion(
                owned=handle,
                outcome=DockerJobOutcome(
                    exit_code=session.exit_code,
                    cleanup_verified=cleaned,
                    timed_out=False,
                    outcome_unknown=not known,
                ),
                output=output,
            )
            terminal = ExecutionStopTerminal(
                outcome=("timed_out" if record.reason == "timeout" else "cancelled")
                if known
                else "unknown",
                evidence="owned" if known else "uncertain",
                cleanup_verified=cleaned,
                exit_code=session.exit_code if known else None,
                attempt_id=handle.intent.attempt_id if known else None,
                ownership_digest=fingerprint(
                    {
                        "intent": handle.intent.fingerprint,
                        "container": handle.container_id,
                        "birth": handle.created_at,
                    }
                )
                if known
                else None,
                output_facts=facts.output_facts,
                output_digest=facts.output_digest,
            )
            observation = ExecutionStopObservation(
                scope=stop.scope,
                request_id=record.request_id,
                manifest_digest=record.manifest_digest,
                terminal=terminal,
                owned=handle if known else None,
            )
            result = store.finish_stop_with_capture(
                observation, output, deadline=stop.recording_deadline
            )
            if (
                result.terminal != terminal
                or result.scope != record.scope
                or result.request_id != record.request_id
            ):
                raise DockerBackendFault("docker_journal_unconfirmed")
            if not known:
                self._quarantined = True
            return result
        except Exception:
            self._quarantined = True
            raise DockerBackendFault("docker_journal_unconfirmed") from None
        finally:
            session.sink = None

"""Serialized transitions, live stop deadlines and same-owner guarded cancellation recording."""

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from jeff.application.execution import DockerBackendFault, DockerOutputSink
from jeff.infrastructure.runtime_output import GuardedOutputCollector

from .support_controller import setup_controller
from .support_ports import Backend, Sink


def test_transition_serialization_and_uncertain_start_never_launch_twice() -> None:
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    backend.entered = threading.Event()
    backend.release = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(
            controller.run,
            handle,
            Sink(),
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
        try:
            assert backend.entered.wait(1)
            with pytest.raises(DockerBackendFault) as busy:
                controller.terminate_remove_owned(handle, deadline=time.monotonic() + 5)
            assert busy.value.code == "docker_busy"
        finally:
            backend.release.set()
        assert pending.result(timeout=2).cleanup_verified
    assert backend.calls == ["create", "attach", "remove"]
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    journal.fail_phase = "start_intent"
    outcome = controller.run(
        handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.outcome_unknown and outcome.cleanup_verified
    with pytest.raises(DockerBackendFault):
        controller.run(
            handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
        )
    assert backend.calls == ["create", "remove"]


def test_live_stop_latch_never_extends_first_deadlines_or_reconstructs_them() -> None:
    from jeff.application.execution import ExecutionStopSignal

    _, _, _, claim, _ = setup_controller()
    start = time.monotonic()
    stop = ExecutionStopSignal(claim.record, job_deadline=start + 30)
    phase = stop.phase
    assert not stop.requested and phase == "admitted"
    stop.mark_running()
    phase = stop.phase
    assert phase == "running_observed"
    first = uuid4()
    stop.request(first, "cancel")
    grace, cleanup, recording = stop.grace_deadline, stop.cleanup_deadline, stop.recording_deadline
    assert start <= grace <= start + 5.1
    assert grace <= cleanup <= start + 10.1 and cleanup < recording <= start + 15.1
    stop.request(uuid4(), "timeout")
    assert (stop.grace_deadline, stop.cleanup_deadline, stop.recording_deadline) == (
        grace,
        cleanup,
        recording,
    )
    phase = stop.phase
    assert stop.requested and phase == "stop_requested"
    request = stop.request_record()
    assert request.request_id == first and request.reason == "cancel"
    stop.request(uuid4(), "shutdown", shutdown_deadline=cleanup - 1)
    assert stop.cleanup_deadline <= cleanup - 6 and stop.recording_deadline <= cleanup - 1
    assert stop.request_record() == request
    with pytest.raises(DockerBackendFault) as interrupted:
        stop.check()
    assert interrupted.value.code == "docker_cancelled"


def test_cancelled_attach_returns_to_same_owner_before_guarded_stop_recording() -> None:
    from jeff.application.execution import (
        DockerOutputSnapshot,
        ExecutionInspectionScope,
        ExecutionStopObservation,
        ExecutionStopRecord,
        ExecutionStopRequest,
        ExecutionStopSignal,
    )

    controller, original, journal, claim, declaration = setup_controller()
    grace_values: list[int] = []

    class CancellableBackend(Backend):
        def attach_cancellable(
            self,
            endpoint: str,
            container_id: str,
            sink: DockerOutputSink,
            stop: ExecutionStopSignal,
            observe_running: Callable[[], None],
            *,
            deadline: float,
        ) -> int:
            self.calls.append("attach")
            self.running = True
            observe_running()
            assert stop.phase == "running_observed"
            sink.accept("stdout", b"safe prefix\n")
            stop.request(uuid4(), "cancel")
            stop.check()
            raise AssertionError("Cancelled attach must return control without container mutations")

        def stop_gracefully(
            self, endpoint: str, container_id: str, *, grace_seconds: int, deadline: float
        ) -> None:
            grace_values.append(grace_seconds)
            self.stop(endpoint, container_id, deadline=deadline)

    backend = CancellableBackend(original.target, journal)
    controller.backend = backend
    signal = ExecutionStopSignal(claim.record, job_deadline=time.monotonic() + 30)
    capture = GuardedOutputCollector(claim.record.manifest.profile.output_bytes)
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    with pytest.raises(DockerBackendFault) as interrupted:
        controller.run(
            handle,
            capture,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
            stop=signal,
        )
    assert interrupted.value.code == "docker_cancelled"
    assert backend.calls == ["create", "attach"]
    request = signal.request_record()
    record = ExecutionStopRecord(
        scope=signal.scope,
        request_id=request.request_id,
        manifest_digest=request.manifest_digest,
        reason="cancel",
        requested_at_ms=1000,
        clock_confirmed=True,
    )
    signal.confirm(record)
    observations: list[ExecutionStopObservation] = []

    class Stops:
        def request_stop(
            self, request: ExecutionStopRequest, *, deadline: float
        ) -> ExecutionStopRecord:
            raise AssertionError("Coordinator already persisted intent")

        def inspect_stop(
            self, scope: ExecutionInspectionScope, *, deadline: float
        ) -> ExecutionStopRecord | None:
            raise AssertionError("Do not reconstruct control from SQL")

        def finish_stop(
            self, observation: ExecutionStopObservation, *, deadline: float
        ) -> ExecutionStopRecord:
            raise AssertionError("Use atomic guarded capture recording")

        def finish_stop_with_capture(
            self,
            observation: ExecutionStopObservation,
            snapshot: DockerOutputSnapshot,
            *,
            deadline: float,
        ) -> ExecutionStopRecord:
            observations.append(observation)
            assert backend.calls == ["create", "attach", "stop", "remove"]
            assert snapshot.stdout == "safe prefix\n" and snapshot.aborted
            return record.model_copy(update={"terminal": observation.terminal})

    result = controller.finish_stop_owned(handle, capture, signal, Stops())
    assert result.terminal is not None and result.terminal.outcome == "cancelled"
    assert result.terminal.exit_code == 7 and result.terminal.cleanup_verified
    assert 0 <= grace_values[0] <= 5
    assert [e.phase for e in journal.events] == [
        "create_intent",
        "created",
        "start_intent",
        "exited",
        "cleaned",
    ]
    assert len(observations) == 1
    with pytest.raises(DockerBackendFault):
        controller.finish_stop_owned(handle, capture, signal, Stops())
    assert len(observations) == 1

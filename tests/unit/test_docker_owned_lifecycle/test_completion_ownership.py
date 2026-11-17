"""One-attempt admission, exact live ownership, output completion and lost-ACK fencing."""

import time

import pytest
from pydantic import ValidationError

from jeff.application.execution import (
    DockerBackendFault,
    DockerCompletion,
    DockerExecutionResult,
    DockerLaunchEvent,
)
from jeff.infrastructure.runtime_execution import DockerJobController, create_arguments
from jeff.infrastructure.runtime_output import GuardedOutputCollector

from .support_controller import setup_controller
from .support_ports import Inputs, Sink, Verifier


def test_controller_completion_is_live_bound_and_one_shot_even_when_ack_is_lost() -> None:
    class Results:
        def __init__(self, lose: bool) -> None:
            self.calls: list[DockerCompletion] = []
            self.lose = lose

        def complete_execution(
            self, completion: DockerCompletion, *, deadline: float
        ) -> DockerExecutionResult:
            self.calls.append(completion)
            if self.lose:
                raise DockerBackendFault("docker_journal_unconfirmed")
            return DockerExecutionResult(
                owned=completion.owned,
                outcome=completion.outcome,
                output_facts=completion.output_facts,
                output_digest=completion.output_digest,
                completion_digest=completion.fingerprint,
                disposition="completed" if completion.confirmed else "quarantined",
                sequence=1,
                recorded_at_ms=1,
                payload_state="retained",
                output=completion.output,
            )

    for lose, post_eof_failure, timeout in (
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ):
        controller, backend, journal, claim, declaration = setup_controller()
        handle = controller.create(
            claim,
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
        sink = GuardedOutputCollector(claim.record.manifest.profile.output_bytes)
        backend.fail_after_eof = post_eof_failure
        backend.timeout_attach = timeout
        if not (lose or post_eof_failure or timeout):
            backend.exit_code = 0
        controller.run(
            handle, sink, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
        )
        results = Results(lose)
        with pytest.raises(DockerBackendFault):
            controller.complete(handle.model_copy(), sink, results, deadline=time.monotonic() + 5)
        with pytest.raises(DockerBackendFault):
            controller.complete(
                handle,
                GuardedOutputCollector(sink.retention_limit),
                results,
                deadline=time.monotonic() + 5,
            )
        if lose:
            with pytest.raises(DockerBackendFault):
                controller.complete(handle, sink, results, deadline=time.monotonic() + 5)
        else:
            result = controller.complete(handle, sink, results, deadline=time.monotonic() + 5)
            assert result.disposition == (
                "quarantined" if post_eof_failure or timeout else "completed"
            )
        with pytest.raises(DockerBackendFault):
            controller.complete(handle, sink, results, deadline=time.monotonic() + 5)
        assert len(results.calls) == 1 and results.calls[0].output.complete == (not timeout)
        if timeout:
            assert results.calls[0].output.aborted
        assert backend.calls == (
            ["create", "attach", "stop", "remove"] if timeout else ["create", "attach", "remove"]
        )
        if lose:
            assert (
                journal.events[-1].phase == "cleaned"
            )  # Never overwrite a possibly committed finish.


def test_owned_nonzero_run_records_intents_and_removal_is_verified() -> None:
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    args = create_arguments(handle.intent)
    assert args[0] == "create" and "--pull=never" in args
    assert "--read-only" in args and "--cap-drop=ALL" in args and "--no-healthcheck" in args
    assert "none" in args and "65532:65532" in args
    sink = GuardedOutputCollector(claim.record.manifest.profile.output_bytes)
    result = controller.run(
        handle, sink, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert result.exit_code == 7 and result.cleanup_verified and not result.outcome_unknown
    output = sink.seal()
    assert output.stdout == "synthetic-output" and output.complete
    assert output.stdout_source_bytes == len(b"synthetic-output")
    assert backend.calls == ["create", "attach", "remove"]
    assert [event.phase for event in journal.events] == [
        "create_intent",
        "created",
        "start_intent",
        "exited",
        "cleaned",
    ]


def test_claim_replay_and_uncertain_journal_never_create_again() -> None:
    controller, backend, journal, claim, declaration = setup_controller()
    with pytest.raises(DockerBackendFault):
        controller.create(
            claim.model_copy(update={"launch_claim": None}),
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    assert backend.calls == [] and journal.events == []
    journal.fail_phase = "create_intent"
    with pytest.raises(DockerBackendFault) as failed:
        controller.create(
            claim,
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    assert failed.value.code == "docker_journal_unconfirmed" and backend.calls == []
    journal.fail_phase = None
    other = DockerJobController(
        Verifier(backend.target),
        backend,
        Inputs(),
        journal,
        owner_instance_id=claim.record.owner_instance_id,
    )
    with pytest.raises(DockerBackendFault):
        other.create(
            claim,
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    assert (
        backend.calls == []
    )  # External journal's unique claim reservation survives controller replacement.

    intent = journal.events[-1].intent
    birth = "2026-01-01T00:00:00.000000000Z"
    with pytest.raises(ValidationError):
        DockerLaunchEvent(intent=intent, phase="create_intent", created_at=birth)
    with pytest.raises(ValidationError):
        DockerLaunchEvent(intent=intent, phase="unknown", container_id="c" * 64)
    with pytest.raises(ValidationError):
        DockerLaunchEvent(
            intent=intent, phase="created", container_id="c" * 64, created_at="not-a-birth"
        )
    observed = DockerLaunchEvent(intent=intent, phase="unknown", observed_container_id="c" * 64)
    assert (
        observed.container_id is None
    )  # Partial create response remains observation, not ownership.


def test_ambiguous_create_and_copied_handles_never_adopt_or_restart() -> None:
    controller, backend, journal, claim, declaration = setup_controller()
    backend.uncertain_create = True
    with pytest.raises(DockerBackendFault):
        controller.create(
            claim,
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    with pytest.raises(DockerBackendFault):
        controller.create(
            claim,
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    assert backend.calls == ["create"] and journal.events[-1].phase == "unknown"
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    with pytest.raises(DockerBackendFault) as forged:
        controller.run(
            handle.model_copy(),
            Sink(),
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    assert forged.value.code == "docker_ownership_unconfirmed" and backend.calls == ["create"]
    controller.run(
        handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    with pytest.raises(DockerBackendFault):
        controller.run(
            handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
        )
    with pytest.raises(DockerBackendFault):
        controller.create(
            claim,
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    assert backend.calls.count("attach") == backend.calls.count("create") == 1
    controller, backend, journal, claim, declaration = setup_controller()
    backend.inspect_unavailable = True
    with pytest.raises(DockerBackendFault):
        controller.create(
            claim,
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    assert journal.events[-1].observed_container_id == "c" * 64
    assert journal.events[-1].container_id is None and backend.calls == ["create"]
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    backend.attach_status = 125
    sink = GuardedOutputCollector(claim.record.manifest.profile.output_bytes)
    outcome = controller.run(
        handle, sink, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.exit_code == 7 and outcome.outcome_unknown and outcome.cleanup_verified
    assert sink.seal().complete  # Both EOFs are not controller/transport confirmation.
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    backend.fail_after_eof = True
    sink = GuardedOutputCollector(claim.record.manifest.profile.output_bytes)
    uncertain = controller.run(
        handle, sink, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert sink.seal().complete and uncertain.outcome_unknown and uncertain.cleanup_verified
    assert uncertain.exit_code is None

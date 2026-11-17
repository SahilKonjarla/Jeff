"""Durable prelaunch stop fences, immutable natural completion and owned terminal facts."""

import time
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.execution import DockerBackendFault, ReviewFault
from jeff.infrastructure.runtime_output import GuardedOutputCollector

from .support_authority import authority_store, command
from .support_launch import completion, fact, launch_intent, launch_scope


def test_stop_revokes_approved_authority_atomically_and_replays_first_facts(tmp_path: Path) -> None:
    from jeff.application.execution import ExecutionInspectionScope, ExecutionStopRequest

    with authority_store(tmp_path) as (store, project, manifest, generation):
        proposed = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        scope = ExecutionInspectionScope(
            project_id=project.project_id,
            canonical_root=project.root,
            owner_instance_id=generation,
            approval_id=proposed.approval_id,
        )
        request = ExecutionStopRequest(
            **scope.model_dump(),
            request_id=uuid4(),
            manifest_digest=manifest.fingerprint,
        )
        assert store.inspect_stop(scope, deadline=time.monotonic() + 5) is None
        with pytest.raises(ReviewFault):  # Pending reviews still use deny.
            store.request_stop(request, deadline=time.monotonic() + 5)
        store.authority(
            command(
                project,
                generation,
                operation="approve",
                approval_id=proposed.approval_id,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        first = store.request_stop(request, deadline=time.monotonic() + 5)
        assert first.terminal is not None and first.terminal.outcome == "cancelled"
        assert first.terminal.evidence == "not_started" and first.terminal.cleanup_verified
        assert store.request_stop(request, deadline=time.monotonic() + 5) == first
        duplicate = request.model_copy(update={"request_id": uuid4()})
        assert store.request_stop(duplicate, deadline=time.monotonic() + 5) == first
        assert store.inspect_stop(scope, deadline=time.monotonic() + 5) == first
        with pytest.raises(ReviewFault):
            store.request_stop(
                request.model_copy(update={"reason": "shutdown"}), deadline=time.monotonic() + 5
            )
        with pytest.raises(ReviewFault):
            store.authority(
                command(
                    project,
                    generation,
                    operation="claim",
                    approval_id=proposed.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        shown = store.authority(
            command(
                project,
                generation,
                operation="show",
                approval_id=proposed.approval_id,
            ),
            deadline=time.monotonic() + 5,
        )
        assert shown.record.state.value == "invalidated" and shown.launch_claim is None
        assert shown.record.claim_id is None


def test_stop_prelaunch_fence_requires_no_inflight_create_and_never_adopts(tmp_path: Path) -> None:
    from jeff.application.execution import (
        ExecutionInspectionScope,
        ExecutionStopObservation,
        ExecutionStopRequest,
        ExecutionStopTerminal,
    )

    for inflight in (False, True):
        with authority_store(tmp_path / str(inflight)) as (store, project, manifest, generation):
            intent = launch_intent(store, project, manifest, generation)
            scope = ExecutionInspectionScope(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
                approval_id=intent.record.approval_id,
            )
            if inflight:
                store.record(fact(intent, "create_intent"), deadline=time.monotonic() + 5)
            request = ExecutionStopRequest(
                **scope.model_dump(),
                request_id=uuid4(),
                manifest_digest=manifest.fingerprint,
            )
            first = store.request_stop(request, deadline=time.monotonic() + 5)
            assert first.terminal is None
            if not inflight:
                with pytest.raises(DockerBackendFault):
                    store.record(fact(intent, "create_intent"), deadline=time.monotonic() + 5)
            observation = ExecutionStopObservation(
                scope=scope,
                request_id=first.request_id,
                manifest_digest=manifest.fingerprint,
                terminal=ExecutionStopTerminal(
                    outcome="cancelled",
                    evidence="not_started",
                    cleanup_verified=True,
                ),
            )
            if inflight:
                with pytest.raises(ReviewFault):
                    store.finish_stop(observation, deadline=time.monotonic() + 5)
                pending = store.inspect_stop(scope, deadline=time.monotonic() + 5)
                assert pending is not None and pending.terminal is None
            else:
                final = store.finish_stop(observation, deadline=time.monotonic() + 5)
                assert final.terminal == observation.terminal
                assert store.finish_stop(observation, deadline=time.monotonic() + 5) == final
                shown = store.authority(
                    command(
                        project,
                        generation,
                        operation="show",
                        approval_id=scope.approval_id,
                    ),
                    deadline=time.monotonic() + 5,
                )
                assert shown.record.claim_state == "finished" and shown.launch_claim is None
                with pytest.raises(DockerBackendFault):
                    store.record(fact(intent, "create_intent"), deadline=time.monotonic() + 5)


def test_stop_natural_completion_wins_without_rewriting_legacy_result(tmp_path: Path) -> None:
    from jeff.application.execution import ExecutionInspectionScope, ExecutionStopRequest

    for request_before in (True, False):
        with authority_store(tmp_path / str(request_before)) as (
            store,
            project,
            manifest,
            generation,
        ):
            intent = launch_intent(store, project, manifest, generation)
            scope = ExecutionInspectionScope(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
                approval_id=intent.record.approval_id,
            )
            request = ExecutionStopRequest(
                **scope.model_dump(),
                request_id=uuid4(),
                manifest_digest=manifest.fingerprint,
            )
            for phase in ("create_intent", "created", "start_intent"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            if request_before:
                assert store.request_stop(request, deadline=time.monotonic() + 5).terminal is None
            for phase in ("exited", "cleaned"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            observed = completion(intent)
            original = observed.fingerprint
            result = store.complete_execution(observed, deadline=time.monotonic() + 5)
            assert result.disposition == "completed" and result.completion_digest == original
            if not request_before:
                store.request_stop(request, deadline=time.monotonic() + 5)
            stopped = store.inspect_stop(scope, deadline=time.monotonic() + 5)
            assert stopped is not None and stopped.terminal is not None
            assert stopped.terminal.outcome == "completed"
            assert stopped.terminal.exit_code == 7
            assert store.request_stop(request, deadline=time.monotonic() + 5) == stopped
            assert (
                store.execution_result(launch_scope(intent), deadline=time.monotonic() + 5)
                == result
            )


def test_stop_known_owned_exit_keeps_partial_logs_distinct_and_unknown_quarantined(
    tmp_path: Path,
) -> None:
    from jeff.application.execution import (
        DockerOutputFacts,
        ExecutionInspectionScope,
        ExecutionStopObservation,
        ExecutionStopRequest,
        ExecutionStopTerminal,
    )
    from jeff.domain.execution import fingerprint

    for uncertain in (False, True):
        with authority_store(tmp_path / str(uncertain)) as (store, project, manifest, generation):
            intent = launch_intent(store, project, manifest, generation)
            scope = ExecutionInspectionScope(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
                approval_id=intent.record.approval_id,
            )
            for phase in ("create_intent", "created", "start_intent"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            request = ExecutionStopRequest(
                **scope.model_dump(),
                request_id=uuid4(),
                manifest_digest=manifest.fingerprint,
                reason="timeout",
            )
            first = store.request_stop(request, deadline=time.monotonic() + 5)
            for phase in ("exited", "cleaned"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            owned = completion(intent).owned
            capture = GuardedOutputCollector(manifest.profile.output_bytes)
            capture.accept("stdout", b"partial")
            snapshot = capture.seal(abort=True)
            facts = DockerOutputFacts.model_validate(
                snapshot.model_dump(exclude={"stdout", "stderr"})
            )
            terminal = ExecutionStopTerminal(
                outcome="timed_out",
                evidence="owned",
                cleanup_verified=True,
                exit_code=7,
                attempt_id=intent.attempt_id,
                ownership_digest=fingerprint(
                    {
                        "intent": intent.fingerprint,
                        "container": owned.container_id,
                        "birth": owned.created_at,
                    }
                ),
                output_facts=facts,
                output_digest="a" * 64,
            )
            observation = ExecutionStopObservation(
                scope=scope,
                request_id=first.request_id,
                manifest_digest=manifest.fingerprint,
                terminal=terminal,
                owned=owned,
            )
            if not uncertain:
                forged_completed = observation.model_copy(
                    update={
                        "terminal": terminal.model_copy(update={"outcome": "completed"}),
                    }
                )
                with pytest.raises(ReviewFault):
                    store.finish_stop(forged_completed, deadline=time.monotonic() + 5)
            if uncertain:
                store.record(fact(intent, "unknown"), deadline=time.monotonic() + 5)
                with pytest.raises(ReviewFault):
                    store.finish_stop(observation, deadline=time.monotonic() + 5)
                observation = observation.model_copy(
                    update={"terminal": terminal.model_copy(update={"outcome": "unknown"})}
                )
            final = store.finish_stop(observation, deadline=time.monotonic() + 5)
            assert final.terminal is not None and final.terminal.output_facts is not None
            assert (
                final.terminal.output_facts.aborted and not final.terminal.output_facts.stdout_eof
            )
            assert final.terminal.outcome == ("unknown" if uncertain else "timed_out")
            shown = store.authority(
                command(project, generation, operation="show", approval_id=scope.approval_id),
                deadline=time.monotonic() + 5,
            )
            assert shown.record.claim_state == ("unknown" if uncertain else "finished")
            # Wire12 projects separate terminal facts, never a fabricated v1 result
            # or retroactively attached text for this metadata-only DB9 record.
            inspected = store.inspect_execution(scope, deadline=time.monotonic() + 5)
            assert inspected.completion_state == "stop_recorded" and inspected.result is None
            assert inspected.stop is not None and inspected.stop.outcome == final.terminal.outcome
            assert inspected.stop.payload_state == "missing" and inspected.stop_output is None
            assert store.snapshot().state == "ready"

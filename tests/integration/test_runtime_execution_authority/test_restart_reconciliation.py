"""Repeated SQL-owner restarts preserve spent authority, not live execution permission.

These use synthetic journal facts and caller reply loss, not real daemon/container
crashes. Historical public show/logs access belongs to the next Phase5 step.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from jeff.application.execution import (
    AuthorityRecord,
    DockerBackendFault,
    ExecutionInspectionScope,
    ExecutionManifest,
    ExecutionStopObservation,
    ExecutionStopRequest,
    ExecutionStopTerminal,
    ReviewFault,
)
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_authority import authority_store, command
from .support_launch import completion, fact, launch_intent, launch_scope, stopped_sample


@contextmanager
def reopened(
    previous: SQLiteStorage, *, retention_clock: Callable[[], int] | None = None
) -> Iterator[tuple[SQLiteStorage, UUID]]:
    previous.close(timeout=5)
    generation = uuid4()
    current = SQLiteStorage(previous.owner, instance_id=generation, retention_clock=retention_clock)
    current.start()
    try:
        ready = current.wait_ready(5)
        assert ready.state == "ready" and ready.schema_version == 12
        yield current, generation
    finally:
        current.close(timeout=5)


def propose(
    store: SQLiteStorage, project: Project, manifest: ExecutionManifest, generation: UUID
) -> AuthorityRecord:
    return store.authority(
        command(
            project,
            generation,
            operation="propose",
            manifest=manifest,
            current_manifest_digest=manifest.fingerprint,
        ),
        deadline=time.monotonic() + 5,
    ).record


@pytest.mark.parametrize("state", ("pending", "approved", "denied", "expired"))
def test_repeated_restart_never_revives_unused_authority(tmp_path: Path, state: str) -> None:
    clock = {"wall": 100_000, "mono": 100}
    with authority_store(tmp_path, clock) as (store, project, manifest, generation):
        original = propose(store, project, manifest, generation)
        if state in {"approved", "denied"}:
            store.authority(
                command(
                    project,
                    generation,
                    operation="approve" if state == "approved" else "deny",
                    approval_id=original.approval_id,
                    current_manifest_digest=manifest.fingerprint if state == "approved" else None,
                ),
                deadline=time.monotonic() + 5,
            )
        elif state == "expired":
            clock.update(wall=701_000, mono=701)
        before = store.authority(
            command(project, generation, operation="show", approval_id=original.approval_id),
            deadline=time.monotonic() + 5,
        ).record
        assert before.state == state
        expected = "invalidated" if state in {"pending", "approved"} else state
        for _ in range(2):
            with reopened(store) as (again, current):
                shown = again.authority(
                    command(project, current, operation="show", approval_id=original.approval_id),
                    deadline=time.monotonic() + 5,
                )
                assert shown.record == before.model_copy(update={"state": expected})
                assert shown.launch_claim is None and shown.record.claim_id is None
                for action in ("approve", "claim"):
                    with pytest.raises(ReviewFault):
                        again.authority(
                            command(
                                project,
                                current,
                                operation=action,
                                approval_id=original.approval_id,
                                current_manifest_digest=manifest.fingerprint,
                            ),
                            deadline=time.monotonic() + 5,
                        )
                store = again


@pytest.mark.parametrize(
    "boundary", range(6), ids=("claim", "create", "created", "start", "exit", "clean")
)
def test_each_unfinished_launch_boundary_remains_unknown_across_restarts(
    tmp_path: Path, boundary: int
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        phases = ("create_intent", "created", "start_intent", "exited", "cleaned")[:boundary]
        for phase in phases:
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        settings = store.owner.store.layout.settings.read_bytes()
        for _ in range(2):
            with reopened(store) as (again, current):
                observed = again.authority(
                    command(
                        project, current, operation="show", approval_id=intent.record.approval_id
                    ),
                    deadline=time.monotonic() + 5,
                )
                assert observed.launch_claim is None and observed.record.claim_state == "unknown"
                assert observed.record.model_dump(
                    exclude={"claim_state"}
                ) == intent.record.model_dump(exclude={"claim_state"})
                for phase in ("create_intent", "start_intent", "unknown"):
                    with pytest.raises(DockerBackendFault, match="docker_authority_required"):
                        again.record(fact(intent, phase), deadline=time.monotonic() + 5)
                if phases:
                    snapshot = again.launch_status(
                        launch_scope(intent), deadline=time.monotonic() + 5
                    )
                    assert snapshot.phases == phases and snapshot.uncertain
                    assert snapshot.intent == intent and snapshot.claim_state == "unknown"
                else:
                    with pytest.raises(DockerBackendFault):
                        again.launch_status(launch_scope(intent), deadline=time.monotonic() + 5)
                with pytest.raises(DockerBackendFault):
                    again.execution_result(launch_scope(intent), deadline=time.monotonic() + 5)
                candidate = propose(again, project, manifest, current)
                again.authority(
                    command(
                        project,
                        current,
                        operation="approve",
                        approval_id=candidate.approval_id,
                        current_manifest_digest=manifest.fingerprint,
                    ),
                    deadline=time.monotonic() + 5,
                )
                with pytest.raises(ReviewFault, match="execution_outcome_unknown"):
                    again.authority(
                        command(
                            project,
                            current,
                            operation="claim",
                            approval_id=candidate.approval_id,
                            current_manifest_digest=manifest.fingerprint,
                        ),
                        deadline=time.monotonic() + 5,
                    )
                assert again.owner.store.layout.settings.read_bytes() == settings
                store = again


def test_lost_claim_reply_never_recovers_launch_permission_after_restart(tmp_path: Path) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        pending = propose(store, project, manifest, generation)
        store.authority(
            command(
                project,
                generation,
                operation="approve",
                approval_id=pending.approval_id,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        claim = command(
            project,
            generation,
            operation="claim",
            approval_id=pending.approval_id,
            current_manifest_digest=manifest.fingerprint,
        )

        def lose_reply() -> None:
            store.authority(claim, deadline=time.monotonic() + 5)
            raise RuntimeError("synthetic observer lost committed claim reply")

        with pytest.raises(RuntimeError, match="synthetic observer"):
            lose_reply()
        replay = store.authority(claim, deadline=time.monotonic() + 5)
        assert replay.launch_claim is None and replay.record.claim_state == "claimed"
        for _ in range(2):
            with reopened(store) as (again, current):
                for request in (claim, claim.model_copy(update={"owner_instance_id": current})):
                    with pytest.raises(ReviewFault):
                        again.authority(request, deadline=time.monotonic() + 5)
                facts = again.authority(
                    command(project, current, operation="show", approval_id=pending.approval_id),
                    deadline=time.monotonic() + 5,
                )
                assert facts.launch_claim is None and facts.record.claim_state == "unknown"
                assert facts.record.claim_id == replay.record.claim_id
                assert facts.record.claimed_at_ms == replay.record.claimed_at_ms
                store = again


@pytest.mark.parametrize("unknown", (False, True), ids=("completed", "quarantined"))
def test_committed_result_survives_lost_reply_and_repeated_restart(
    tmp_path: Path, unknown: bool
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        request = completion(intent, unknown=unknown)

        def lose_reply() -> None:
            store.complete_execution(request, deadline=time.monotonic() + 5)
            raise RuntimeError("synthetic observer lost committed completion reply")

        with pytest.raises(RuntimeError, match="synthetic observer"):
            lose_reply()
        saved = store.execution_result(launch_scope(intent), deadline=time.monotonic() + 5)
        assert saved.disposition == ("quarantined" if unknown else "completed")
        for _ in range(2):
            with reopened(store) as (again, current):
                assert (
                    again.execution_result(launch_scope(intent), deadline=time.monotonic() + 5)
                    == saved
                )
                with pytest.raises(DockerBackendFault, match="docker_authority_required"):
                    again.complete_execution(request, deadline=time.monotonic() + 5)
                with pytest.raises(DockerBackendFault, match="docker_authority_required"):
                    again.record(fact(intent, "unknown"), deadline=time.monotonic() + 5)
                with pytest.raises(ReviewFault):
                    again.authority(
                        command(
                            project,
                            current,
                            operation="claim",
                            approval_id=intent.record.approval_id,
                            current_manifest_digest=manifest.fingerprint,
                        ),
                        deadline=time.monotonic() + 5,
                    )
                snapshot = again.launch_status(launch_scope(intent), deadline=time.monotonic() + 5)
                assert snapshot.claim_state == ("unknown" if unknown else "finished")
                store = again


@pytest.mark.parametrize("terminal", (False, True), ids=("pending-stop", "known-stop"))
def test_restart_does_not_transfer_stop_mutation_authority(tmp_path: Path, terminal: bool) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        if terminal:
            first, capture = stopped_sample(store, project, manifest, generation)
            observation = capture.observation
        else:
            intent = launch_intent(store, project, manifest, generation)
            scope = ExecutionInspectionScope(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
                approval_id=intent.record.approval_id,
            )
            first = store.request_stop(
                ExecutionStopRequest(
                    **scope.model_dump(),
                    request_id=uuid4(),
                    manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
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
        request = ExecutionStopRequest(
            **first.scope.model_dump(),
            request_id=uuid4(),
            manifest_digest=manifest.fingerprint,
            reason="shutdown",
        )
        assert store.request_stop(request, deadline=time.monotonic() + 5) == first
        for _ in range(2):
            with reopened(store) as (again, current):
                changed_scope = first.scope.model_copy(update={"owner_instance_id": current})
                for supplied in (
                    request,
                    request.model_copy(update={"owner_instance_id": current}),
                ):
                    with pytest.raises(ReviewFault, match="execution_target_mismatch"):
                        again.request_stop(supplied, deadline=time.monotonic() + 5)
                with pytest.raises(ReviewFault, match="execution_target_mismatch"):
                    again.finish_stop(observation, deadline=time.monotonic() + 5)
                rebound = observation.model_copy(update={"scope": changed_scope})
                if terminal:
                    # Relabeling scope cannot relabel the captured owned launch binding.
                    with pytest.raises(ValueError, match="different owned work"):
                        again.finish_stop(rebound, deadline=time.monotonic() + 5)
                else:
                    with pytest.raises(ReviewFault, match="execution_target_mismatch"):
                        again.finish_stop(rebound, deadline=time.monotonic() + 5)
                shown = again.authority(
                    command(
                        project, current, operation="show", approval_id=first.scope.approval_id
                    ),
                    deadline=time.monotonic() + 5,
                )
                assert shown.launch_claim is None and shown.record.state == "consumed"
                assert shown.record.claim_state == ("finished" if terminal else "unknown")
                store = again


@pytest.mark.parametrize("unknown", (False, True), ids=("completed", "quarantined"))
def test_expired_output_cannot_restore_consumed_authority(tmp_path: Path, unknown: bool) -> None:
    wall = [time.time_ns() // 1_000_000]

    def clock() -> int:
        return wall[0]

    with authority_store(tmp_path, retention_clock=clock) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        original = store.complete_execution(
            completion(intent, unknown=unknown), deadline=time.monotonic() + 5
        )
        assert original.output is not None
        wall[0] += 31 * 86_400_000
        for _ in range(2):
            with reopened(store, retention_clock=clock) as (again, current):
                result = again.execution_result(launch_scope(intent), deadline=time.monotonic() + 5)
                assert result.payload_state == "expired" and result.output is None
                assert result.model_dump(
                    exclude={"output", "payload_state"}
                ) == original.model_dump(exclude={"output", "payload_state"})
                shown = again.authority(
                    command(
                        project, current, operation="show", approval_id=intent.record.approval_id
                    ),
                    deadline=time.monotonic() + 5,
                )
                assert shown.record.state == "consumed" and shown.launch_claim is None
                assert shown.record.claim_state == ("unknown" if unknown else "finished")
                if unknown:
                    with pytest.raises(ReviewFault, match="execution_outcome_unknown"):
                        launch_intent(again, project, manifest, current)
                store = again

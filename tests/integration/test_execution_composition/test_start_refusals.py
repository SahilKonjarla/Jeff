"""Confirmed Start refusals remain distinct from uncertain claim outcomes."""

import os
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from jeff.application.dispatch import ProjectDispatcher
from jeff.application.execution import (
    AuthorityOperation,
    AuthorityRecord,
    AuthorityResult,
    ExecutionInspectionScope,
    FreshExecutionCall,
    FreshExecutionReply,
    ReviewFault,
)
from jeff.application.execution_coordinator import ExecutionCoordinator
from jeff.application.questions import QuestionService
from jeff.application.runtime import RuntimeIdentityData
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher
from jeff.infrastructure.runtime_state import Ownership
from jeff.infrastructure.runtime_storage import SQLiteStorage
from jeff.integrations.git import GitRepositoryRead

from . import support_composition as fixtures
from .support_composition import approved, composed, pending, show
from .test_historical_inspection import restarted_coordinator


def start_reply(
    coordinator: ExecutionCoordinator,
    store: SQLiteStorage,
    project: Project,
    record: AuthorityRecord,
    *,
    request_id: UUID | None = None,
) -> FreshExecutionReply:
    assert store.instance_id is not None
    identity = RuntimeIdentityData(instance_id=store.instance_id, pid=os.getpid())
    workers = RuntimeDispatcher(
        ProjectDispatcher(
            coordinator.projects,
            QuestionService(coordinator.projects, GitRepositoryRead()),
            store,
            identity,
        ),
        fresh=coordinator,
    )
    call = FreshExecutionCall(
        request_id=request_id or uuid4(),
        directory=project.root,
        operation="start",
        approval_id=record.approval_id,
        reviewed_manifest_digest=record.manifest_digest,
    )
    workers.start()  # Existing in-process worker threads, not a Native daemon.
    try:
        reply = workers.submit(call, time.monotonic() + 5).result(timeout=5)
        assert isinstance(reply, FreshExecutionReply)
        return reply
    finally:
        workers.close(timeout=5)


def test_denied_start_returns_confirmed_refusal_without_execution(tmp_path: Path) -> None:
    with composed(tmp_path) as (coordinator, store, project, docker):
        record = pending(coordinator, project)
        coordinator.decide(
            project.root,
            record.approval_id,
            record.manifest_digest,
            approve=False,
            deadline=time.monotonic() + 5,
        )
        reply = start_reply(coordinator, store, project, record)
        assert not reply.ok and reply.error is not None
        assert reply.error.error.code == "execution_unavailable"
        assert not docker.calls and show(store, project, record.approval_id).claim_state is None


def test_consumed_start_refuses_new_request_but_keeps_original_fact_replay(tmp_path: Path) -> None:
    with composed(tmp_path) as (coordinator, store, project, docker):
        record = approved(coordinator, project)
        request_id = uuid4()
        first = start_reply(coordinator, store, project, record, request_id=request_id)
        assert first.ok and first.admission is not None and not first.admission.replayed
        replay = start_reply(coordinator, store, project, record, request_id=request_id)
        assert replay.ok and replay.admission is not None and replay.admission.replayed
        refused = start_reply(coordinator, store, project, record)
        assert not refused.ok and refused.error is not None
        assert refused.error.error.code == "execution_conflict"
        assert docker.calls == ["create", "attach", "remove"]
        assert show(store, project, record.approval_id).claim_state == "finished"


def test_expired_start_returns_confirmed_refusal_without_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wall, mono = [1000000000], [100.0]

    def clocked(owner: Ownership, *, instance_id: UUID) -> SQLiteStorage:
        return SQLiteStorage(
            owner,
            instance_id=instance_id,
            review_clock=lambda: wall[0],
            review_monotonic=lambda: mono[0],
        )

    monkeypatch.setattr(fixtures, "SQLiteStorage", clocked)
    with composed(tmp_path) as (coordinator, store, project, docker):
        record = pending(coordinator, project)
        wall[0] += 601000
        mono[0] += 601
        reply = start_reply(coordinator, store, project, record)
        assert not reply.ok and reply.error is not None
        assert reply.error.error.code == "execution_unavailable"
        observed = show(store, project, record.approval_id)
        assert observed.state.value == "expired" and observed.claim_state is None
        assert observed.expires_at_ms == record.expires_at_ms and not docker.calls


def test_replacement_reader_cannot_start_old_authority(tmp_path: Path) -> None:
    with composed(tmp_path) as (original, previous, project, docker):
        record = approved(original, project)
        assert start_reply(original, previous, project, record).ok
        with restarted_coordinator(original, previous, docker) as (current, store):
            reply = start_reply(current, store, project, record)
            assert not reply.ok and reply.error is not None
            assert reply.error.error.code == "execution_unavailable"
            observed = store.inspect_execution(
                ExecutionInspectionScope(
                    project_id=project.project_id,
                    canonical_root=project.root,
                    owner_instance_id=current.instance_id,
                    approval_id=record.approval_id,
                ),
                deadline=time.monotonic() + 5,
            )
            assert observed.record.owner_instance_id == original.instance_id
            assert observed.record.claim_state == "finished"
            assert observed.result is not None and observed.result.outcome.exit_code == 0
            assert docker.calls == ["create", "attach", "remove"]


@pytest.mark.parametrize("mode", ("lost_claim", "postclaim_validation", "postclaim_conflict"))
def test_committed_claim_faults_stay_unknown(tmp_path: Path, mode: str) -> None:
    with composed(tmp_path) as (coordinator, store, project, docker):
        record = approved(coordinator, project)

        class Claims:
            def authority(
                self, operation: AuthorityOperation, *, deadline: float
            ) -> AuthorityResult:
                result = store.authority(operation, deadline=deadline)
                if operation.operation == "claim":
                    if mode != "postclaim_validation":
                        raise ReviewFault(
                            "storage_outcome_unconfirmed"
                            if mode == "lost_claim"
                            else "execution_conflict"
                        )
                    return result.model_copy(
                        update={
                            "record": result.record.model_copy(
                                update={"owner_instance_id": uuid4()}
                            )
                        }
                    )
                return result

        coordinator.authority = Claims()
        reply = start_reply(coordinator, store, project, record)
        assert not reply.ok and reply.error is not None
        assert reply.error.error.code == "request_outcome_unknown"
        observed = show(store, project, record.approval_id)
        assert observed.state.value == "consumed" and observed.claim_state == "claimed"
        assert not docker.calls


def test_unconfirmed_refusal_transaction_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with composed(tmp_path) as (coordinator, store, project, docker):
        record = approved(coordinator, project)
        assert start_reply(coordinator, store, project, record).ok
        retire = Ownership.retire_storage_recovery
        claim_active, retirements = [False], [0]

        def uncertain(owner: Ownership, fd: int) -> None:
            retire(owner, fd)
            if owner is store.owner and claim_active[0]:
                retirements[0] += 1
                if (
                    retirements[0] == 2
                ):  # Claim begin succeeded; commit retirement ACK is uncertain.
                    raise OSError("synthetic_retirement_ack_lost")

        class Claims:
            def authority(
                self, operation: AuthorityOperation, *, deadline: float
            ) -> AuthorityResult:
                if operation.operation == "claim":
                    claim_active[0] = True
                return store.authority(operation, deadline=deadline)

        monkeypatch.setattr(Ownership, "retire_storage_recovery", uncertain)
        coordinator.authority = Claims()
        reply = start_reply(coordinator, store, project, record)
        assert not reply.ok and reply.error is not None
        assert reply.error.error.code == "request_outcome_unknown"
        assert retirements[0] == 2 and docker.calls == ["create", "attach", "remove"]

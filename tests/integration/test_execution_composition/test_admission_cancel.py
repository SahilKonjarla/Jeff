"""Preclaim/active cancellation and durable admission/replay boundaries."""

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jeff.application.execution import (
    AuthorityOperation,
    AuthorityRecord,
    AuthorityResult,
    DockerExecutionResult,
    ReviewFault,
)
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_composition import approved, composed, show


def test_cancel_during_preclaim_verification_is_reachable_before_admission_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jeff.application.execution import ExecutionCancelCall, ExecutionStopRecord

    with composed(tmp_path, cancellable=True) as (coordinator, store, project, docker):
        record = approved(coordinator, project)
        call = ExecutionCancelCall(
            request_id=uuid4(),
            approval_id=record.approval_id,
            reviewed_manifest_digest=record.manifest_digest,
        )
        original = docker.read
        docker.queries.clear()

        def cancel_on_first_read(
            query: str, *, endpoint: str, image_id: str | None, deadline: float
        ) -> bytes:
            data = original(query, endpoint=endpoint, image_id=image_id, deadline=deadline)
            accepted = coordinator.submit_cancel(call, deadline=time.monotonic() + 2).result(
                timeout=2
            )
            assert accepted.terminal is not None and accepted.terminal.evidence == "not_started"
            return data

        monkeypatch.setattr(docker, "read", cancel_on_first_read)
        acknowledgements: list[object] = []
        result = coordinator.start(
            project.root,
            record.approval_id,
            record.manifest_digest,
            request_id=uuid4(),
            admission_deadline=time.monotonic() + 5,
            on_admitted=acknowledgements.append,
        )
        assert isinstance(result, ExecutionStopRecord) and result.terminal is not None
        assert result.terminal.outcome == "cancelled" and result.terminal.evidence == "not_started"
        assert not acknowledgements and not docker.calls and docker.queries == ["client.version"]
        assert coordinator.shutdown_confirmed
        shown = show(store, project, record.approval_id)
        assert shown.claim_id is None and shown.state.value == "invalidated"


def test_active_cancel_uses_owning_worker_and_inspects_partial_logs(tmp_path: Path) -> None:
    from jeff.application.execution import (
        ExecutionCancelCall,
        ExecutionStopRecord,
        FreshExecutionCall,
    )

    with composed(tmp_path, cancellable=True) as (coordinator, store, project, docker):
        record = approved(coordinator, project)
        docker.entered = threading.Event()
        original_start_id = uuid4()
        with ThreadPoolExecutor(max_workers=1) as worker:
            work = worker.submit(
                coordinator.start,
                project.root,
                record.approval_id,
                record.manifest_digest,
                request_id=original_start_id,
                admission_deadline=time.monotonic() + 5,
                on_admitted=lambda _: None,
            )
            assert docker.entered.wait(2)
            assert coordinator.execution_phase(record.approval_id) == "running_observed"
            call = ExecutionCancelCall(
                request_id=uuid4(),
                approval_id=record.approval_id,
                reviewed_manifest_digest=record.manifest_digest,
            )
            with pytest.raises(ReviewFault):
                coordinator.submit_cancel(
                    call.model_copy(update={"reviewed_manifest_digest": "0" * 64}),
                    deadline=time.monotonic() + 1,
                ).result(timeout=2)
            assert coordinator.execution_phase(record.approval_id) == "running_observed"
            accepted = coordinator.submit_cancel(call, deadline=time.monotonic() + 2).result(
                timeout=3
            )
            assert accepted.request_id == call.request_id
            result = work.result(timeout=5)
        assert isinstance(result, ExecutionStopRecord) and result.terminal is not None
        assert result.terminal.outcome == "cancelled" and result.terminal.cleanup_verified
        assert docker.calls == ["create", "attach", "stop", "remove"]
        assert coordinator.execution_phase(record.approval_id) is None
        for operation in ("show", "logs"):
            reply = coordinator.execute(
                FreshExecutionCall(
                    directory=project.root,
                    request_id=uuid4(),
                    operation=operation,
                    approval_id=record.approval_id,
                ),
                time.monotonic() + 5,
            )
            assert (
                reply.ok and reply.completion_state == "stop_recorded" and reply.completion is None
            )
            assert reply.stop is not None and reply.stop.outcome == "cancelled"
            assert reply.stop.output_facts is not None and reply.stop.output_facts.aborted
            assert (reply.output is not None) == (operation == "logs")
            if reply.output is not None:
                assert reply.output.stdout == "public output\n"
            assert not {
                "nonce",
                "container_id",
                "ownership_digest",
                "attempt_id",
                "owner_instance_id",
            } & set(reply.stop.model_dump())
        assert (
            coordinator.submit_cancel(call, deadline=time.monotonic() + 2).result(timeout=3)
            == result
        )
        coordinator.start(
            project.root,
            record.approval_id,
            record.manifest_digest,
            request_id=original_start_id,
            admission_deadline=time.monotonic() + 5,
            on_admitted=lambda _: None,
        )
        assert docker.calls == ["create", "attach", "stop", "remove"]


def test_confirmed_admission_survives_reply_loss_and_replays_facts_only(tmp_path: Path) -> None:
    def exercise(root: Path, lose_reply: bool) -> None:
        with composed(root) as (coordinator, store, project, docker):
            record = approved(coordinator, project)
            starter = coordinator.start
            operation_id = uuid4()
            response: Future[object] = Future()
            docker.entered = threading.Event()
            docker.release = threading.Event()
            deadline = time.monotonic() + 0.5

            def acknowledged(value: object) -> None:
                assert show(store, project, record.approval_id).claim_state == "claimed"
                assert docker.calls == []  # Durable claim, before any container mutation.
                response.set_result(value)
                if lose_reply:
                    raise RuntimeError("synthetic lost admission observer")

            with ThreadPoolExecutor(max_workers=1) as worker:
                work = worker.submit(
                    starter,
                    project.root,
                    record.approval_id,
                    record.manifest_digest,
                    request_id=operation_id,
                    admission_deadline=deadline,
                    on_admitted=acknowledged,
                )
                admission = response.result(timeout=2)
                from jeff.application.execution import ExecutionAdmission

                assert isinstance(admission, ExecutionAdmission)
                assert (
                    admission.job_id == record.job_id
                    and not admission.replayed
                    and admission.claim_state == "claimed"
                )
                assert not {"launch_claim", "claim_id", "nonce", "container_id", "owned"} & set(
                    admission.model_dump()
                )
                for change in (
                    {"launch_claim": "SYNTHETIC_PRIVATE"},
                    {"replayed": 1},
                    {"claim_state": "finished"},
                    {"authority_kind": "review_only_v1"},
                ):
                    with pytest.raises(ValidationError) as caught:
                        ExecutionAdmission.model_validate({**admission.model_dump(), **change})
                    assert "SYNTHETIC_PRIVATE" not in str(caught.value)
                assert docker.entered.wait(2) and not work.done()
                # Expiring a caller budget must not shorten an already-admitted job.
                threading.Event().wait(max(0, deadline - time.monotonic()) + 0.02)
                docker.release.set()
                result = work.result(timeout=5)
            assert isinstance(result, DockerExecutionResult) and result.disposition == "completed"
            assert not result.outcome.outcome_unknown and result.outcome.cleanup_verified
            assert docker.calls == ["create", "attach", "remove"]
            before = (list(docker.calls), list(docker.queries))
            replays: list[ExecutionAdmission] = []
            replay = starter(
                project.root,
                record.approval_id,
                record.manifest_digest,
                request_id=operation_id,
                admission_deadline=time.monotonic() + 5,
                on_admitted=replays.append,
            )
            assert isinstance(replay, AuthorityRecord) and replay.claim_state == "finished"
            assert (
                len(replays) == 1 and replays[0].replayed and replays[0].claim_state == "finished"
            )
            assert (docker.calls, docker.queries) == before
            with pytest.raises(ReviewFault):
                starter(
                    project.root,
                    record.approval_id,
                    record.manifest_digest,
                    request_id=uuid4(),
                    admission_deadline=time.monotonic() + 5,
                    on_admitted=replays.append,
                )
            assert (docker.calls, docker.queries) == before and len(replays) == 1
            other = approved(coordinator, project)
            with pytest.raises(ReviewFault) as conflict:
                starter(
                    project.root,
                    other.approval_id,
                    other.manifest_digest,
                    request_id=operation_id,
                    admission_deadline=time.monotonic() + 5,
                    on_admitted=replays.append,
                )
            assert conflict.value.code == "execution_conflict"
            assert docker.calls == before[0] and len(replays) == 1
            assert show(store, project, other.approval_id).claim_state is None

    exercise(tmp_path / "a", False)
    exercise(tmp_path / "l", True)

    # A lost SQL claim ACK must never be replaced with a new launch permission.
    class LostClaim:
        def __init__(self, base: SQLiteStorage) -> None:
            self.base = base
            self.once = True

        def authority(self, operation: AuthorityOperation, *, deadline: float) -> AuthorityResult:
            result = self.base.authority(operation, deadline=deadline)
            if operation.operation == "claim" and self.once:
                self.once = False
                raise ReviewFault("storage_outcome_unconfirmed")
            return result

    with composed(tmp_path / "u") as (coordinator, store, project, docker):
        record = approved(coordinator, project)
        coordinator.authority = LostClaim(store)
        operation_id = uuid4()
        observations: list[object] = []
        with pytest.raises(ReviewFault):
            coordinator.start(
                project.root,
                record.approval_id,
                record.manifest_digest,
                request_id=operation_id,
                admission_deadline=time.monotonic() + 5,
                on_admitted=observations.append,
            )
        assert not observations and not docker.calls
        from jeff.application.execution import FreshExecutionCall

        inspected = coordinator.execute(
            FreshExecutionCall(
                request_id=uuid4(),
                directory=project.root,
                operation="show",
                approval_id=record.approval_id,
            ),
            time.monotonic() + 5,
        )
        assert (
            inspected.ok
            and inspected.completion_state == "claim_only"
            and inspected.completion is None
        )
        before = list(docker.queries)
        replay = coordinator.start(
            project.root,
            record.approval_id,
            record.manifest_digest,
            request_id=operation_id,
            admission_deadline=time.monotonic() + 5,
            on_admitted=observations.append,
        )
        assert isinstance(replay, AuthorityRecord) and replay.claim_state == "claimed"
        assert len(observations) == 1 and not docker.calls and docker.queries == before
        assert show(store, project, record.approval_id).claim_id is not None

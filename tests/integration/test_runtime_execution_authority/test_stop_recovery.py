"""Scope/claim races, stop rollback/lost ACK and first immutable stop-time budgets."""

import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.execution import ReviewFault
from jeff.application.storage import StorageFault
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_authority import authority_store, command
from .support_launch import launch_intent


def test_stop_claim_races_scope_and_namespace_never_reissue_permission(tmp_path: Path) -> None:
    from jeff.application.execution import (
        ExecutionInspectionScope,
        ExecutionStopObservation,
        ExecutionStopRequest,
        ExecutionStopTerminal,
    )

    for index in range(4):
        with authority_store(tmp_path / str(index)) as (store, project, manifest, generation):
            proposal = command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            )
            record = store.authority(proposal, deadline=time.monotonic() + 5).record
            store.authority(
                command(
                    project,
                    generation,
                    operation="approve",
                    approval_id=record.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
            scope = ExecutionInspectionScope(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
                approval_id=record.approval_id,
            )
            request = ExecutionStopRequest(
                **scope.model_dump(), request_id=uuid4(), manifest_digest=manifest.fingerprint
            )
            for changes in (
                {"owner_instance_id": uuid4()},
                {"canonical_root": project.root / "wrong"},
                {"manifest_digest": "b" * 64},
                {"request_id": proposal.request_id},
            ):
                with pytest.raises(ReviewFault):
                    store.request_stop(
                        request.model_copy(update=changes), deadline=time.monotonic() + 5
                    )
            claim = command(
                project,
                generation,
                operation="claim",
                approval_id=record.approval_id,
                current_manifest_digest=manifest.fingerprint,
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                stop_future = pool.submit(
                    store.request_stop, request, deadline=time.monotonic() + 5
                )
                claim_future = pool.submit(store.authority, claim, deadline=time.monotonic() + 5)
                stopped = stop_future.result(timeout=5)
                try:
                    claimed = claim_future.result(timeout=5)
                except ReviewFault:
                    assert stopped.terminal is not None
                else:
                    assert claimed.launch_claim is not None and stopped.terminal is None
                    replay = store.authority(claim, deadline=time.monotonic() + 5)
                    assert replay.launch_claim is None
                    store.finish_stop(
                        ExecutionStopObservation(
                            scope=scope,
                            request_id=stopped.request_id,
                            manifest_digest=manifest.fingerprint,
                            terminal=ExecutionStopTerminal(
                                outcome="cancelled", evidence="not_started", cleanup_verified=True
                            ),
                        ),
                        deadline=time.monotonic() + 5,
                    )
            assert (
                store.request_stop(request, deadline=time.monotonic() + 5).request_id
                == stopped.request_id
            )
            final = store.inspect_stop(scope, deadline=time.monotonic() + 5)
            assert final is not None and final.terminal is not None
            assert final.terminal.outcome == "cancelled"


def test_stop_rollback_lost_commit_ack_and_restart_never_restore_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jeff.application.execution import ExecutionStopRequest

    for mode in ("rollback", "lost_ack"):
        with authority_store(tmp_path / mode) as (store, project, manifest, generation):
            record = store.authority(
                command(
                    project,
                    generation,
                    operation="propose",
                    manifest=manifest,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            ).record
            store.authority(
                command(
                    project,
                    generation,
                    operation="approve",
                    approval_id=record.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
            request = ExecutionStopRequest(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
                approval_id=record.approval_id,
                request_id=uuid4(),
                manifest_digest=manifest.fingerprint,
            )
            commit = store._commit_write

            def failed_commit(
                connection: sqlite3.Connection,
                fd: int,
                *,
                _mode: str = mode,
                _commit: Callable[[sqlite3.Connection, int], None] = commit,
            ) -> None:
                if _mode == "lost_ack":
                    _commit(connection, fd)
                    raise StorageFault("storage_outcome_unconfirmed")
                raise StorageFault("storage_busy")

            with monkeypatch.context() as patch:
                patch.setattr(store, "_commit_write", failed_commit)
                with pytest.raises(ReviewFault):
                    store.request_stop(request, deadline=time.monotonic() + 5)
            if mode == "rollback":
                from jeff.application.execution import ExecutionInspectionScope

                scope = ExecutionInspectionScope.model_validate(
                    request.model_dump(exclude={"request_id", "manifest_digest", "reason"})
                )
                assert store.inspect_stop(scope, deadline=time.monotonic() + 5) is None
                shown = store.authority(
                    command(project, generation, operation="show", approval_id=record.approval_id),
                    deadline=time.monotonic() + 5,
                )
                assert shown.record.state.value == "approved" and shown.launch_claim is None
            else:
                store.close(timeout=5)
                new_generation = uuid4()
                reopened = SQLiteStorage(store.owner, instance_id=new_generation)
                reopened.start()
                try:
                    assert reopened.wait_ready(5).state == "ready"
                    shown = reopened.authority(
                        command(
                            project,
                            new_generation,
                            operation="show",
                            approval_id=record.approval_id,
                        ),
                        deadline=time.monotonic() + 5,
                    )
                    assert shown.record.state.value == "invalidated" and shown.launch_claim is None
                    with pytest.raises(ReviewFault):
                        reopened.request_stop(
                            request.model_copy(update={"owner_instance_id": new_generation}),
                            deadline=time.monotonic() + 5,
                        )
                    with pytest.raises(ReviewFault):
                        reopened.authority(
                            command(
                                project,
                                new_generation,
                                operation="claim",
                                approval_id=record.approval_id,
                                current_manifest_digest=manifest.fingerprint,
                            ),
                            deadline=time.monotonic() + 5,
                        )
                finally:
                    reopened.close(timeout=5)


def test_stop_replay_keeps_first_utc_and_budgets_without_restarting_a_timer(tmp_path: Path) -> None:
    from jeff.application.execution import ExecutionInspectionScope, ExecutionStopRequest

    clock = {"wall": 100_000, "mono": 100}
    with authority_store(tmp_path, clock) as (store, project, manifest, generation):
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
        first = store.request_stop(request, deadline=time.monotonic() + 5)
        assert first.requested_at_ms == 100_000 and first.clock_confirmed
        clock.update(wall=120_000, mono=120)
        assert store.request_stop(request, deadline=time.monotonic() + 5) == first
        assert (
            store.request_stop(
                request.model_copy(
                    update={
                        "request_id": uuid4(),
                        "reason": "shutdown",
                    }
                ),
                deadline=time.monotonic() + 5,
            )
            == first
        )
        assert (first.grace_seconds, first.cleanup_seconds, first.completion_seconds) == (5, 10, 5)
        assert "monotonic" not in first.model_dump_json()

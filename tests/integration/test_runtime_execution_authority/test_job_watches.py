"""Durable watch registration and ACK across real SQLite owner reopen."""

import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from jeff.application.execution import ReviewFault
from jeff.application.execution.watch import WatchOperation, WatchScope
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_authority import authority_store
from .support_launch import completion, fact, launch_intent


@pytest.mark.parametrize("interrupted", [False, True])
def test_watch_survives_reopen_and_ack_is_idempotent(tmp_path: Path, interrupted: bool) -> None:
    def deadline() -> float:
        return time.monotonic() + 5

    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        job = intent.record.job_id

        def scope(
            owner: SQLiteStorage, operation: WatchOperation, target: UUID | None = None
        ) -> WatchScope:
            assert owner.instance_id is not None
            return WatchScope(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=owner.instance_id,
                operation=operation,
                target=target,
            )

        register = scope(store, "watch.job", job)
        first = store.watch(register, deadline=deadline())
        assert first == store.watch(register, deadline=deadline())
        assert not store.watch(scope(store, "watch.pending"), deadline=deadline()).notifications
        notification = first.notification_id
        assert notification is not None
        with pytest.raises(ReviewFault, match="execution_watch_not_ready"):
            store.watch(scope(store, "watch.ack", notification), deadline=deadline())
        if not interrupted:
            for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
                store.record(fact(intent, phase), deadline=deadline())
            store.complete_execution(completion(intent), deadline=deadline())
        store.close()

        def reopen() -> SQLiteStorage:
            replacement = SQLiteStorage(store.owner, instance_id=uuid4())
            replacement.start()
            assert replacement.wait_ready(5).schema_version == 12
            return replacement

        restarted = reopen()
        try:
            query = scope(restarted, "watch.pending")
            inbox = restarted.watch(query, deadline=deadline())
            assert inbox == restarted.watch(query, deadline=deadline())
            [notice] = inbox.notifications
            assert (notice.notification_id, notice.job_id) == (notification, job)
            assert (notice.outcome, notice.exit_code) == (
                ("interrupted", None) if interrupted else ("failed", 7)
            )
            ack = scope(restarted, "watch.ack", notification)
            assert restarted.watch(ack, deadline=deadline()).acknowledged_id == notification
            assert restarted.watch(ack, deadline=deadline()).acknowledged_id == notification
        finally:
            restarted.close()
        after_ack = reopen()
        try:
            assert after_ack.watch(scope(after_ack, "watch.job", job), deadline=deadline()) == first
            assert not after_ack.watch(
                scope(after_ack, "watch.pending"), deadline=deadline()
            ).notifications
        finally:
            after_ack.close()

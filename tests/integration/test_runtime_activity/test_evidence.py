"""Atomic lifecycle/replay, identity/event guards and real receipt/event transaction faults."""

import os
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.history import ActivityRecord, GitObservation, ReplayScope
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import ReceiptClaim, ReceiptScope, ReceiptTransition, StorageFault
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


def test_atomic_lifecycle_evidence_scoped_replay_no_write_and_restart(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project = uuid4(), uuid4()
    claim = ReceiptClaim(
        request_id=uuid4(),
        owner_instance_id=instance,
        operation="ask.branch",
        canonical_root=tmp_path / "p",
        project_id=project,
    )
    scope = ReceiptScope(
        request_id=claim.request_id, canonical_root=claim.canonical_root, project_id=project
    )

    def deadline() -> float:
        return time.monotonic() + 5

    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(owner, instance_id=instance)
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            store.claim(claim, deadline=deadline())
            receipt, trace = store.trace(scope, deadline=deadline())
            assert receipt is not None and trace is not None
            assert [event.kind for event in trace.events] == ["request.accepted"]
            epoch = trace.database_epoch
            store.finish(
                ReceiptTransition(
                    request_id=claim.request_id, owner_instance_id=instance, state="running"
                ),
                deadline=deadline(),
            )
            store.observe(
                ActivityRecord(
                    request_id=claim.request_id,
                    owner_instance_id=instance,
                    git=GitObservation(
                        category="status.branch",
                        process_started=True,
                        code="ok",
                        elapsed_ms=2,
                        output_bytes=81,
                    ),
                ),
                deadline=deadline(),
            )
            store.finish(
                ReceiptTransition(
                    request_id=claim.request_id,
                    owner_instance_id=instance,
                    state="succeeded",
                    outcome_code="ok",
                ),
                deadline=deadline(),
            )
            before = layout.database.read_bytes(), layout.database.stat().st_mtime_ns
            again = store.claim(claim, deadline=deadline())
            assert not again.created
            receipt, trace = store.trace(scope, deadline=deadline())
            assert trace is not None and receipt is not None and receipt.state == "succeeded"
            assert [event.kind for event in trace.events] == [
                "request.accepted",
                "request.running",
                "git.finished",
                "request.succeeded",
            ]
            assert (layout.database.read_bytes(), layout.database.stat().st_mtime_ns) == before
            replay = store.replay(
                ReplayScope(project_id=project, canonical_root=claim.canonical_root, limit=2),
                deadline=deadline(),
            )
            assert replay.events == trace.events[:2] and replay.has_more
            rest = store.replay(
                ReplayScope(
                    project_id=project,
                    canonical_root=claim.canonical_root,
                    after=replay.next_after,
                    expected_epoch=epoch,
                ),
                deadline=deadline(),
            )
            assert rest.events == trace.events[2:] and not rest.has_more
            assert not store.replay(
                ReplayScope(project_id=uuid4(), canonical_root=claim.canonical_root),
                deadline=deadline(),
            ).events
            with pytest.raises(StorageFault):
                store.trace(scope.model_copy(update={"project_id": uuid4()}), deadline=deadline())
            for update in ({"after": trace.high_water + 1}, {"expected_epoch": uuid4()}):
                with pytest.raises(StorageFault) as invalid:
                    store.replay(
                        ReplayScope(
                            project_id=project, canonical_root=claim.canonical_root, **update
                        ),
                        deadline=deadline(),
                    )
                assert invalid.value.code == "activity_invalid_cursor"
            assert (layout.database.read_bytes(), layout.database.stat().st_mtime_ns) == before
        finally:
            store.close()
        reopened = SQLiteStorage(owner, instance_id=uuid4())
        reopened.start()
        try:
            assert reopened.wait_ready(5).schema_version == 12
            _, persisted = reopened.trace(scope, deadline=deadline())
            assert (
                persisted is not None
                and persisted.database_epoch == epoch
                and persisted.events == trace.events
            )
        finally:
            reopened.close()


def test_nullable_identity_routing_budget_and_owner_guards(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project = uuid4(), uuid4()
    claim = ReceiptClaim(
        request_id=uuid4(),
        owner_instance_id=instance,
        operation="init",
        canonical_root=tmp_path / "p",
        project_id=None,
    )
    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(owner, instance_id=instance)
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            store.claim(claim, deadline=time.monotonic() + 5)
            store.finish(
                ReceiptTransition(
                    request_id=claim.request_id, owner_instance_id=instance, state="running"
                ),
                deadline=time.monotonic() + 5,
            )
            store.finish(
                ReceiptTransition(
                    request_id=claim.request_id,
                    owner_instance_id=instance,
                    state="running",
                    project_id=project,
                ),
                deadline=time.monotonic() + 5,
            )
            for _ in range(27):
                store.observe(
                    ActivityRecord(
                        request_id=claim.request_id,
                        owner_instance_id=instance,
                        registration_created=False,
                    ),
                    deadline=time.monotonic() + 5,
                )
            with pytest.raises(StorageFault) as full:
                store.observe(
                    ActivityRecord(
                        request_id=claim.request_id,
                        owner_instance_id=instance,
                        registration_created=False,
                    ),
                    deadline=time.monotonic() + 5,
                )
            assert (
                full.value.code == "activity_budget_exceeded" and store.snapshot().state == "ready"
            )
            with pytest.raises(StorageFault):
                store.observe(
                    ActivityRecord(
                        request_id=claim.request_id,
                        owner_instance_id=uuid4(),
                        registration_created=False,
                    ),
                    deadline=time.monotonic() + 5,
                )
            store.finish(
                ReceiptTransition(
                    request_id=claim.request_id,
                    owner_instance_id=instance,
                    state="unknown",
                    outcome_code="partial",
                ),
                deadline=time.monotonic() + 5,
            )
            page = store.replay(
                ReplayScope(project_id=project, canonical_root=claim.canonical_root),
                deadline=time.monotonic() + 5,
            )
            assert len(page.events) == 31
            assert page.events[0].project_id is None and page.events[2].project_id == project
            assert page.events[-1].kind == "request.unknown"
        finally:
            store.close()
        again = SQLiteStorage(owner, instance_id=uuid4())
        again.start()
        try:
            assert again.wait_ready(5).state == "ready"
            assert (
                again.replay(
                    ReplayScope(project_id=project, canonical_root=claim.canonical_root),
                    deadline=time.monotonic() + 5,
                ).events
                == page.events
            )
        finally:
            again.close()


@pytest.mark.parametrize("fault", ["rollback", "lost_ack"])
def test_real_receipt_and_event_transaction_failure_never_replays_or_fakes_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance = uuid4()
    claim = ReceiptClaim(
        request_id=uuid4(),
        owner_instance_id=instance,
        operation="ask.branch",
        canonical_root=tmp_path / "p",
        project_id=uuid4(),
    )
    original = sqlite3.connect

    class FailedEvidence(sqlite3.Connection):
        armed = False

        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            if sql.startswith("INSERT INTO activity_events") and parameters[6] == "request.running":
                self.armed = True
                if fault == "rollback":
                    raise sqlite3.OperationalError("SYNTHETIC_PRIVATE_EVENT")
            result = super().execute(sql, parameters)
            if sql == "COMMIT" and self.armed and fault == "lost_ack":
                raise sqlite3.OperationalError("SYNTHETIC_PRIVATE_ACK")
            return result

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=FailedEvidence)

    with owner:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            owner.store.credential(owner)
            store = SQLiteStorage(owner, instance_id=instance)
            store.start()
            try:
                assert store.wait_ready(5).state == "ready"
                store.claim(claim, deadline=time.monotonic() + 5)
                with pytest.raises(StorageFault) as failed:
                    store.finish(
                        ReceiptTransition(
                            request_id=claim.request_id, owner_instance_id=instance, state="running"
                        ),
                        deadline=time.monotonic() + 5,
                    )
                assert "SYNTHETIC_PRIVATE" not in str(failed.value)
            finally:
                store.close()
        again = SQLiteStorage(owner, instance_id=instance)
        again.start()
        try:
            assert again.wait_ready(5).state == "ready"
            receipt, trace = again.trace(
                ReceiptScope(
                    request_id=claim.request_id,
                    canonical_root=claim.canonical_root,
                    project_id=claim.project_id,
                ),
                deadline=time.monotonic() + 5,
            )
            assert receipt is not None and trace is not None
            expected = (
                ["request.accepted"]
                if fault == "rollback"
                else ["request.accepted", "request.running"]
            )
            assert [event.kind for event in trace.events] == expected
            assert receipt.state == ("accepted" if fault == "rollback" else "running")
            assert not again.claim(claim, deadline=time.monotonic() + 5).created
        finally:
            again.close()
    assert b"SYNTHETIC_PRIVATE" not in layout.database.read_bytes()

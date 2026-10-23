"""Public scoped SQL-owner diagnostics: acceptance order, snapshot pages and no writes."""

import os
import time
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.history import HistoryCursor, HistoryRead, HistoryScope
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import ReceiptClaim, ReceiptTransition, StorageFault
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


def test_latest_debug_is_scoped_acceptance_order_and_does_not_write(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project = uuid4(), uuid4()
    scope = HistoryScope(project_id=project, canonical_root=tmp_path / "p")
    with owner:
        store = SQLiteStorage(owner, instance_id=instance)
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            with pytest.raises(StorageFault) as empty:
                store.debug(scope, deadline=time.monotonic() + 5)
            assert empty.value.code == "no_history"
            assert store.snapshot().state == "ready"
            first = ReceiptClaim(
                request_id=uuid4(),
                owner_instance_id=instance,
                operation="ask.branch",
                canonical_root=scope.canonical_root,
                project_id=project,
            )
            store.claim(first, deadline=time.monotonic() + 5)
            store.claim(
                first.model_copy(update={"request_id": uuid4(), "project_id": uuid4()}),
                deadline=time.monotonic() + 5,
            )
            before = layout.database.read_bytes(), layout.database.stat().st_mtime_ns
            receipt, trace = store.debug(scope, deadline=time.monotonic() + 5)
            assert receipt is not None and receipt.request_id == first.request_id
            assert receipt.state == "accepted" and trace is not None
            assert trace.events[0].kind == "request.accepted"
            assert before == (layout.database.read_bytes(), layout.database.stat().st_mtime_ns)
            page = store.history(
                HistoryRead(**scope.model_dump(), limit=1), deadline=time.monotonic() + 5
            )
            assert [item.request_id for item in page.items] == [first.request_id]
            assert page.next_cursor is None
            nullable = first.model_copy(
                update={"request_id": uuid4(), "project_id": None, "operation": "init"}
            )
            store.claim(nullable, deadline=time.monotonic() + 5)
            store.finish(
                ReceiptTransition(
                    request_id=nullable.request_id, owner_instance_id=instance, state="running"
                ),
                deadline=time.monotonic() + 5,
            )
            newer = first.model_copy(update={"request_id": uuid4()})
            store.claim(newer, deadline=time.monotonic() + 5)
            read = HistoryRead(**scope.model_dump(), limit=1)
            snapshot = store.history(read, deadline=time.monotonic() + 5)
            assert snapshot.items[0].request_id == newer.request_id
            assert snapshot.next_cursor is not None
            store.finish(
                ReceiptTransition(
                    request_id=nullable.request_id,
                    owner_instance_id=instance,
                    state="running",
                    project_id=project,
                ),
                deadline=time.monotonic() + 5,
            )
            rest = store.history(
                read.model_copy(update={"cursor": snapshot.next_cursor}),
                deadline=time.monotonic() + 5,
            )
            assert [item.request_id for item in rest.items] == [first.request_id]
            assert rest.next_cursor is None
        finally:
            store.close()


def test_history_snapshot_pages_are_scope_bound_stable_and_read_only(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project = uuid4(), uuid4()
    scope = HistoryRead(project_id=project, canonical_root=tmp_path / "p", limit=2)
    with owner:
        store = SQLiteStorage(owner, instance_id=instance)
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            empty = store.history(scope, deadline=time.monotonic() + 5)
            assert not empty.items and empty.next_cursor is None
            claims = [
                ReceiptClaim(
                    request_id=uuid4(),
                    owner_instance_id=instance,
                    operation="ask.branch",
                    canonical_root=scope.canonical_root,
                    project_id=project,
                )
                for _ in range(4)
            ]
            for claim in claims[:3]:
                store.claim(claim, deadline=time.monotonic() + 5)
                store.claim(
                    claim.model_copy(update={"request_id": uuid4(), "project_id": uuid4()}),
                    deadline=time.monotonic() + 5,
                )
            # Complete the oldest first/last independently of acceptance ordering.
            store.finish(
                ReceiptTransition(
                    request_id=claims[0].request_id,
                    owner_instance_id=instance,
                    state="failed",
                    outcome_code="operation_failed",
                ),
                deadline=time.monotonic() + 5,
            )
            page = store.history(scope, deadline=time.monotonic() + 5)
            assert [i.request_id for i in page.items] == [
                claims[2].request_id,
                claims[1].request_id,
            ]
            assert page.next_cursor is not None
            store.claim(claims[3], deadline=time.monotonic() + 5)
            before = layout.database.read_bytes(), layout.database.stat().st_mtime_ns
            rest = store.history(
                scope.model_copy(update={"cursor": page.next_cursor}), deadline=time.monotonic() + 5
            )
            assert [i.request_id for i in rest.items] == [claims[0].request_id]
            assert rest.next_cursor is None and rest.items[0].state == "failed"
            assert rest.snapshot_high_water == page.snapshot_high_water
            cursor = HistoryCursor.decode(page.next_cursor)
            for changed in (
                {"database_epoch": uuid4()},
                {"project_id": uuid4()},
                {"canonical_root": tmp_path / "foreign"},
                {"retention_generation": 1},
                {"snapshot_high_water": rest.high_water + 1},
            ):
                invalid = cursor.model_copy(update=changed).encode()
                with pytest.raises(StorageFault) as refused:
                    store.history(
                        scope.model_copy(update={"cursor": invalid}), deadline=time.monotonic() + 5
                    )
                assert refused.value.code == "activity_invalid_cursor"
                assert store.snapshot().state == "ready"
            assert before == (layout.database.read_bytes(), layout.database.stat().st_mtime_ns)
        finally:
            store.close()

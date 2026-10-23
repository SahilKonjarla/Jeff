"""Committed observed-identity pages through the SQL-owner interface, not SQL assertions."""

import os
import time
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.history import ReplayScope, StreamCursor, StreamRead
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import ReceiptClaim, ReceiptTransition, StorageFault
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


def test_stream_never_retrofits_null_identity_and_advances_empty_intervals(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project = uuid4(), uuid4()
    with owner:
        store = SQLiteStorage(owner, instance_id=instance)
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            scope = StreamRead(project_id=project, canonical_root=tmp_path / "p")
            live = store.stream_page(scope, deadline=time.monotonic() + 5)
            saved = live.position(live.scanned_through).encode()
            assert StreamCursor.decode(saved).after == 0
            claim = ReceiptClaim(
                request_id=uuid4(),
                owner_instance_id=instance,
                operation="init",
                canonical_root=scope.canonical_root,
                project_id=None,
            )
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
            page = store.stream_page(
                scope.model_copy(update={"cursor": saved}), deadline=time.monotonic() + 5
            )
            assert [event.kind for event in page.events] == ["identity.established"]
            assert [event.sequence for event in page.events] == [3]
            old = store.replay(
                ReplayScope(project_id=project, canonical_root=scope.canonical_root),
                deadline=time.monotonic() + 5,
            )
            assert [event.sequence for event in old.events] == [1, 2, 3]
            store.claim(
                claim.model_copy(update={"request_id": uuid4(), "project_id": uuid4()}),
                deadline=time.monotonic() + 5,
            )
            before = layout.database.read_bytes(), layout.database.stat().st_mtime_ns
            empty = store.stream_page(
                scope.model_copy(update={"cursor": page.position(3).encode()}),
                deadline=time.monotonic() + 5,
            )
            assert not empty.events and empty.scanned_through == 4
            assert before == (layout.database.read_bytes(), layout.database.stat().st_mtime_ns)
            cursor = page.position(3)
            for change in (
                {"database_epoch": uuid4()},
                {"project_id": uuid4()},
                {"canonical_root": tmp_path / "other"},
                {"after": 5},
                {"retention_generation": 1},
            ):
                with pytest.raises(StorageFault) as refused:
                    store.stream_page(
                        scope.model_copy(
                            update={"cursor": cursor.model_copy(update=change).encode()}
                        ),
                        deadline=time.monotonic() + 5,
                    )
                assert refused.value.code == "activity_invalid_cursor"
                assert store.snapshot().state == "ready"
            assert before == (layout.database.read_bytes(), layout.database.stat().st_mtime_ns)
        finally:
            store.close()


def test_delivery_hint_failure_cannot_rewrite_a_committed_receipt(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None

    def unavailable() -> None:
        raise RuntimeError("Synthetic scheduling failure")

    project, instance = uuid4(), uuid4()
    with owner:
        store = SQLiteStorage(owner, instance_id=instance, commit_hint=unavailable)
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            scope = StreamRead(project_id=project, canonical_root=tmp_path / "p")
            baseline = store.stream_page(scope, deadline=time.monotonic() + 5)
            claim = ReceiptClaim(
                request_id=uuid4(),
                owner_instance_id=instance,
                project_id=project,
                canonical_root=scope.canonical_root,
                operation="ask.branch",
            )
            result = store.claim(claim, deadline=time.monotonic() + 5)
            assert result.created and result.receipt.state == "accepted"
            assert not store.notification_healthy and store.snapshot().state == "ready"
            page = store.stream_page(
                scope.model_copy(update={"cursor": baseline.position(0).encode()}),
                deadline=time.monotonic() + 5,
            )
            assert [event.request_id for event in page.events] == [claim.request_id]
        finally:
            store.close()

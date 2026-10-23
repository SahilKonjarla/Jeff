"""Committed observed-identity pages through the SQL-owner interface, not SQL assertions."""

import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.history import HistoryRead, ReplayScope, StreamCursor, StreamRead
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import ReceiptClaim, ReceiptScope, ReceiptTransition, StorageFault
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
                {"retention_generation": page.retention_generation + 1},
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


@pytest.mark.parametrize("outcome", ["commit", "rollback", "lost_ack"])
def test_retention_position_is_checked_committed_and_published_before_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project = uuid4(), uuid4()
    wall = [0]
    armed, entered, release = threading.Event(), threading.Event(), threading.Event()
    original = sqlite3.connect

    class HeldCommit(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            if armed.is_set() and sql == "COMMIT":
                entered.set()
                if not release.wait(3):
                    raise sqlite3.OperationalError("Synthetic fixture gate expired")
                if outcome == "rollback":
                    raise sqlite3.OperationalError("Synthetic rollback")
            result = super().execute(sql, parameters)
            if armed.is_set() and sql == "COMMIT" and outcome == "lost_ack":
                raise sqlite3.OperationalError("Synthetic lost acknowledgement")
            return result

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=HeldCommit)

    with owner, monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        observed = []
        store = SQLiteStorage(
            owner,
            instance_id=instance,
            retention_clock=lambda: wall[0],
            commit_hint=lambda: observed.append(store.retention_position()),
        )
        assert store.retention_position() is None
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            before = store.retention_position()
            assert before is not None and before.replay_floor == 0
            claim = ReceiptClaim(
                request_id=uuid4(),
                owner_instance_id=instance,
                operation="ask.branch",
                canonical_root=tmp_path / "p",
                project_id=project,
            )
            store.claim(claim, deadline=time.monotonic() + 5)
            done = store.finish(
                ReceiptTransition(
                    request_id=claim.request_id,
                    owner_instance_id=instance,
                    state="succeeded",
                    outcome_code="ok",
                ),
                deadline=time.monotonic() + 5,
            )
            assert done.finished_at_ms is not None
            wall[0] = done.finished_at_ms + 30 * 86400000 + 1
            observed.clear()
            armed.set()
            with ThreadPoolExecutor(max_workers=1) as caller:
                retirement = caller.submit(store.retire, deadline=time.monotonic() + 5)
                try:
                    assert entered.wait(2)
                    assert store.retention_position() == before
                    assert not observed
                finally:
                    release.set()
                if outcome == "commit":
                    result = retirement.result(timeout=3)
                    current = store.retention_position()
                    assert current is not None
                    assert current.retention_generation == before.retention_generation + 1
                    assert current.replay_floor == result.replay_floor == 2
                    assert observed == [current]
                else:
                    with pytest.raises(StorageFault) as failed:
                        retirement.result(timeout=3)
                    assert failed.value.code == (
                        "storage_outcome_unconfirmed" if outcome == "lost_ack" else "storage_failed"
                    )
                    assert not observed and store.retention_position() is None
                assert (
                    before.replay_floor == 0
                )  # immutable old snapshot never retroactively changes
        finally:
            release.set()
            store.close()


def test_actual_expiry_invalidates_all_cursor_kinds_but_preserves_exact_unknown_trace(
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project = uuid4(), uuid4()
    wall = [0]
    read = HistoryRead(canonical_root=tmp_path / "p", project_id=project, limit=1)
    with owner:
        store = SQLiteStorage(owner, instance_id=instance, retention_clock=lambda: wall[0])
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            unknown = None
            finished = []
            for state in ("succeeded", "unknown", "failed"):
                claim = ReceiptClaim(
                    request_id=uuid4(),
                    owner_instance_id=instance,
                    operation="ask.branch",
                    canonical_root=read.canonical_root,
                    project_id=project,
                )
                store.claim(claim, deadline=time.monotonic() + 5)
                done = store.finish(
                    ReceiptTransition.model_validate(
                        {
                            "request_id": claim.request_id,
                            "owner_instance_id": instance,
                            "state": state,
                            "outcome_code": "ok" if state == "succeeded" else "partial",
                        }
                    ),
                    deadline=time.monotonic() + 5,
                )
                assert done.finished_at_ms is not None
                finished.append(done.finished_at_ms)
                if state == "unknown":
                    unknown = ReceiptScope(
                        request_id=claim.request_id,
                        canonical_root=read.canonical_root,
                        project_id=project,
                    )
            assert unknown is not None
            snapshot = store.history(read, deadline=time.monotonic() + 5)
            assert snapshot.next_cursor is not None
            stream = store.stream_page(
                StreamRead(canonical_root=read.canonical_root, project_id=project),
                deadline=time.monotonic() + 5,
            )
            saved_stream = stream.position(stream.scanned_through).encode()
            original_receipt, original_trace = store.trace(unknown, deadline=time.monotonic() + 5)
            wall[0] = max(finished) + 30 * 86400000 + 1
            retired = store.retire(deadline=time.monotonic() + 5)
            assert retired.retired_count == 2 and retired.replay_floor == 6
            with pytest.raises(StorageFault) as history_invalid:
                store.history(
                    read.model_copy(update={"cursor": snapshot.next_cursor}),
                    deadline=time.monotonic() + 5,
                )
            assert history_invalid.value.code == "activity_invalid_cursor"
            with pytest.raises(StorageFault) as stream_invalid:
                store.stream_page(
                    StreamRead(
                        canonical_root=read.canonical_root, project_id=project, cursor=saved_stream
                    ),
                    deadline=time.monotonic() + 5,
                )
            assert stream_invalid.value.code == "activity_invalid_cursor"
            replay = ReplayScope(canonical_root=read.canonical_root, project_id=project)
            with pytest.raises(StorageFault) as raw_invalid:
                store.replay(replay, deadline=time.monotonic() + 5)
            assert raw_invalid.value.code == "activity_invalid_cursor"
            boundary = store.replay(
                replay.model_copy(update={"after": retired.replay_floor}),
                deadline=time.monotonic() + 5,
            )
            assert not boundary.events and boundary.next_after == 6
            actual, trace = store.trace(unknown, deadline=time.monotonic() + 5)
            assert actual == original_receipt and trace is not None and original_trace is not None
            assert trace.events == original_trace.events
            assert trace.events[-1].sequence < retired.replay_floor
            fresh = store.history(read, deadline=time.monotonic() + 5)
            assert [item.request_id for item in fresh.items] == [unknown.request_id]
            assert fresh.next_cursor is None
        finally:
            store.close()

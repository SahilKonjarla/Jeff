"""Bounded maintenance under traffic, real SQLite busy backoff and lost commit acknowledgement."""

import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import ReceiptClaim, ReceiptTransition, StorageFault
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_schedule import _scheduled_progress


def test_scheduled_batches_are_bounded_and_progress_during_continuous_sql_traffic(
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance = uuid4()
    project = Project(uuid4(), tmp_path / "p")
    mono, wall = [100.0], [0]
    traffic_stop = threading.Event()
    traffic_ready = threading.Event()
    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(
            owner,
            instance_id=instance,
            scheduled_retention=True,
            retention_monotonic=lambda: mono[0],
            retention_clock=lambda: wall[0],
        )
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            store.index(project)
            for _ in range(101):
                claim = ReceiptClaim(
                    request_id=uuid4(),
                    owner_instance_id=instance,
                    canonical_root=project.root,
                    project_id=project.project_id,
                    operation="ask.branch",
                )
                store.claim(claim, deadline=time.monotonic() + 5)
                receipt = store.finish(
                    ReceiptTransition(
                        request_id=claim.request_id,
                        owner_instance_id=instance,
                        state="succeeded",
                        outcome_code="ok",
                    ),
                    deadline=time.monotonic() + 5,
                )
                assert receipt.finished_at_ms is not None
                wall[0] = receipt.finished_at_ms + 30 * 86400000 + 1

            def traffic() -> None:
                for _ in range(10):
                    assert store.lookup(project, deadline=time.monotonic() + 5) == project
                traffic_ready.set()
                while not traffic_stop.is_set():
                    assert store.lookup(project, deadline=time.monotonic() + 5) == project

            with ThreadPoolExecutor(max_workers=8) as callers:
                futures = [callers.submit(traffic) for _ in range(8)]
                try:
                    assert traffic_ready.wait(3)
                    mono[0] = 160
                    first = _scheduled_progress(store, 1)
                    assert first.attempts == 1 and first.next_attempt_at == 220
                    assert first.last_batch is not None
                    assert first.last_batch.retired_count == 100 and first.last_batch.has_more
                    assert all(not future.done() for future in futures)
                    time.sleep(0.12)
                    assert store.retention_snapshot().attempts == 1
                    mono[0] = 220
                    last = _scheduled_progress(store, 2)
                    assert last.last_batch is not None
                    assert last.last_batch.retired_count == 1 and not last.last_batch.has_more
                finally:
                    traffic_stop.set()
                    for future in futures:
                        future.result(timeout=5)
        finally:
            traffic_stop.set()
            store.close()


def test_actual_sqlite_busy_consumes_attempt_and_backoff_starts_after_completion(
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    mono = [100.0]
    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(owner, scheduled_retention=True, retention_monotonic=lambda: mono[0])
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            with closing(sqlite3.connect(layout.database, isolation_level=None)) as holder:
                holder.execute("BEGIN IMMEDIATE")
                try:
                    mono[0] = 160
                    deadline = time.monotonic() + 3
                    while not store.retention_snapshot().active and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert store.retention_snapshot().active
                    mono[0] = 170  # time passes while the real two-second busy wait is active
                    busy = _scheduled_progress(store, 1)
                    assert busy.last_outcome == "busy" and busy.error == "storage_busy"
                    assert busy.next_attempt_at == 230 and busy.last_batch is None
                    assert store.snapshot().state == "ready"
                finally:
                    holder.execute("ROLLBACK")
            time.sleep(0.12)
            assert store.retention_snapshot().attempts == 1
            mono[0] = 230
            retry = _scheduled_progress(store, 2)
            assert retry.attempts == 2 and retry.last_outcome == "committed"
            assert retry.error is None and retry.next_attempt_at == 290
        finally:
            store.close()


def test_scheduled_lost_commit_ack_closes_admission_without_retrying_retirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance = uuid4()
    mono, wall = [100.0], [0]
    original = sqlite3.connect

    class LostRetirementAck(sqlite3.Connection):
        retired = False

        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            result = super().execute(sql, parameters)
            if sql.startswith("INSERT INTO request_tombstones"):
                self.retired = True
            if sql == "COMMIT" and self.retired:
                raise sqlite3.OperationalError("SYNTHETIC_PRIVATE_RETIREMENT_ACK")
            return result

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=LostRetirementAck)

    with owner:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            owner.store.credential(owner)
            store = SQLiteStorage(
                owner,
                instance_id=instance,
                scheduled_retention=True,
                retention_clock=lambda: wall[0],
                retention_monotonic=lambda: mono[0],
            )
            store.start()
            try:
                assert store.wait_ready(5).state == "ready"
                claim = ReceiptClaim(
                    request_id=uuid4(),
                    owner_instance_id=instance,
                    canonical_root=tmp_path / "p",
                    project_id=uuid4(),
                    operation="ask.branch",
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
                mono[0] = 160
                failed = _scheduled_progress(store, 1)
                assert failed.last_outcome == "failed"
                assert failed.error == "storage_outcome_unconfirmed"
                assert failed.last_batch is None and failed.next_attempt_at is None
                assert store.snapshot().state == "failed"
                mono[0] = 1000
                time.sleep(0.12)
                assert store.retention_snapshot().attempts == 1
            finally:
                store.close()
        reopened = SQLiteStorage(owner, instance_id=instance)
        reopened.start()
        try:
            assert reopened.wait_ready(5).state == "ready"
            with pytest.raises(StorageFault) as retired:
                reopened.claim(claim, deadline=time.monotonic() + 5)
            assert retired.value.code == "request_retired"
        finally:
            reopened.close()

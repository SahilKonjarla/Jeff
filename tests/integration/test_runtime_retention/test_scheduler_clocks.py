"""Idle cadence, backward wall-clock deferral and invalid monotonic/wall samples."""

import os
import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import ReceiptClaim, ReceiptScope, ReceiptTransition, StorageFault
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_schedule import _scheduled_progress


def test_scheduled_retirement_runs_on_idle_owner_only_after_ready_plus_sixty_seconds(
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project = uuid4(), uuid4()
    wall, monotonic = [0], [100.0]
    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(
            owner,
            instance_id=instance,
            retention_clock=lambda: wall[0],
            retention_monotonic=lambda: monotonic[0],
            scheduled_retention=True,
        )
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            assert store.retention_snapshot().next_attempt_at == 160
            claim = ReceiptClaim(
                request_id=uuid4(),
                owner_instance_id=instance,
                canonical_root=tmp_path / "p",
                project_id=project,
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
            monotonic[0] = 159.999
            time.sleep(0.12)  # real owner idle polls, not a shortened production cadence
            assert store.retention_snapshot().attempts == 0
            monotonic[0] = 160
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                progress = store.retention_snapshot()  # cached only: no queued diagnostic SQL
                if progress.attempts == 1 and not progress.active:
                    break
                time.sleep(0.01)
            assert progress.last_outcome == "committed"
            assert progress.last_batch is not None and progress.last_batch.retired_count == 1
            assert progress.next_attempt_at == 220
            assert (
                store.inspect(
                    ReceiptScope(
                        request_id=claim.request_id,
                        canonical_root=claim.canonical_root,
                        project_id=project,
                    ),
                    deadline=time.monotonic() + 5,
                )
                is None
            )
        finally:
            store.close()


def test_observed_backward_wall_step_defers_even_when_the_receipt_would_look_old(
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project = uuid4(), uuid4()
    wall = [0]
    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(owner, instance_id=instance, retention_clock=lambda: wall[0])
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            claim = ReceiptClaim(
                request_id=uuid4(),
                owner_instance_id=instance,
                canonical_root=tmp_path / "p",
                project_id=project,
                operation="ask.branch",
            )
            accepted = store.claim(claim, deadline=time.monotonic() + 5).receipt
            wall[0] = accepted.accepted_at_ms + 30 * 86400000 + 10000
            assert store.retire(deadline=time.monotonic() + 5).retired_count == 0
            done = store.finish(
                ReceiptTransition(
                    request_id=claim.request_id,
                    owner_instance_id=instance,
                    state="succeeded",
                    outcome_code="ok",
                ),
                deadline=time.monotonic() + 5,
            )
            wall[0] -= 1
            with pytest.raises(StorageFault) as deferred:
                store.retire(deadline=time.monotonic() + 5)
            assert deferred.value.code == "storage_busy"
            assert (
                store.inspect(
                    ReceiptScope(
                        request_id=claim.request_id,
                        canonical_root=claim.canonical_root,
                        project_id=project,
                    ),
                    deadline=time.monotonic() + 5,
                )
                == done
            )
            assert store.snapshot().state == "ready"
            wall[0] += 1
            assert store.retire(deadline=time.monotonic() + 5).retired_count == 1
        finally:
            store.close()


@pytest.mark.parametrize("bad", [1e308, float(2**55 + 8), float("nan"), float("inf"), -1.0, True])
def test_unrepresentable_monotonic_interval_never_spins_expiry(tmp_path: Path, bad: Any) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(owner, scheduled_retention=True, retention_monotonic=lambda: bad)
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            time.sleep(0.12)
            progress = store.retention_snapshot()
            assert progress.attempts == 0 and progress.next_attempt_at is None
        finally:
            store.close()


@pytest.mark.parametrize("bad", [True, -1, 9223372036854775808, 1.5, "bad", OSError()])
def test_bad_wall_sample_defers_one_cadence_without_sql_failure_or_immediate_retry(
    tmp_path: Path,
    bad: Any,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    wall: list[Any] = [bad]
    mono = [100.0]

    def sample() -> int:
        if isinstance(wall[0], Exception):
            raise wall[0]
        return cast(int, wall[0])  # intentional clock-driver fault, not a valid typed value

    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(
            owner,
            scheduled_retention=True,
            retention_clock=sample,
            retention_monotonic=lambda: mono[0],
        )
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            mono[0] = 160
            deferred = _scheduled_progress(store, 1)
            assert deferred.attempts == 1 and deferred.last_outcome == "clock_deferred"
            assert deferred.next_attempt_at == 220 and deferred.last_batch is None
            assert store.snapshot().state == "ready"
            wall[0] = time.time_ns() // 1000000
            time.sleep(0.12)
            assert store.retention_snapshot().attempts == 1
            mono[0] = 220
            empty = _scheduled_progress(store, 2)
            assert empty.last_outcome == "committed" and empty.attempts == 2
            assert empty.last_batch is not None and empty.last_batch.retired_count == 0
            assert empty.next_attempt_at == 280
        finally:
            store.close()

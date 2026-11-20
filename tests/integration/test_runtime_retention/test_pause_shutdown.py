"""Stop/pause admission and actual in-flight retirement drain, never premature SQL release."""

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.runtime import RuntimeIdentityData, RuntimeLayout
from jeff.application.storage import ReceiptClaim, ReceiptTransition, StorageFault
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_ipc import ControlServer
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


@pytest.mark.parametrize("before_ready", [False, True])
def test_stop_acceptance_pauses_retention_without_sealing_ordinary_sql(
    tmp_path: Path,
    before_ready: bool,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    identity = RuntimeIdentityData(instance_id=uuid4(), pid=os.getpid())
    mono = [100.0]
    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(
            owner,
            instance_id=identity.instance_id,
            scheduled_retention=True,
            retention_monotonic=lambda: mono[0],
        )
        server = ControlServer(
            owner, b"\x00" * 32, identity, store.snapshot, on_stop=store.pause_retention
        )
        if before_ready:
            server.request_stop()
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            server.request_stop()
            assert server.stopping and store.retention_snapshot().paused
            assert store.retention_snapshot().next_attempt_at is None
            mono[0] = 1000
            time.sleep(0.12)
            assert store.retention_snapshot().attempts == 0
            project = Project(uuid4(), tmp_path / "p")
            assert store.index(project) == (project, True)
            with pytest.raises(StorageFault) as paused:
                store.retire(deadline=time.monotonic() + 5)
            assert paused.value.code == "storage_closing"
            assert store.snapshot().state == "ready"
            server.request_stop()  # idempotent, not another maintenance attempt
        finally:
            store.close()


def test_pause_and_close_do_not_count_a_running_retirement_as_released_sql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    identity = RuntimeIdentityData(instance_id=uuid4(), pid=os.getpid())
    mono, wall = [100.0], [0]
    armed, entered, release = threading.Event(), threading.Event(), threading.Event()
    original = sqlite3.connect

    class HeldRetirement(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            result = super().execute(sql, parameters)
            if armed.is_set() and sql.startswith("INSERT INTO request_tombstones"):
                entered.set()
                if not release.wait(5):
                    raise sqlite3.OperationalError("SYNTHETIC_FIXTURE_GATE_EXPIRED")
            return result

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=HeldRetirement)

    with owner:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            owner.store.credential(owner)
            store = SQLiteStorage(
                owner,
                instance_id=identity.instance_id,
                scheduled_retention=True,
                retention_clock=lambda: wall[0],
                retention_monotonic=lambda: mono[0],
            )
            store.start()
            try:
                assert store.wait_ready(5).state == "ready"
                claim = ReceiptClaim(
                    request_id=uuid4(),
                    owner_instance_id=identity.instance_id,
                    canonical_root=tmp_path / "p",
                    project_id=uuid4(),
                    operation="ask.branch",
                )
                store.claim(claim, deadline=time.monotonic() + 5)
                done = store.finish(
                    ReceiptTransition(
                        request_id=claim.request_id,
                        owner_instance_id=identity.instance_id,
                        state="succeeded",
                        outcome_code="ok",
                    ),
                    deadline=time.monotonic() + 5,
                )
                assert done.finished_at_ms is not None
                wall[0] = done.finished_at_ms + 30 * 86400000 + 1
                armed.set()
                mono[0] = 160
                assert entered.wait(3)
                server = ControlServer(
                    owner, b"\x00" * 32, identity, store.snapshot, on_stop=store.pause_retention
                )
                server.request_stop()
                assert store.retention_snapshot().paused and store.retention_snapshot().active
                with pytest.raises(StorageFault) as unconfirmed:
                    store.close(timeout=0)
                assert unconfirmed.value.code == "storage_outcome_unconfirmed"
                assert not store.closed
            finally:
                release.set()
                store.close(timeout=5)
            progress = store.retention_snapshot()
            assert store.closed and progress.paused and not progress.active
            assert progress.last_outcome == "committed" and progress.next_attempt_at is None
        reopened = SQLiteStorage(owner, instance_id=identity.instance_id)
        reopened.start()
        try:
            assert reopened.wait_ready(5).state == "ready"
            with pytest.raises(StorageFault) as retired:
                reopened.claim(claim, deadline=time.monotonic() + 5)
            assert retired.value.code == "request_retired"
        finally:
            reopened.close()

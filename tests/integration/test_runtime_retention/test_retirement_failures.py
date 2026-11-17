"""Retirement transaction faults, bounded corruption refusal and concurrent duplicate admission."""

import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import ReceiptClaim, ReceiptScope, ReceiptTransition, StorageFault
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


@pytest.mark.parametrize(
    "failed_sql",
    [
        "INSERT INTO request_tombstones",
        "DELETE FROM activity_events",
        "DELETE FROM activity_requests",
        "DELETE FROM request_receipts",
        "UPDATE activity_meta SET generation",
        "COMMIT",
    ],
)
def test_retirement_rollback_or_lost_commit_ack_never_creates_a_replay_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_sql: str,
) -> None:
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
    armed = [False]
    clock = [0]
    original = sqlite3.connect

    class FailedRetirement(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            result = super().execute(sql, parameters)
            if armed[0] and sql.startswith(failed_sql):
                armed[0] = False
                raise sqlite3.OperationalError("SYNTHETIC_PRIVATE_RETIREMENT")
            return result

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=FailedRetirement)

    with owner:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            owner.store.credential(owner)
            store = SQLiteStorage(owner, instance_id=instance, retention_clock=lambda: clock[0])
            store.start()
            try:
                assert store.wait_ready(5).state == "ready"
                store.claim(claim, deadline=time.monotonic() + 5)
                terminal = store.finish(
                    ReceiptTransition(
                        request_id=claim.request_id,
                        owner_instance_id=instance,
                        state="failed",
                        outcome_code="operation_failed",
                    ),
                    deadline=time.monotonic() + 5,
                )
                assert terminal.finished_at_ms is not None
                before = store.trace(scope, deadline=time.monotonic() + 5)
                clock[0] = terminal.finished_at_ms + 30 * 86400000 + 1
                armed[0] = True
                with pytest.raises(StorageFault) as failure:
                    store.retire(deadline=time.monotonic() + 5)
                assert not armed[0], "The actual SQLite mutation/commit must have been exercised"
                assert failure.value.code == (
                    "storage_outcome_unconfirmed" if failed_sql == "COMMIT" else "storage_failed"
                )
                assert "SYNTHETIC_PRIVATE_RETIREMENT" not in str(failure.value)
            finally:
                store.close()
        reopened = SQLiteStorage(owner, instance_id=instance)
        reopened.start()
        try:
            assert reopened.wait_ready(5).state == "ready"
            if failed_sql == "COMMIT":
                assert reopened.trace(scope, deadline=time.monotonic() + 5) == (None, None)
                with pytest.raises(StorageFault) as retired:
                    reopened.claim(claim, deadline=time.monotonic() + 5)
                assert retired.value.code == "request_retired"
            else:
                assert reopened.trace(scope, deadline=time.monotonic() + 5) == before
                assert not reopened.claim(claim, deadline=time.monotonic() + 5).created
        finally:
            reopened.close()


@pytest.mark.parametrize("corruption", ["excess_events", "uuid_overlap", "generation_limit"])
def test_retirement_refuses_corrupt_or_exhausted_metadata_without_partial_deletion(
    tmp_path: Path,
    corruption: str,
) -> None:
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
    with owner:
        owner.store.credential(owner)
        initial = SQLiteStorage(owner, instance_id=instance)
        initial.start()
        try:
            assert initial.wait_ready(5).state == "ready"
            initial.claim(claim, deadline=time.monotonic() + 5)
            done = initial.finish(
                ReceiptTransition(
                    request_id=claim.request_id,
                    owner_instance_id=instance,
                    state="succeeded",
                    outcome_code="ok",
                ),
                deadline=time.monotonic() + 5,
            )
            assert done.finished_at_ms is not None
        finally:
            initial.close()
        # Explicit owned structural/corruption fixture, not a functional SQL inspection oracle.
        with closing(sqlite3.connect(layout.database, isolation_level=None)) as fixture:
            if corruption == "excess_events":
                for sequence in range(3, 34):
                    fixture.execute(
                        "INSERT INTO activity_events(event_id,request_id,owner_instance_id,"
                        "canonical_root,project_id,scope_project_id,kind,recorded_at_ms) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (
                            sequence,
                            str(claim.request_id),
                            str(instance),
                            str(claim.canonical_root),
                            str(project),
                            str(project),
                            "request.running",
                            done.finished_at_ms,
                        ),
                    )
                fixture.execute("UPDATE activity_meta SET high_water=33")
            elif corruption == "uuid_overlap":
                fixture.execute(
                    "INSERT INTO request_tombstones VALUES (?)", (str(claim.request_id),)
                )
            else:
                fixture.execute("UPDATE activity_meta SET generation=9223372036854775807")
        before = layout.database.read_bytes()
        store = SQLiteStorage(
            owner,
            instance_id=instance,
            retention_clock=lambda: cast(int, done.finished_at_ms) + 30 * 86400000 + 1,
        )
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            with pytest.raises(StorageFault) as refused:
                if corruption == "uuid_overlap":
                    store.claim(claim, deadline=time.monotonic() + 5)
                else:
                    store.retire(deadline=time.monotonic() + 5)
            assert refused.value.code == (
                "storage_unrecognized" if corruption == "uuid_overlap" else "storage_failed"
            )
            assert store.snapshot().state == "failed" and store.retention_position() is None
            assert layout.database.read_bytes() == before
        finally:
            store.close()


def test_concurrent_expiry_and_duplicate_claim_never_reopens_request_admission(
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
            gate = threading.Barrier(5)

            def retire() -> int:
                gate.wait(timeout=3)
                return store.retire(deadline=time.monotonic() + 5).retired_count

            def duplicate() -> str:
                gate.wait(timeout=3)
                try:
                    result = store.claim(claim, deadline=time.monotonic() + 5)
                except StorageFault as fault:
                    assert fault.code == "request_retired"
                    return "retired"
                assert not result.created and result.receipt == done
                return "duplicate"

            with ThreadPoolExecutor(max_workers=5) as callers:
                expired = callers.submit(retire)
                claims = [callers.submit(duplicate) for _ in range(4)]
                assert expired.result(timeout=5) == 1
                assert all(
                    future.result(timeout=5) in ("retired", "duplicate") for future in claims
                )
            with pytest.raises(StorageFault) as retired:
                store.claim(claim, deadline=time.monotonic() + 5)
            assert retired.value.code == "request_retired" and store.snapshot().state == "ready"
        finally:
            store.close()

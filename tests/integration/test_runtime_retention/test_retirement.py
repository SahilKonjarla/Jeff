"""Atomic cutoff/tombstone semantics, preserved unfinished evidence and bounded UUID audit."""

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

from jeff.application.history import HistoryRead, ReplayScope, StreamRead
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import ReceiptClaim, ReceiptScope, ReceiptTransition, StorageFault
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


def test_terminal_retirement_removes_evidence_but_permanently_refuses_uuid(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance = uuid4()
    project = Project(uuid4(), tmp_path / "project")
    claim = ReceiptClaim(
        request_id=uuid4(),
        owner_instance_id=instance,
        operation="ask.branch",
        canonical_root=project.root,
        project_id=project.project_id,
    )
    scope = ReceiptScope(
        request_id=claim.request_id, canonical_root=project.root, project_id=project.project_id
    )
    read = HistoryRead(project_id=project.project_id, canonical_root=project.root)
    clock = [0]
    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(owner, instance_id=instance, retention_clock=lambda: clock[0])
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            assert store.index(project) == (project, True)
            assert store.claim(claim, deadline=time.monotonic() + 5).created
            terminal = store.finish(
                ReceiptTransition(
                    request_id=claim.request_id,
                    owner_instance_id=instance,
                    state="succeeded",
                    outcome_code="ok",
                ),
                deadline=time.monotonic() + 5,
            )
            assert terminal.finished_at_ms is not None
            before = store.history(read, deadline=time.monotonic() + 5)
            live = store.stream_page(
                StreamRead(project_id=project.project_id, canonical_root=project.root),
                deadline=time.monotonic() + 5,
            )
            cursor = live.position(live.scanned_through).encode()
            clock[0] = terminal.finished_at_ms + 30 * 86400000
            boundary = store.retire(deadline=time.monotonic() + 5)
            assert boundary.retired_count == 0
            assert boundary.retention_generation == before.retention_generation
            clock[0] += 1
            batch = store.retire(deadline=time.monotonic() + 5)
            assert batch.retired_count == 1 and not batch.has_more
            assert batch.database_epoch == before.database_epoch
            assert batch.high_water == before.high_water
            assert batch.retention_generation == before.retention_generation + 1
            assert batch.replay_floor == before.high_water
            assert store.inspect(scope, deadline=time.monotonic() + 5) is None
            assert store.trace(scope, deadline=time.monotonic() + 5) == (None, None)
            assert not store.history(read, deadline=time.monotonic() + 5).items
            for position in (
                ReplayScope(project_id=project.project_id, canonical_root=project.root),
                StreamRead(
                    project_id=project.project_id, canonical_root=project.root, cursor=cursor
                ),
            ):
                with pytest.raises(StorageFault) as invalid:
                    if isinstance(position, ReplayScope):
                        store.replay(position, deadline=time.monotonic() + 5)
                    else:
                        store.stream_page(position, deadline=time.monotonic() + 5)
                assert invalid.value.code == "activity_invalid_cursor"
            with pytest.raises(StorageFault) as retired:
                store.claim(claim, deadline=time.monotonic() + 5)
            assert retired.value.code == "request_retired"
            assert store.snapshot().state == "ready"
            assert store.lookup(project) == project
        finally:
            store.close()
        next_instance = uuid4()
        reopened = SQLiteStorage(owner, instance_id=next_instance)
        restarted_claim = claim.model_copy(update={"owner_instance_id": next_instance})
        reopened.start()
        try:
            assert reopened.wait_ready(5).schema_version == 4
            with pytest.raises(StorageFault) as retired:
                reopened.claim(restarted_claim, deadline=time.monotonic() + 5)
            assert retired.value.code == "request_retired"
            assert reopened.snapshot().state == "ready"
            assert reopened.claim(
                restarted_claim.model_copy(update={"request_id": uuid4()}),
                deadline=time.monotonic() + 5,
            ).created
        finally:
            reopened.close()


def test_retirement_preserves_all_unknown_accepted_running_evidence(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project = uuid4(), uuid4()
    clock = [0]
    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(owner, instance_id=instance, retention_clock=lambda: clock[0])
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            kept = []
            terminal_times = []
            states: tuple[Literal["unknown", "accepted", "running", "succeeded", "failed"], ...] = (
                "unknown",
                "accepted",
                "running",
                "succeeded",
                "failed",
            )
            for state in states:
                claim = ReceiptClaim(
                    request_id=uuid4(),
                    owner_instance_id=instance,
                    operation="ask.branch",
                    canonical_root=tmp_path / "p",
                    project_id=project,
                )
                store.claim(claim, deadline=time.monotonic() + 5)
                if state != "accepted":
                    receipt = store.finish(
                        ReceiptTransition(
                            request_id=claim.request_id,
                            owner_instance_id=instance,
                            state=state,
                            outcome_code=None
                            if state == "running"
                            else "ok"
                            if state == "succeeded"
                            else "partial",
                        ),
                        deadline=time.monotonic() + 5,
                    )
                    if receipt.finished_at_ms is not None:
                        terminal_times.append(receipt.finished_at_ms)
                if state in ("unknown", "accepted", "running"):
                    scope = ReceiptScope(
                        request_id=claim.request_id,
                        canonical_root=claim.canonical_root,
                        project_id=project,
                    )
                    kept.append((scope, store.trace(scope, deadline=time.monotonic() + 5)))
            clock[0] = max(terminal_times) + 30 * 86400000 + 1
            batch = store.retire(deadline=time.monotonic() + 5)
            assert batch.retired_count == 2
            for scope, (original_receipt, trace) in kept:
                actual, evidence = store.trace(scope, deadline=time.monotonic() + 5)
                assert actual == original_receipt and evidence is not None and trace is not None
                assert evidence.events == trace.events
                assert evidence.events[-1].sequence < batch.replay_floor
            no_op = store.retire(deadline=time.monotonic() + 5)
            assert no_op.retired_count == 0
            assert no_op.retention_generation == batch.retention_generation
        finally:
            store.close()


def test_retirement_batch_is_bounded_and_tombstones_store_only_uuid(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project = uuid4(), uuid4()
    clock = [0]
    retired_ids = set()
    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(owner, instance_id=instance, retention_clock=lambda: clock[0])
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            for _ in range(101):
                claim = ReceiptClaim(
                    request_id=uuid4(),
                    owner_instance_id=instance,
                    operation="ask.branch",
                    canonical_root=tmp_path / "p",
                    project_id=project,
                )
                retired_ids.add(str(claim.request_id))
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
                clock[0] = receipt.finished_at_ms + 30 * 86400000 + 1
            first = store.retire(deadline=time.monotonic() + 5)
            assert first.retired_count == 100 and first.has_more
            remaining = store.history(
                HistoryRead(project_id=project, canonical_root=tmp_path / "p"),
                deadline=time.monotonic() + 5,
            )
            assert len(remaining.items) == 1
            last = store.retire(deadline=time.monotonic() + 5)
            assert last.retired_count == 1 and not last.has_more
            assert last.retention_generation == first.retention_generation + 1
            assert last.high_water == first.high_water
            assert not store.history(
                HistoryRead(project_id=project, canonical_root=tmp_path / "p"),
                deadline=time.monotonic() + 5,
            ).items
        finally:
            store.close()
        # Approved structural/privacy audit ONLY; functional assertions above use the interface.
        # Freed pages may retain old bytes: this is not forensic erasure or a DB-wide marker scan.
        with closing(
            sqlite3.connect(layout.database.as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as audit:
            columns = audit.execute("PRAGMA table_info(request_tombstones)").fetchall()
            assert [(row[1], row[2], row[3], row[5]) for row in columns] == [
                ("request_id", "TEXT", 1, 1),
            ]
            rows = audit.execute("SELECT * FROM request_tombstones LIMIT 102").fetchall()
            assert {row[0] for row in rows} == retired_ids
            assert all(len(row) == 1 for row in rows)

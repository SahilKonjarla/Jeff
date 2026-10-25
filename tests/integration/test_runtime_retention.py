"""Retirement through the real SQL-owner interface, never arbitrary purge SQL."""

import json
import os
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import pytest

from jeff.application.dispatch import ProjectCall, ProjectDispatcher
from jeff.application.history import HistoryRead, ReplayScope, StreamRead
from jeff.application.projects import Failure, ProjectService
from jeff.application.questions import QuestionService, QuestionSuccess
from jeff.application.runtime import RuntimeIdentityData, RuntimeLayout
from jeff.application.storage import (
    ReceiptClaim,
    ReceiptScope,
    ReceiptTransition,
    RetentionProgress,
    StorageFault,
)
from jeff.domain.projects import Project
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_ipc import ControlServer
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage
from jeff.integrations.git import GitProjectDiscovery, GitRepositoryRead


def _scheduled_progress(store: SQLiteStorage, attempts: int) -> RetentionProgress:
    """Observe a completed scheduled attempt without submitting diagnostic SQL."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        progress = store.retention_snapshot()
        if progress.attempts >= attempts and not progress.active:
            return progress
        time.sleep(0.01)
    raise AssertionError("Scheduled owner attempt did not complete within the fixture bound")


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


def test_dispatcher_refuses_retired_uuid_as_known_nonacceptance_without_poisoning_storage(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    assert projects.execute("init", git_repo).ok
    identity = RuntimeIdentityData(instance_id=uuid4(), pid=os.getpid())
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    clock = [0]
    with owner:
        store = SQLiteStorage(
            owner, instance_id=identity.instance_id, retention_clock=lambda: clock[0]
        )
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            dispatch = ProjectDispatcher(
                projects,
                QuestionService(projects, GitRepositoryRead()),
                store,
                identity,
            )
            call = ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
            first = dispatch.execute(call, time.monotonic() + 10)
            assert isinstance(first.result, QuestionSuccess)
            assert first.receipt is not None and first.receipt.finished_at_ms is not None
            clock[0] = first.receipt.finished_at_ms + 30 * 86400000 + 1
            assert store.retire(deadline=time.monotonic() + 5).retired_count == 1
            again = dispatch.execute(call, time.monotonic() + 10)
            assert isinstance(again.result, Failure)
            assert again.result.error.code == "request_retired"
            assert again.accepted is False and again.receipt is None
            assert store.snapshot().state == "ready"
            configured = dispatch.execute(
                ProjectCall(operation="config", directory=git_repo), time.monotonic() + 10
            )
            assert configured.result.ok
        finally:
            store.close()


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


def test_retirement_preserves_all_unknown_accepted_running_evidence(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project = uuid4(), uuid4()
    clock = [0]
    with owner:
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


@pytest.mark.parametrize("migration_fault", ["index", "lost_ack", "busy"])
def test_actual_checkpoint_c_schema_three_migration_is_atomic_then_retirable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owned_runtime_launches: None,
    migration_fault: str,
) -> None:
    old = os.environ.get("JEFF_TEST_C_PYTHON")
    assert old is not None, "Requires an explicitly verified owned checkpoint-C package"
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    instance, project_id, request = uuid4(), uuid4(), uuid4()
    project = Project(project_id, tmp_path / "p")
    code = """
import json,os,sys,time
from pathlib import Path
from uuid import UUID
from jeff.application.history import HistoryRead
from jeff.application.runtime import PROTOCOL_REVISION,RuntimeLayout
from jeff.application.storage import ReceiptClaim,ReceiptTransition
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage
assert PROTOCOL_REVISION==5
layout=RuntimeLayout(data_dir=Path(sys.argv[1]),runtime_dir=Path(sys.argv[2]),uid=os.geteuid())
instance,project,request=map(UUID,sys.argv[3:6])
root=Path(sys.argv[6])
owner=RuntimeStateStore(layout).acquire(create=True)
assert owner is not None
with owner:
    store=SQLiteStorage(owner,instance_id=instance);store.start()
    try:
        assert store.wait_ready(5).schema_version==3
        store.index(Project(project,root))
        store.claim(ReceiptClaim(request_id=request,owner_instance_id=instance,project_id=project,
                    canonical_root=root,operation='ask.branch'),deadline=time.monotonic()+5)
        done=store.finish(ReceiptTransition(request_id=request,owner_instance_id=instance,
                          state='succeeded',outcome_code='ok'),deadline=time.monotonic()+5)
        page=store.history(HistoryRead(project_id=project,canonical_root=root),deadline=time.monotonic()+5)
        print(json.dumps({'finished':done.finished_at_ms,'epoch':str(page.database_epoch),
                          'generation':page.retention_generation,'high_water':page.high_water}))
    finally:store.close()
"""
    result = subprocess.run(
        [
            old,
            "-I",
            "-B",
            "-c",
            code,
            str(layout.data_dir),
            str(layout.runtime_dir),
            str(instance),
            str(project_id),
            str(request),
            str(project.root),
        ],
        env={"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, "Owned C schema3 fixture failed; raw output suppressed"
    facts = json.loads(result.stdout)
    owner = RuntimeStateStore(layout).acquire(create=False)
    assert owner is not None
    original = sqlite3.connect

    class FailedMigration(sqlite3.Connection):
        schema_four = False

        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            result = super().execute(sql, parameters)
            if sql == "PRAGMA user_version=4":
                self.schema_four = True
            if migration_fault == "index" and sql.startswith("CREATE INDEX receipts_expirable"):
                raise sqlite3.OperationalError("SYNTHETIC_PRIVATE_SCHEMA_FOUR")
            if migration_fault == "lost_ack" and self.schema_four and sql == "COMMIT":
                raise sqlite3.OperationalError("SYNTHETIC_PRIVATE_SCHEMA_FOUR_ACK")
            return result

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=FailedMigration)

    with owner:
        before = layout.database.read_bytes()
        with (
            monkeypatch.context() as patch,
            closing(original(layout.database, isolation_level=None)) as holder,
        ):
            if migration_fault == "busy":
                holder.execute("BEGIN IMMEDIATE")  # real two-second SQLite contention
            patch.setattr(sqlite3, "connect", connect)
            failing = SQLiteStorage(owner, instance_id=instance)
            failing.start()
            try:
                expected = {
                    "index": "storage_failed",
                    "lost_ack": "storage_outcome_unconfirmed",
                    "busy": "storage_busy",
                }
                assert failing.wait_ready(5).error == expected[migration_fault]
                if migration_fault != "lost_ack":
                    assert layout.database.read_bytes() == before
                else:
                    assert layout.database.read_bytes() != before  # actual schema4 COMMIT happened
            finally:
                failing.close()
                if holder.in_transaction:
                    holder.execute("ROLLBACK")
        store = SQLiteStorage(
            owner,
            instance_id=instance,
            retention_clock=lambda: facts["finished"] + 30 * 86400000 + 1,
        )
        store.start()
        try:
            assert store.wait_ready(5).schema_version == 4
            assert store.lookup(project) == project
            page = store.history(
                HistoryRead(project_id=project_id, canonical_root=project.root),
                deadline=time.monotonic() + 5,
            )
            assert str(page.database_epoch) == facts["epoch"]
            assert page.high_water == facts["high_water"]
            assert page.retention_generation == facts["generation"] + 1
            assert [item.request_id for item in page.items] == [request]
            assert store.retire(deadline=time.monotonic() + 5).retired_count == 1
            assert store.lookup(project) == project
        finally:
            store.close()


def test_actual_schema_two_legacy_only_expiry_keeps_unknown_and_never_backfills(
    tmp_path: Path,
    owned_runtime_launches: None,
) -> None:
    old = os.environ.get("JEFF_TEST_V3_PYTHON")
    assert old is not None, "Requires verified retained wire3/schema2 package"
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    old_instance, project_id = uuid4(), uuid4()
    project = Project(project_id, tmp_path / "p")
    requests = [uuid4() for _ in range(5)]
    code = """
import json,os,sys,time
from pathlib import Path
from uuid import UUID
from jeff.application.runtime import PROTOCOL_REVISION,RuntimeLayout
from jeff.application.storage import ReceiptClaim,ReceiptTransition
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage
assert PROTOCOL_REVISION==3
layout=RuntimeLayout(data_dir=Path(sys.argv[1]),runtime_dir=Path(sys.argv[2]),uid=os.geteuid())
instance,project=UUID(sys.argv[3]),UUID(sys.argv[4]);root=Path(sys.argv[5])
owner=RuntimeStateStore(layout).acquire(create=True)
assert owner is not None
with owner:
    store=SQLiteStorage(owner,instance_id=instance);store.start()
    try:
        assert store.wait_ready(5).schema_version==2
        store.index(Project(project,root));finished=[]
        states=('succeeded','failed','unknown','accepted','running')
        for request,state in zip(map(UUID,sys.argv[6:]),states):
            store.claim(ReceiptClaim(request_id=request,owner_instance_id=instance,project_id=project,
                        canonical_root=root,operation='ask.branch'),deadline=time.monotonic()+5)
            if state!='accepted':
                outcome=None if state=='running' else 'ok' if state=='succeeded' else 'partial'
                receipt=store.finish(ReceiptTransition(request_id=request,owner_instance_id=instance,
                    state=state,outcome_code=outcome),deadline=time.monotonic()+5)
                if receipt.finished_at_ms is not None:finished.append(receipt.finished_at_ms)
        print(json.dumps({'finished':max(finished)}))
    finally:store.close()
"""
    result = subprocess.run(
        [
            old,
            "-I",
            "-B",
            "-c",
            code,
            str(layout.data_dir),
            str(layout.runtime_dir),
            str(old_instance),
            str(project_id),
            str(project.root),
            *map(str, requests),
        ],
        env={"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, "Owned legacy-only fixture failed; raw output suppressed"
    clock = json.loads(result.stdout)["finished"] + 30 * 86400000 + 1
    owner = RuntimeStateStore(layout).acquire(create=False)
    assert owner is not None
    instance = uuid4()
    with owner:
        store = SQLiteStorage(owner, instance_id=instance, retention_clock=lambda: clock)
        store.start()
        try:
            assert store.wait_ready(5).schema_version == 4
            read = HistoryRead(project_id=project_id, canonical_root=project.root)
            before = store.history(read, deadline=time.monotonic() + 5)
            assert before.high_water == 0 and not before.items
            assert store.lookup(project) == project
            batch = store.retire(deadline=time.monotonic() + 5)
            assert batch.retired_count == 2 and batch.replay_floor == batch.high_water == 0
            assert batch.database_epoch == before.database_epoch
            assert batch.retention_generation == before.retention_generation + 1
            for index, request in enumerate(requests):
                scope = ReceiptScope(
                    request_id=request, canonical_root=project.root, project_id=project_id
                )
                receipt, trace = store.trace(scope, deadline=time.monotonic() + 5)
                if index < 2:
                    assert receipt is None and trace is None
                    with pytest.raises(StorageFault) as retired:
                        store.claim(
                            ReceiptClaim(
                                request_id=request,
                                owner_instance_id=instance,
                                operation="ask.branch",
                                canonical_root=project.root,
                                project_id=project_id,
                            ),
                            deadline=time.monotonic() + 5,
                        )
                    assert retired.value.code == "request_retired"
                else:
                    assert receipt is not None and receipt.state == "unknown"
                    assert trace is not None and trace.legacy_metadata_only and not trace.events
            assert not store.history(read, deadline=time.monotonic() + 5).items
            assert store.lookup(project) == project and store.snapshot().state == "ready"
        finally:
            store.close()


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


def test_scheduled_retirement_runs_on_idle_owner_only_after_ready_plus_sixty_seconds(
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project = uuid4(), uuid4()
    wall, monotonic = [0], [100.0]
    with owner:
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


@pytest.mark.parametrize("bad", [1e308, float(2**55 + 8), float("nan"), float("inf"), -1.0, True])
def test_unrepresentable_monotonic_interval_never_spins_expiry(tmp_path: Path, bad: Any) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
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


def test_huge_retention_days_are_safe_and_settings_reload_only_on_restart(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project = uuid4(), uuid4()
    mono, wall = [100.0], [0]
    huge = 10**100
    with owner:
        # Create ordinary private defaults, then change only this owned fixture's policy.
        initial = SQLiteStorage(owner)
        initial.start()
        try:
            assert initial.wait_ready(5).retention_days == 30
        finally:
            initial.close()
        layout.settings.write_text(f"schema_version: 1\nhistory:\n  retention_days: {huge}\n")
        store = SQLiteStorage(
            owner,
            instance_id=instance,
            scheduled_retention=True,
            retention_clock=lambda: wall[0],
            retention_monotonic=lambda: mono[0],
        )
        store.start()
        try:
            assert store.wait_ready(5).retention_days == huge
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
            wall[0] = done.finished_at_ms + 86400000 + 1
            mono[0] = 160
            before = _scheduled_progress(store, 1)
            assert before.last_batch is not None and before.last_batch.retired_count == 0
            layout.settings.write_text(
                "# restart only\nschema_version: 1\nhistory:\n  retention_days: 1\n"
            )
            mono[0] = 220
            live = _scheduled_progress(store, 2)
            assert live.last_batch is not None and live.last_batch.retired_count == 0
            assert store.snapshot().retention_days == huge
            assert live.last_batch.retention_generation == before.last_batch.retention_generation
        finally:
            store.close()
        restarted = SQLiteStorage(
            owner,
            instance_id=instance,
            scheduled_retention=True,
            retention_clock=lambda: wall[0],
            retention_monotonic=lambda: mono[0],
        )
        restarted.start()
        try:
            assert restarted.wait_ready(5).retention_days == 1
            assert restarted.retention_snapshot().attempts == 0
            assert restarted.retention_snapshot().next_attempt_at == 280
            mono[0] = 280
            expired = _scheduled_progress(restarted, 1)
            assert expired.last_batch is not None and expired.last_batch.retired_count == 1
            assert layout.settings.read_text().startswith("# restart only\n")
        finally:
            restarted.close()


def test_scheduler_is_disabled_by_default_even_when_both_clocks_advance(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    mono, wall = [100.0], [0]
    with owner:
        store = SQLiteStorage(
            owner, retention_monotonic=lambda: mono[0], retention_clock=lambda: wall[0]
        )
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            mono[0], wall[0] = 1000000, 9223372036854775807
            time.sleep(0.12)
            progress = store.retention_snapshot()
            assert not progress.enabled and progress.attempts == 0
            assert progress.next_attempt_at is None and progress.last_batch is None
        finally:
            store.close()


def test_sixty_second_delay_begins_after_sql_initialization_not_at_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    mono = [100.0]
    original = sqlite3.connect

    class StartupClockAdvance(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            result = super().execute(sql, parameters)
            if sql == "PRAGMA user_version=4":
                mono[0] = 500  # controlled time passes during actual schema initialization
            return result

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=StartupClockAdvance)

    with owner, monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        store = SQLiteStorage(owner, scheduled_retention=True, retention_monotonic=lambda: mono[0])
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            assert store.retention_snapshot().next_attempt_at == 560
            assert store.retention_snapshot().attempts == 0
            mono[0] = 559.999
            time.sleep(0.12)
            assert store.retention_snapshot().attempts == 0
            mono[0] = 560
            assert _scheduled_progress(store, 1).last_outcome == "committed"
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

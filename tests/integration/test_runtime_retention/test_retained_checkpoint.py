"""Actual retained-interpreter compatibility programs; requires verified external packages."""

import json
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.history import HistoryRead
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import ReceiptClaim, ReceiptScope, StorageFault
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


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
            owner.store.credential(owner)
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
            assert store.wait_ready(5).schema_version == 10
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
        owner.store.credential(owner)
        store = SQLiteStorage(owner, instance_id=instance, retention_clock=lambda: clock)
        store.start()
        try:
            assert store.wait_ready(5).schema_version == 10
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

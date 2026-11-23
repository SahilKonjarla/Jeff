"""Actual retained-v3 migration rollback and legacy coverage; requires its real interpreter."""

import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.history import ReplayScope
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import ReceiptScope
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


def test_actual_v3_schema_two_migration_rollback_legacy_coverage_and_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = os.environ.get("JEFF_TEST_V3_PYTHON")
    if old is None:
        pytest.skip("Requires explicitly verified owned v3 environment")
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    instance, project, request = uuid4(), uuid4(), uuid4()
    code = """
import os,sys,time
from pathlib import Path
from uuid import UUID
from jeff.application.runtime import PROTOCOL_REVISION,RuntimeLayout
from jeff.application.storage import ReceiptClaim,ReceiptTransition
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage
assert PROTOCOL_REVISION==3
layout=RuntimeLayout(data_dir=Path(sys.argv[1]),runtime_dir=Path(sys.argv[2]),uid=os.geteuid())
instance,project,request=map(UUID,sys.argv[3:6])
owner=RuntimeStateStore(layout).acquire(create=True)
assert owner is not None
with owner:
    store=SQLiteStorage(owner,instance_id=instance);store.start()
    try:
        assert store.wait_ready(5).schema_version==2
        store.index(Project(project,Path(sys.argv[6])))
        store.claim(ReceiptClaim(request_id=request,owner_instance_id=instance,project_id=project,canonical_root=Path(sys.argv[6]),operation='ask.branch'),deadline=time.monotonic()+5)
        store.finish(ReceiptTransition(request_id=request,owner_instance_id=instance,state='succeeded',outcome_code='ok'),deadline=time.monotonic()+5)
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
            str(project),
            str(request),
            str(tmp_path / "p"),
        ],
        env={"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        "Owned legacy storage fixture failed; raw driver output suppressed"
    )
    original = sqlite3.connect

    class FailedMigration(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            if sql.startswith("CREATE TABLE activity_events"):
                raise sqlite3.OperationalError("SYNTHETIC_PRIVATE_MIGRATION")
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=FailedMigration)

    owner = RuntimeStateStore(layout).acquire(create=False)
    assert owner is not None
    scope = ReceiptScope(request_id=request, project_id=project, canonical_root=tmp_path / "p")
    with owner:
        before = layout.database.read_bytes()
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            owner.store.credential(owner)
            failing = SQLiteStorage(owner, instance_id=uuid4())
            failing.start()
            try:
                assert failing.wait_ready(5).state == "failed"
                assert layout.database.read_bytes() == before
            finally:
                failing.close()
        store = SQLiteStorage(owner, instance_id=uuid4())
        store.start()
        try:
            assert store.wait_ready(5).schema_version == 12
            from jeff.domain.projects import Project

            assert store.lookup(Project(project, tmp_path / "p")) is not None
            receipt, trace = store.trace(scope, deadline=time.monotonic() + 5)
            assert receipt is not None and receipt.state == "succeeded"
            assert trace is not None and trace.legacy_metadata_only and not trace.events
            assert not store.replay(
                ReplayScope(project_id=project, canonical_root=tmp_path / "p"),
                deadline=time.monotonic() + 5,
            ).events
        finally:
            store.close()

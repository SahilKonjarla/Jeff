"""Real owner-thread receipts: claim once, finish, scoped read and restart uncertainty."""

import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import (
    DATABASE_APPLICATION_ID,
    ReceiptClaim,
    ReceiptScope,
    ReceiptTransition,
    StorageFault,
)
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


def test_receipt_claim_finish_lookup_and_restart_without_replay(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance = uuid4()
    claim = ReceiptClaim(
        request_id=uuid4(),
        owner_instance_id=instance,
        operation="ask.branch",
        canonical_root=tmp_path / "project",
        project_id=uuid4(),
    )
    scope = ReceiptScope(
        request_id=claim.request_id,
        canonical_root=claim.canonical_root,
        project_id=claim.project_id,
    )

    def deadline() -> float:
        return time.monotonic() + 5

    with owner:
        storage = SQLiteStorage(owner, instance_id=instance)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
            first = storage.claim(claim, deadline=deadline())
            assert first.created and first.receipt.state == "accepted"
            assert not storage.claim(claim, deadline=deadline()).created
            running = storage.finish(
                ReceiptTransition(
                    request_id=claim.request_id, owner_instance_id=instance, state="running"
                ),
                deadline=deadline(),
            )
            assert running.state == "running"
            assert storage.inspect(scope, deadline=deadline()) == running
            with pytest.raises(StorageFault) as wrong:
                storage.inspect(
                    scope.model_copy(update={"project_id": uuid4()}), deadline=deadline()
                )
            assert wrong.value.code == "receipt_scope_mismatch"
        finally:
            storage.close()
        again = SQLiteStorage(owner, instance_id=uuid4())
        again.start()
        try:
            assert again.wait_ready(5).schema_version == 4
            recovered = again.inspect(scope, deadline=deadline())
            assert recovered is not None and recovered.state == "unknown"
            assert recovered.outcome_code == "interrupted"
            assert not again.claim(claim, deadline=deadline()).created
            with pytest.raises(StorageFault):
                again.finish(
                    ReceiptTransition(
                        request_id=claim.request_id,
                        owner_instance_id=instance,
                        state="succeeded",
                        outcome_code="ok",
                    ),
                    deadline=deadline(),
                )
        finally:
            again.close()


def test_schema_one_registry_migrates_and_concurrent_claim_is_unique(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project_id = uuid4(), uuid4()
    project = Project(project_id, tmp_path / "registered")
    with owner:
        layout.data_dir.mkdir(mode=0o700)
        with closing(sqlite3.connect(layout.database, isolation_level=None)) as fixture:
            fixture.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
                "name TEXT NOT NULL UNIQUE)"
            )
            fixture.execute(
                "CREATE TABLE projects (project_id TEXT NOT NULL PRIMARY KEY, "
                "canonical_root TEXT NOT NULL UNIQUE)"
            )
            fixture.execute(
                "INSERT INTO projects VALUES (?,?)", (str(project_id), str(project.root))
            )
            fixture.execute("INSERT INTO schema_migrations VALUES (1,'registry_v1')")
            fixture.execute(f"PRAGMA application_id={DATABASE_APPLICATION_ID}")
            fixture.execute("PRAGMA user_version=1")
        layout.database.chmod(0o600)
        storage = SQLiteStorage(owner, instance_id=instance)
        storage.start()
        try:
            assert storage.wait_ready(5).schema_version == 4
            assert storage.lookup(project) == project
            claim = ReceiptClaim(
                request_id=uuid4(),
                owner_instance_id=instance,
                operation="ask.branch",
                canonical_root=project.root,
                project_id=project_id,
            )
            with ThreadPoolExecutor(max_workers=4) as callers:
                results = list(
                    callers.map(
                        lambda _: storage.claim(claim, deadline=time.monotonic() + 5), range(4)
                    )
                )
            assert sum(result.created for result in results) == 1
            assert len({result.receipt for result in results}) == 1
            with pytest.raises(StorageFault) as conflict:
                storage.claim(
                    claim.model_copy(update={"operation": "ask.changes"}),
                    deadline=time.monotonic() + 5,
                )
            assert conflict.value.code == "receipt_conflict"
            receipt = storage.finish(
                ReceiptTransition(
                    request_id=claim.request_id,
                    owner_instance_id=instance,
                    state="succeeded",
                    outcome_code="ok",
                ),
                deadline=time.monotonic() + 5,
            )
            before = layout.database.read_bytes(), layout.database.stat().st_mtime_ns
            assert (
                storage.inspect(
                    ReceiptScope(
                        request_id=claim.request_id,
                        canonical_root=project.root,
                        project_id=project_id,
                    ),
                    deadline=time.monotonic() + 5,
                )
                == receipt
            )
            assert (layout.database.read_bytes(), layout.database.stat().st_mtime_ns) == before
        finally:
            storage.close()


def test_actual_receipt_commit_acknowledgement_loss_is_inspectable_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance = uuid4()
    claim = ReceiptClaim(
        request_id=uuid4(),
        owner_instance_id=instance,
        operation="ask.branch",
        canonical_root=tmp_path / "p",
        project_id=uuid4(),
    )
    original = sqlite3.connect

    class LostAck(sqlite3.Connection):
        claimed = False

        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            result = super().execute(sql, parameters)
            if sql.startswith("INSERT INTO request_receipts"):
                self.claimed = True
            if sql == "COMMIT" and self.claimed:
                raise sqlite3.OperationalError("SYNTHETIC_PRIVATE_RECEIPT")
            return result

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=LostAck)

    with owner:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            storage = SQLiteStorage(owner, instance_id=instance)
            storage.start()
            try:
                assert storage.wait_ready(5).state == "ready"
                with pytest.raises(StorageFault) as lost:
                    storage.claim(claim, deadline=time.monotonic() + 5)
                assert lost.value.code == "storage_outcome_unconfirmed"
                assert "SYNTHETIC_PRIVATE_RECEIPT" not in str(lost.value)
            finally:
                storage.close()
        reopened = SQLiteStorage(owner, instance_id=instance)
        reopened.start()
        try:
            assert reopened.wait_ready(5).state == "ready"
            assert not reopened.claim(claim, deadline=time.monotonic() + 5).created
        finally:
            reopened.close()


def test_schema_two_migration_failure_rolls_back_actual_ddl_and_preserves_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    project = Project(uuid4(), tmp_path / "preserved")
    original = sqlite3.connect

    class FailedMigration(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            if sql.startswith("CREATE INDEX receipts_unfinished"):
                raise sqlite3.OperationalError("SYNTHETIC_PRIVATE_SCHEMA_TWO")
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=FailedMigration)

    with owner:
        initial = SQLiteStorage(owner)
        initial.start()
        try:
            assert initial.wait_ready(5).state == "ready"
            assert initial.index(project) == (project, True)
        finally:
            initial.close()
        # Construct a clean owned v1 fixture from current known schema, never real data.
        with closing(original(layout.database, isolation_level=None)) as fixture:
            fixture.execute("DROP TABLE activity_events")
            fixture.execute("DROP TABLE activity_requests")
            fixture.execute("DROP TABLE activity_meta")
            fixture.execute("DROP TABLE request_receipts")
            fixture.execute("DROP TABLE request_tombstones")
            fixture.execute("DELETE FROM schema_migrations WHERE version>=2")
            fixture.execute("PRAGMA user_version=1")
        before = layout.database.read_bytes()
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            failing = SQLiteStorage(owner)
            failing.start()
            try:
                failed = failing.wait_ready(5)
                assert failed.state == "failed" and failed.error == "storage_failed"
                assert layout.database.read_bytes() == before
            finally:
                failing.close()
        retry = SQLiteStorage(owner)
        retry.start()
        try:
            assert retry.wait_ready(5).schema_version == 4
            assert retry.lookup(project) == project
        finally:
            retry.close()


def test_init_identity_can_be_associated_once_and_diagnostic_exception_is_narrow(
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance = uuid4()
    claim = ReceiptClaim(
        request_id=uuid4(),
        owner_instance_id=instance,
        operation="init",
        canonical_root=tmp_path / "p",
        project_id=None,
    )
    with owner:
        storage = SQLiteStorage(owner, instance_id=instance)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
            assert storage.claim(claim, deadline=time.monotonic() + 5).created
            storage.finish(
                ReceiptTransition(
                    request_id=claim.request_id, owner_instance_id=instance, state="running"
                ),
                deadline=time.monotonic() + 5,
            )
            asserted_id = uuid4()
            scope = ReceiptScope(
                request_id=claim.request_id,
                canonical_root=claim.canonical_root,
                project_id=asserted_id,
            )
            with pytest.raises(StorageFault):
                storage.inspect(scope, deadline=time.monotonic() + 5)
            before = layout.database.read_bytes()
            unbound = storage.inspect(
                scope.model_copy(update={"allow_unbound_init": True}), deadline=time.monotonic() + 5
            )
            assert unbound is not None and unbound.project_id is None
            assert layout.database.read_bytes() == before
            associated = storage.finish(
                ReceiptTransition(
                    request_id=claim.request_id,
                    owner_instance_id=instance,
                    state="running",
                    project_id=asserted_id,
                ),
                deadline=time.monotonic() + 5,
            )
            assert associated.project_id == asserted_id
            assert storage.lookup(Project(asserted_id, claim.canonical_root)) is None
            with pytest.raises(StorageFault) as refused:
                storage.finish(
                    ReceiptTransition(
                        request_id=claim.request_id,
                        owner_instance_id=instance,
                        state="running",
                        project_id=uuid4(),
                    ),
                    deadline=time.monotonic() + 5,
                )
            assert refused.value.code == "receipt_conflict"
            with pytest.raises(StorageFault):
                storage.inspect(
                    scope.model_copy(update={"project_id": None, "allow_unbound_init": True}),
                    deadline=time.monotonic() + 5,
                )
        finally:
            storage.close()

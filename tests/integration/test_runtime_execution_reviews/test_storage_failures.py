"""Migration rollback, pre-DDL publication refusal and active-caller retirement uncertainty."""

import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import StorageFault
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_schema4 import schema4_registry


def test_failed_migration_ddl_rolls_back_schema4_and_registered_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_connect = sqlite3.connect
    failures = 0
    created_review_table = False
    with schema4_registry(tmp_path) as (owner, _project):
        layout = owner.store.layout
        before = layout.database.read_bytes()
        assert int.from_bytes(before[60:64], "big") == 4

        class FailingMigration(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
                nonlocal failures, created_review_table
                if sql.startswith("CREATE TABLE execution_operations"):
                    assert created_review_table
                    failures += 1
                    raise sqlite3.OperationalError("SYNTHETIC_MIGRATION_DDL_FAILURE")
                result = super().execute(sql, parameters)
                if sql.startswith("CREATE TABLE execution_reviews"):
                    created_review_table = True
                return result

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return original_connect(*args, **kwargs, factory=FailingMigration)

        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            storage = SQLiteStorage(owner)
            storage.start()
            try:
                ready = storage.wait_ready(5)
                assert ready.state == "failed"
                assert ready.error == "storage_failed"
                assert failures == 1 and created_review_table
            finally:
                storage.close(timeout=5)
        # Exact rollback preserves the whole legacy DB, including its registration.
        assert layout.database.read_bytes() == before
        assert not (layout.data_dir / "recovery.json").exists()
        assert not Path(str(layout.database) + "-journal").exists()


def test_failed_permit_publication_prevents_migration_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_connect, original_link = sqlite3.connect, os.link
    failures = 0
    migration_ddl: list[str] = []
    with schema4_registry(tmp_path) as (owner, _project):
        layout = owner.store.layout
        before = layout.database.read_bytes()

        class ObservedMigration(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
                if sql.startswith("CREATE TABLE execution_"):
                    migration_ddl.append(sql)
                return super().execute(sql, parameters)

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return original_connect(*args, **kwargs, factory=ObservedMigration)

        def link(source: Any, target: Any, *args: Any, **kwargs: Any) -> None:
            nonlocal failures
            if target == "recovery.json":
                failures += 1
                raise OSError("SYNTHETIC_PERMIT_PUBLICATION_FAILURE")
            original_link(source, target, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            patch.setattr(os, "link", link)
            storage = SQLiteStorage(owner)
            storage.start()
            try:
                ready = storage.wait_ready(5)
                assert ready.state == "failed"
                assert ready.error == "storage_recovery_required"
                assert failures == 1 and migration_ddl == []
            finally:
                storage.close(timeout=5)
        assert layout.database.read_bytes() == before
        assert not (layout.data_dir / "recovery.json").exists()
        assert not Path(str(layout.database) + "-journal").exists()
        assert not list(layout.data_dir.glob(".recovery-*"))


def test_rollback_retirement_failure_resolves_active_caller_and_seals_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    project = Project(uuid4(), tmp_path / "registered")
    original_unlink = os.unlink
    failures = 0

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal failures
        if path == "recovery.json":
            failures += 1
            raise OSError("SYNTHETIC_RETIREMENT_FAILURE")
        original_unlink(path, *args, **kwargs)

    with owner:
        owner.store.credential(owner)
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
            storage.index(project)
            with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as callers:
                patch.setattr(os, "unlink", unlink)
                # A real registry conflict rolls back a real, armed transaction.
                pending = callers.submit(
                    storage.index,
                    Project(uuid4(), project.root),
                    deadline=time.monotonic() + 6,
                )
                with pytest.raises(StorageFault) as caught:
                    pending.result(timeout=2)
                assert caught.value.code == "storage_outcome_unconfirmed"
            assert failures == 1
            assert storage.snapshot().state == "failed"
            assert storage.snapshot().error == "storage_outcome_unconfirmed"
            with pytest.raises(StorageFault) as closed:
                storage.lookup(project)
            assert closed.value.code == "storage_closing"
        finally:
            storage.close(timeout=5)

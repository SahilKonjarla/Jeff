"""Registry commit/reopen, immutable conflicts, real migration rollback and lost commit ACK."""

import os
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import StorageFault
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


def test_real_registry_commit_reopen_and_readonly_lookup(tmp_path: Path) -> None:
    layout = RuntimeLayout(
        data_dir=tmp_path / "d?mode=ro&cache=shared#", runtime_dir=tmp_path / "r", uid=os.geteuid()
    )
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    project = Project(uuid4(), tmp_path / "project ' literal")
    with owner:
        owner.store.credential(owner)
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            ready = storage.wait_ready(5)
            assert ready.state == "ready", ready
            assert ready.schema_version == 12 and ready.retention_days == 30
            assert storage.lookup(project) is None
            assert storage.index(project) == (project, True)
            assert storage.index(project) == (project, False)
            assert storage.lookup(project) == project
        finally:
            storage.close()
        again = SQLiteStorage(owner)
        again.start()
        try:
            assert again.wait_ready(5).state == "ready"
            assert again.lookup(project) == project
            before = layout.database.read_bytes()
            assert again.lookup(project) == project
            assert layout.database.read_bytes() == before
            assert layout.database.stat().st_mode & 0o7777 == 0o600
        finally:
            again.close()
    assert not any(layout.data_dir.glob("state.sqlite3-*"))


def test_conflicting_identity_never_rebinds_and_close_refuses_new_work(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    first = Project(uuid4(), tmp_path / "project")
    with owner:
        owner.store.credential(owner)
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
            assert storage.index(first) == (first, True)
            for conflict in (
                Project(first.project_id, tmp_path / "moved"),
                Project(uuid4(), first.root),
            ):
                with pytest.raises(StorageFault) as caught:
                    storage.index(conflict)
                assert caught.value.code == "registry_conflict"
                assert storage.lookup(first) == first
        finally:
            storage.close()
        with pytest.raises(StorageFault) as caught:
            storage.index(first)
        assert caught.value.code == "storage_closing"


def test_failed_real_migration_rolls_back_and_clean_schema_zero_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    connect = sqlite3.connect

    class Interrupted(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            if sql.startswith("CREATE TABLE projects"):
                # Real marker transaction already committed; real migration transaction
                # is active. Only the SQL driver boundary is fault-injected.
                raise sqlite3.OperationalError("SYNTHETIC_PRIVATE_MIGRATION")
            return super().execute(sql, parameters)

    def failing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return connect(*args, **kwargs, factory=Interrupted)

    with owner:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", failing_connect)
            owner.store.credential(owner)
            storage = SQLiteStorage(owner)
            storage.start()
            try:
                failed = storage.wait_ready(5)
                assert failed.state == "failed" and failed.error == "storage_failed"
                assert "SYNTHETIC_PRIVATE_MIGRATION" not in failed.model_dump_json()
            finally:
                storage.close()
        retry = SQLiteStorage(owner)
        retry.start()
        try:
            assert retry.wait_ready(5).state == "ready"
            project = Project(uuid4(), tmp_path / "new")
            assert retry.index(project) == (project, True)
        finally:
            retry.close()


def test_driver_error_after_actual_commit_is_uncertain_not_retryable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    project = Project(uuid4(), tmp_path / "committed")
    original = sqlite3.connect

    class LostAcknowledgement(sqlite3.Connection):
        wrote_project = False

        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            result = super().execute(sql, parameters)
            if sql.startswith("INSERT INTO projects"):
                self.wrote_project = True
            if sql == "COMMIT" and self.wrote_project:
                raise sqlite3.OperationalError("SYNTHETIC_PRIVATE_COMMIT")
            return result

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=LostAcknowledgement)

    with owner:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            owner.store.credential(owner)
            storage = SQLiteStorage(owner)
            storage.start()
            try:
                assert storage.wait_ready(5).state == "ready"
                with pytest.raises(StorageFault) as caught:
                    storage.index(project)
                assert caught.value.code == "storage_outcome_unconfirmed"
                assert "SYNTHETIC_PRIVATE_COMMIT" not in str(caught.value)
            finally:
                storage.close()
        reopened = SQLiteStorage(owner)
        reopened.start()
        try:
            assert reopened.wait_ready(5).state == "ready"
            assert reopened.lookup(project) == project
        finally:
            reopened.close()

"""Actual private SQLite transactions through the storage-owner interface."""

import os
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import DATABASE_APPLICATION_ID, StorageFault
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
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            ready = storage.wait_ready(5)
            assert ready.state == "ready", ready
            assert ready.schema_version == 2 and ready.retention_days == 30
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


@pytest.mark.parametrize(
    "kind,code",
    [
        ("empty", "storage_unrecognized"),
        ("corrupt", "storage_unrecognized"),
        ("foreign", "storage_unrecognized"),
        ("newer", "storage_schema_unsupported"),
        ("journal", "storage_recovery_required"),
        ("wal", "storage_recovery_required"),
        ("shm", "storage_recovery_required"),
    ],
)
def test_conservative_adoption_preserves_existing_database_and_sidecars(
    tmp_path: Path, kind: str, code: str
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        layout.data_dir.mkdir(mode=0o700)
        if kind == "empty":
            layout.database.touch(mode=0o600)
        elif kind == "corrupt":
            layout.database.write_bytes(b"SYNTHETIC_NOT_SQLITE")
            layout.database.chmod(0o600)
        else:
            with closing(sqlite3.connect(layout.database)) as fixture:
                fixture.execute("CREATE TABLE foreign_data(value TEXT)")
                if kind == "newer":
                    fixture.execute(f"PRAGMA application_id={DATABASE_APPLICATION_ID}")
                    fixture.execute("PRAGMA user_version=3")
            layout.database.chmod(0o600)
        if kind in ("journal", "wal", "shm"):
            sidecar = layout.data_dir / ("state.sqlite3-" + kind)
            sidecar.write_bytes(b"SYNTHETIC_RECOVERY")
            sidecar.chmod(0o600)
        before = {p.name: p.read_bytes() for p in layout.data_dir.iterdir()}
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            failed = storage.wait_ready(5)
            assert failed.state == "failed" and failed.error == code, failed
            for name, payload in before.items():
                assert (layout.data_dir / name).read_bytes() == payload
        finally:
            storage.close()


def test_conflicting_identity_never_rebinds_and_close_refuses_new_work(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    first = Project(uuid4(), tmp_path / "project")
    with owner:
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


def test_running_timeout_retains_slot_queue_is_bounded_and_close_waits_for_real_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    entered, release = threading.Event(), threading.Event()
    original = sqlite3.connect
    threads: set[int] = set()
    close_threads: set[int] = set()

    class Paused(sqlite3.Connection):
        paused = False

        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            threads.add(threading.get_ident())
            if sql.startswith("INSERT INTO projects") and not self.paused:
                self.paused = True
                assert super().execute("PRAGMA trusted_schema").fetchone() == (0,)
                assert super().execute("PRAGMA synchronous").fetchone() == (2,)
                assert super().execute("PRAGMA busy_timeout").fetchone() == (2000,)
                with pytest.raises(sqlite3.OperationalError, match="not authorized"):
                    super().execute("SELECT load_extension('SYNTHETIC_NOT_A_LIBRARY')")
                entered.set()
                if not release.wait(38):
                    raise sqlite3.OperationalError("Synthetic boundary wait expired")
            return super().execute(sql, parameters)

        def close(self) -> None:
            close_threads.add(threading.get_ident())
            super().close()

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=Paused)

    with owner, monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
            with ThreadPoolExecutor(max_workers=18) as workers:
                active = workers.submit(storage.index, Project(uuid4(), tmp_path / "active"))
                assert entered.wait(2)
                began = time.monotonic()
                try:
                    with pytest.raises(StorageFault) as uncertain:
                        active.result(timeout=32)
                    assert uncertain.value.code == "storage_outcome_unconfirmed"
                    assert time.monotonic() - began >= 29
                    pending = [
                        workers.submit(storage.index, Project(uuid4(), tmp_path / str(i)))
                        for i in range(17)
                    ]
                    done, _ = wait(pending, timeout=3, return_when=FIRST_COMPLETED)
                    assert len(done) == 1
                    overflow = done.pop()
                    with pytest.raises(StorageFault) as busy:
                        overflow.result()
                    assert busy.value.code == "storage_busy"
                    with pytest.raises(StorageFault) as closing:
                        storage.close(timeout=0)
                    assert (
                        closing.value.code == "storage_outcome_unconfirmed" and not storage.closed
                    )
                    with pytest.raises(StorageFault) as refused:
                        storage.lookup(Project(uuid4(), tmp_path / "refused"))
                    assert refused.value.code == "storage_closing"
                finally:
                    release.set()
                assert all(
                    future.result(timeout=5)[1] for future in pending if future is not overflow
                )
        finally:
            release.set()
            storage.close()
        assert threads == close_threads and len(threads) == 1
        assert threading.get_ident() not in threads


@pytest.mark.parametrize("alteration", ["unknown_object", "ledger_mismatch"])
def test_recognized_marker_does_not_bless_unknown_objects_or_mismatched_ledger(
    tmp_path: Path, alteration: str
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
        finally:
            storage.close()
        with closing(sqlite3.connect(layout.database, isolation_level=None)) as fixture:
            fixture.execute(
                "CREATE TABLE sqliteXforeign(value TEXT)"
                if alteration == "unknown_object"
                else "UPDATE schema_migrations SET name='unknown_migration' WHERE version=1"
            )
        before = layout.database.read_bytes()
        refused = SQLiteStorage(owner)
        refused.start()
        try:
            failed = refused.wait_ready(5)
            assert failed.state == "failed" and failed.error == "storage_schema_unsupported", failed
            assert layout.database.read_bytes() == before
        finally:
            refused.close()


def test_connection_close_error_is_uncertain_and_checked_descriptor_is_not_leaked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    original = sqlite3.connect

    class FailedClose(sqlite3.Connection):
        def close(self) -> None:
            super().close()
            raise sqlite3.OperationalError("SYNTHETIC_PRIVATE_CLOSE")

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=FailedClose)

    with owner, monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        storage = SQLiteStorage(owner)
        storage.start()
        assert storage.wait_ready(5).state == "ready"
        with pytest.raises(StorageFault) as caught:
            storage.close()
        assert caught.value.code == "storage_outcome_unconfirmed"
        assert storage.closed and storage.snapshot().state == "failed"
        expected = layout.database.stat()
        for name in os.listdir("/dev/fd"):
            try:
                descriptor = os.fstat(int(name))
            except OSError:
                continue
            assert (descriptor.st_dev, descriptor.st_ino) != (expected.st_dev, expected.st_ino)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "mode"])
def test_unsafe_database_never_reaches_the_sqlite_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        layout.data_dir.mkdir(mode=0o700)
        target = tmp_path / "synthetic"
        target.write_bytes(b"SYNTHETIC_PRIVATE")
        target.chmod(0o600)
        if kind == "symlink":
            layout.database.symlink_to(target)
        elif kind == "hardlink":
            os.link(target, layout.database)
        elif kind == "fifo":
            os.mkfifo(layout.database, mode=0o600)
        elif kind == "directory":
            layout.database.mkdir(mode=0o700)
        else:
            layout.database.write_bytes(b"SYNTHETIC_PRIVATE")
            layout.database.chmod(0o644)
        calls: list[bool] = []

        def forbidden(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            calls.append(True)
            raise AssertionError("Unsafe database reached SQL driver")

        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", forbidden)
            storage = SQLiteStorage(owner)
            storage.start()
            try:
                assert storage.wait_ready(5).state == "failed"
                assert not calls
                assert target.read_bytes() == b"SYNTHETIC_PRIVATE"
            finally:
                storage.close()


def test_database_replacement_at_engine_open_is_detected_without_mutating_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    foreign = tmp_path / "foreign.sqlite3"
    with closing(sqlite3.connect(foreign, isolation_level=None)) as fixture:
        fixture.execute("CREATE TABLE foreign_data(value TEXT)")
    payload = foreign.read_bytes()
    original = sqlite3.connect
    replaced = False
    backup = tmp_path / "owned-unmarked.sqlite3"

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        nonlocal replaced
        if not replaced:
            replaced = True
            layout.database.rename(backup)
            layout.database.write_bytes(payload)
            layout.database.chmod(0o600)
        result: sqlite3.Connection = original(*args, **kwargs)
        return result

    with owner, monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            failed = storage.wait_ready(5)
            assert failed.state == "failed" and failed.error == "storage_unsafe", failed
            assert layout.database.read_bytes() == payload
            assert backup.stat().st_size == 0
        finally:
            storage.close()


def test_clean_wal_header_is_refused_before_any_engine_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        initial = SQLiteStorage(owner)
        initial.start()
        try:
            assert initial.wait_ready(5).state == "ready"
        finally:
            initial.close()
        with closing(sqlite3.connect(layout.database, isolation_level=None)) as fixture:
            assert fixture.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        assert not list(layout.data_dir.glob("state.sqlite3-*"))
        before = layout.database.read_bytes()
        assert before[18:20] == b"\x02\x02"
        calls: list[bool] = []
        original = sqlite3.connect

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            calls.append(True)
            result: sqlite3.Connection = original(*args, **kwargs)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            refused = SQLiteStorage(owner)
            refused.start()
            try:
                failed = refused.wait_ready(5)
                assert failed.state == "failed" and failed.error == "storage_schema_unsupported", (
                    failed
                )
                assert not calls
                assert layout.database.read_bytes() == before
                assert not list(layout.data_dir.glob("state.sqlite3-*"))
            finally:
                refused.close()

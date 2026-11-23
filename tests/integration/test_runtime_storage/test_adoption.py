"""Conservative database/sidecar adoption, exact catalog/ledger recognition and WAL refusal."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import DATABASE_APPLICATION_ID, DATABASE_SCHEMA
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


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
                    fixture.execute(f"PRAGMA user_version={DATABASE_SCHEMA + 1}")
            layout.database.chmod(0o600)
        if kind in ("journal", "wal", "shm"):
            sidecar = layout.data_dir / ("state.sqlite3-" + kind)
            sidecar.write_bytes(b"SYNTHETIC_RECOVERY")
            sidecar.chmod(0o600)
        owner.store.credential(owner)
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


@pytest.mark.parametrize(
    "alteration",
    [
        "unknown_object",
        "ledger_mismatch",
        "extra_ledger",
        "floor_ddl",
    ],
)
def test_recognized_marker_does_not_bless_unknown_objects_or_mismatched_ledger(
    tmp_path: Path, alteration: str
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        owner.store.credential(owner)
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
        finally:
            storage.close()
        with closing(sqlite3.connect(layout.database, isolation_level=None)) as fixture:
            statements = {
                "unknown_object": "CREATE TABLE sqliteXforeign(value TEXT)",
                "ledger_mismatch": (
                    "UPDATE schema_migrations SET name='unknown_migration' WHERE version=1"
                ),
                "extra_ledger": (
                    f"INSERT INTO schema_migrations VALUES ({DATABASE_SCHEMA + 1},"
                    "'unknown_migration')"
                ),
                "floor_ddl": "ALTER TABLE activity_meta RENAME COLUMN replay_floor TO erased_floor",
            }
            fixture.execute(statements[alteration])
        before = layout.database.read_bytes()
        refused = SQLiteStorage(owner)
        refused.start()
        try:
            failed = refused.wait_ready(5)
            assert failed.state == "failed" and failed.error == "storage_schema_unsupported", failed
            assert layout.database.read_bytes() == before
        finally:
            refused.close()


def test_clean_wal_header_is_refused_before_any_engine_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        owner.store.credential(owner)
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

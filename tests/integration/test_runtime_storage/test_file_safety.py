"""Unsafe-file and replacement refusal plus checked-descriptor closure under driver error."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import StorageFault
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


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
        owner.store.credential(owner)
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
            owner.store.credential(owner)
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
        owner.store.credential(owner)
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            failed = storage.wait_ready(5)
            assert failed.state == "failed" and failed.error == "storage_unsafe", failed
            assert layout.database.read_bytes() == payload
            assert backup.stat().st_size == 0
        finally:
            storage.close()

"""Public startup defaults to PostgreSQL, without a SQLite or credential fallback."""

import os
from pathlib import Path

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import StorageFault
from jeff.bootstrap.runtime.postgres import selected_database
from jeff.infrastructure.runtime_state import RuntimeStateStore


def test_no_marker_defaults_to_postgresql(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEFF_TEST_SQLITE_FIXTURE", raising=False)
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        assert selected_database(owner) == "jeff"
        assert not layout.database.exists()


def test_default_refuses_old_sqlite_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEFF_TEST_SQLITE_FIXTURE", raising=False)
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    layout.data_dir.mkdir(mode=0o700)
    payload = b"synthetic legacy file"
    layout.database.write_bytes(payload)
    layout.database.chmod(0o600)
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner, pytest.raises(StorageFault) as error:
        selected_database(owner)
    assert error.value.code == "storage_unsafe"
    assert layout.database.read_bytes() == payload

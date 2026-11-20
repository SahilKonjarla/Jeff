"""Original function-scoped private stopped storage with synthetic fixed credential."""

import os
from pathlib import Path

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


@pytest.fixture
def stopped(tmp_path: Path) -> RuntimeLayout:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    layout.data_dir.mkdir(mode=0o700)
    layout.credential.write_bytes(b"\x00" * 32)
    layout.credential.chmod(0o600)
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
        finally:
            storage.close()
    return layout

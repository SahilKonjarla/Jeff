"""Real unshortened running-SQL timeout, bounded queue and owner-thread close lifetime."""

import os
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import StorageFault
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


def test_running_timeout_retains_slot_queue_is_bounded_and_close_waits_for_real_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the real timeout: caller uncertainty must not release running SQL capacity.

    Release the SQL gate before executor teardown so held workers can actually end.
    """
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
        owner.store.credential(owner)
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

"""Read-only residue, shared ownership/observer leases and boundary fact invalidation."""

import hashlib
import os
import socket
import sqlite3
import time
from contextlib import closing
from typing import Literal
from uuid import uuid4

import pytest

from jeff.application.diagnostics import DiagnosticDeadlineExceeded
from jeff.application.runtime import ControlFault, RuntimeLayout
from jeff.infrastructure.runtime_diagnostics import RuntimeDiagnosticsAdapter
from jeff.infrastructure.runtime_state import RuntimeStateStore

from .support_snapshot import snapshot


def test_receipt_only_residue_is_read_only_unavailable_not_clean_stopped(
    stopped: RuntimeLayout,
) -> None:
    store = RuntimeStateStore(stopped)
    owner = store.acquire(create=False)
    assert owner is not None
    with owner, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.bind(str(stopped.socket))
        stopped.socket.chmod(0o600)
        owner.publish_endpoint(uuid4(), store.read_credential())
        endpoint.close()
        stopped.socket.unlink()  # exact fixture entry: interrupted matching cleanup
    before = snapshot(stopped)
    report = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
    assert report.data.summary == "unavailable" and report.data.runtime is None
    assert report.data.checks[4].code == "runtime_endpoint_collision"
    assert snapshot(stopped) == before


def test_recovery_permission_residue_is_read_only_unavailable(
    stopped: RuntimeLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = RuntimeStateStore(stopped).acquire(create=False)
    assert owner is not None
    with owner, closing(sqlite3.connect(stopped.database, isolation_level=None)) as connection:
        size = connection.execute("PRAGMA page_size").fetchone()[0]
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("PRAGMA user_version=4")
        db = os.open(stopped.database, os.O_RDONLY | os.O_NOFOLLOW)
        journal = os.open(stopped.data_dir / "state.sqlite3-journal", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            owner.arm_storage_recovery(db, journal, page_size=size, original_pages=pages)
            connection.execute("COMMIT")  # Leave only a genuine own permit, not a fake journal.
        finally:
            os.close(journal)
            os.close(db)
    before = snapshot(stopped)
    original = os.open

    def denied(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if os.fsdecode(path) in ("ipc.key", "state.sqlite3", "recovery.json"):
            raise AssertionError("Doctor attempted a credential/database/recovery body open")
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", denied)
    report = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
    assert report.data.summary == "unavailable" and report.data.runtime is None
    assert snapshot(stopped) == before


def test_shared_diagnostic_lease_excludes_real_owner_and_releases(stopped: RuntimeLayout) -> None:
    store = RuntimeStateStore(stopped)
    before = snapshot(stopped)
    with store.diagnostic_lease(time.monotonic() + 5) as first:
        first.check()
        assert store.acquire(create=False) is None
        with store.diagnostic_lease(time.monotonic() + 5) as second:
            second.check()
            assert store.acquire(create=False) is None
    with pytest.raises(ControlFault) as closed:
        first.check()
    assert closed.value.code == "runtime_ownership_unconfirmed"
    owner = store.acquire(create=False)
    assert owner is not None
    with owner:
        with pytest.raises(ControlFault) as conflict:
            with store.diagnostic_lease(time.monotonic() + 5):
                pytest.fail("Diagnostic lease must conflict with a genuine exclusive owner")
        assert conflict.value.code == "runtime_ownership_unconfirmed"
    assert snapshot(stopped) == before


def test_held_owner_is_unavailable_without_sqlite_open(
    stopped: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("Held ownership must prevent any diagnostic SQLite open")

    owner = RuntimeStateStore(stopped).acquire(create=False)
    assert owner is not None
    before = snapshot(stopped)
    with owner, monkeypatch.context() as boundary:
        boundary.setattr(sqlite3, "connect", forbidden)
        result = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
        assert not result.ok and result.data.summary == "unavailable"
        assert result.data.checks[4].code == "runtime_ownership_unconfirmed"
        assert result.data.configured_retention_days is None
        assert result.data.database_schema_version is None
        owner.check()
    assert snapshot(stopped) == before


def test_leftover_endpoint_is_preserved_without_sqlite_open(
    stopped: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("Endpoint ambiguity must prevent diagnostic SQLite open")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.bind(str(stopped.socket))
        stopped.socket.chmod(0o600)
        before = snapshot(stopped)
        with monkeypatch.context() as boundary:
            boundary.setattr(sqlite3, "connect", forbidden)
            result = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
        assert not result.ok and result.data.summary == "unavailable"
        assert result.data.checks[4].code == "runtime_endpoint_collision"
        assert result.data.database_schema_version is None
        assert snapshot(stopped) == before
    # Native FD closed normally; filesystem endpoint intentionally retained, never deleted.
    assert stopped.socket.exists()


@pytest.mark.parametrize("change", ["settings", "namespace", "deadline"])
def test_boundary_race_or_deadline_discards_stopped_facts(
    stopped: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    # Faults at the SQLite-open/OS-clock boundary, not a mocked diagnostic result.
    original_connect = sqlite3.connect
    original_clock = time.monotonic
    database_before = hashlib.sha256(stopped.database.read_bytes()).hexdigest()
    with monkeypatch.context() as boundary:

        def connect(
            database: str,
            *,
            uri: bool,
            timeout: float,
            isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None,
        ) -> sqlite3.Connection:
            connection = original_connect(
                database, uri=uri, timeout=timeout, isolation_level=isolation_level
            )
            if change == "settings":
                stopped.settings.write_text("schema_version: 1\nhistory:\n  retention_days: 19\n")
            elif change == "namespace":
                # Owned fixture namespace replacement; no anchor deletion or guessed cleanup.
                stopped.runtime_dir.rename(stopped.runtime_dir.with_name("moved-runtime"))
                stopped.runtime_dir.mkdir(mode=0o700)
            else:
                boundary.setattr(time, "monotonic", lambda: original_clock() + 10)
            return connection

        boundary.setattr(sqlite3, "connect", connect)
        result = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
    assert not result.ok and result.data.summary == "unavailable"
    expected = (
        "diagnostic_deadline_exceeded" if change == "deadline" else "runtime_ownership_unconfirmed"
    )
    assert result.data.checks[4].code == expected
    assert result.data.configured_retention_days is None
    assert result.data.settings_schema_version is None
    assert result.data.database_schema_version is None
    assert result.data.runtime is None
    assert str(stopped.data_dir) not in result.model_dump_json()
    assert hashlib.sha256(stopped.database.read_bytes()).hexdigest() == database_before
    assert not list(stopped.data_dir.glob("state.sqlite3-*"))


def test_readonly_observer_does_not_confuse_owner_exit_with_a_held_lock(
    stopped: RuntimeLayout,
) -> None:
    store = RuntimeStateStore(stopped)
    owner = store.acquire(create=False)
    assert owner is not None
    try:
        with pytest.raises(ControlFault) as changed:
            with store.diagnostic_observation(time.monotonic() + 5) as observed:
                assert observed.held and not observed.has_endpoint
                owner.close()
                observed.check()
        assert changed.value.code == "runtime_ownership_unconfirmed"
    finally:
        owner.close()
    again = store.acquire(create=False)
    assert again is not None
    again.close()


def test_issued_stopped_lease_is_invalidated_when_observation_exits(stopped: RuntimeLayout) -> None:
    store = RuntimeStateStore(stopped)
    with store.diagnostic_observation(time.monotonic() + 5) as observation:
        assert not observation.held
        lease = observation.stopped_lease()
        lease.check()
    with pytest.raises(ControlFault) as closed:
        lease.check()
    assert closed.value.code == "runtime_ownership_unconfirmed"
    owner = store.acquire(create=False)
    assert owner is not None
    owner.close()


def test_observer_honors_expired_shared_deadline_without_creating(stopped: RuntimeLayout) -> None:
    before = snapshot(stopped)
    with pytest.raises(DiagnosticDeadlineExceeded):
        with RuntimeStateStore(stopped).diagnostic_observation(time.monotonic() - 1):
            pytest.fail("Expired operation must not obtain a lease")
    assert snapshot(stopped) == before

"""Actual stopped-state diagnostic/lease seams; synthetic private fixtures only."""

import hashlib
import os
import socket
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
from typing import Literal

import pytest

from jeff.application.runtime import ControlFault, RuntimeLayout
from jeff.infrastructure.runtime_diagnostics import RuntimeDiagnosticsAdapter
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


def snapshot(layout: RuntimeLayout) -> dict[str, tuple[int, int, int, int, str | None]]:
    return {
        str(path): (
            path.lstat().st_ino,
            stat.S_IMODE(path.lstat().st_mode),
            path.lstat().st_mtime_ns,
            path.lstat().st_nlink,
            os.readlink(path)
            if path.is_symlink()
            else hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() and path != layout.credential
            else None,
        )
        for root in (layout.data_dir, layout.runtime_dir)
        for path in (root, *root.rglob("*"))
    }


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


@pytest.mark.parametrize("role", ["lock", "credential", "settings", "database"])
def test_incomplete_installation_is_not_created_or_called_stopped(
    stopped: RuntimeLayout,
    role: str,
) -> None:
    getattr(stopped, role).unlink()
    before = snapshot(stopped)
    result = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
    assert not result.ok and result.data.summary == "unhealthy"
    assert result.data.checks[3].code == "installation_incomplete"
    assert result.data.runtime is None and result.data.database_schema_version is None
    assert snapshot(stopped) == before


@pytest.mark.parametrize("kind", ["mode", "symlink", "hardlink", "fifo", "ancestor"])
def test_unsafe_installation_is_rejected_before_content_reads(
    stopped: RuntimeLayout,
    kind: str,
) -> None:
    if kind == "mode":
        stopped.settings.chmod(0o644)
    elif kind == "symlink":
        stopped.settings.unlink()
        stopped.settings.symlink_to(stopped.data_dir.parent / "absent-target")
    elif kind == "hardlink":
        os.link(stopped.settings, stopped.data_dir / "extra-link")
    elif kind == "fifo":
        stopped.settings.unlink()
        os.mkfifo(stopped.settings, 0o600)
    else:
        stopped.data_dir.parent.chmod(0o777)
    try:
        before = snapshot(stopped)
        result = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
        assert not result.ok and result.data.summary == "unhealthy"
        assert result.data.checks[3].status == "unsafe"
        assert result.data.checks[3].code == "unsafe_runtime_path"
        assert result.data.configured_retention_days is None
        assert result.data.database_schema_version is None
        assert snapshot(stopped) == before
    finally:
        if kind == "ancestor":
            stopped.data_dir.parent.chmod(0o700)


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("settings", "settings_invalid"),
        ("credential_size", "runtime_credentials_invalid"),
        ("foreign", "storage_unrecognized"),
        ("header", "storage_unrecognized"),
        ("future", "storage_schema_unsupported"),
        ("catalog", "storage_schema_unsupported"),
        ("journal", "storage_recovery_required"),
        ("wal", "storage_recovery_required"),
        ("shm", "storage_recovery_required"),
    ],
)
def test_fixed_refusals_preserve_existing_bytes_and_sidecars(
    stopped: RuntimeLayout,
    kind: str,
    reason: str,
) -> None:
    if kind == "settings":
        stopped.settings.write_text("schema_version: 1\nschema_version: 1\n")
    elif kind == "credential_size":
        stopped.credential.write_bytes(b"")
    elif kind in ("foreign", "future", "catalog"):
        # Deliberate catalog/marker mutation for refusal setup, never the assertion seam.
        with closing(sqlite3.connect(stopped.database)) as connection, connection:
            connection.execute(
                {
                    "foreign": "PRAGMA application_id=0",
                    "future": "PRAGMA user_version=5",
                    "catalog": "CREATE TABLE foreign_table(value TEXT)",
                }[kind]
            )
    elif kind == "header":
        stopped.database.write_bytes(b"synthetic non-database fixture")
    else:
        sidecar = stopped.data_dir / ("state.sqlite3-" + kind)
        sidecar.write_bytes(b"synthetic sidecar: not a hot-journal recovery claim")
        sidecar.chmod(0o600)
    before = snapshot(stopped)
    result = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
    assert not result.ok and result.data.summary == "unhealthy"
    assert any(check.code == reason and check.required for check in result.data.checks)
    assert result.data.runtime is None
    assert snapshot(stopped) == before


def test_legacy_catalog_reports_upgrade_without_migrating(stopped: RuntimeLayout) -> None:
    # Synthetic schema3 catalog, NOT a retained-installed-version compatibility claim.
    with closing(sqlite3.connect(stopped.database)) as connection, connection:
        connection.executescript(
            "DROP INDEX receipts_expirable; DROP TABLE request_tombstones;"
            "DROP TABLE activity_meta;"
            "CREATE TABLE activity_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
            "epoch TEXT NOT NULL, "
            "coverage_started_at_ms INTEGER NOT NULL CHECK(coverage_started_at_ms>=0), "
            "high_water INTEGER NOT NULL CHECK(high_water>=0), "
            "generation INTEGER NOT NULL CHECK(generation>=0));"
            "DELETE FROM schema_migrations WHERE version=4; PRAGMA user_version=3;"
        )
    before = snapshot(stopped)
    result = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
    assert not result.ok and result.data.summary == "unsupported"
    assert result.data.database_schema_version == 3
    assert result.data.checks[6].code == "storage_upgrade_required"
    assert snapshot(stopped) == before


def test_large_valid_retention_has_bounded_inactive_summary(stopped: RuntimeLayout) -> None:
    stopped.settings.write_text(f"schema_version: 1\nhistory:\n  retention_days: {10**60}\n")
    before = snapshot(stopped)
    result = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
    assert result.ok and result.data.summary == "stopped"
    assert result.data.configured_retention_days == "above_report_limit"
    assert result.data.runtime is None
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

"""Storage-ready startup and safe startup diagnostics through real owned children."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from jeff.application.projects import Failure
from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage

pytestmark = pytest.mark.usefixtures("owned_runtime_launches")


def test_start_requires_storage_and_restart_reloads_private_settings(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    controller = RuntimeController(layout)
    try:
        stopped = controller.status()
        assert isinstance(stopped, ControlSuccess) and stopped.data.storage.state == "not_checked"
        assert not layout.data_dir.exists()
        first = controller.start()
        assert isinstance(first, ControlSuccess), first
        assert first.data.storage.state == "ready" and first.data.storage.retention_days == 30
        assert first.data.application_ready is True
        payload = b"# fixture only\nschema_version: 1\nhistory: {retention_days: 7}\n"
        layout.settings.write_bytes(payload)
        current = controller.status()
        assert isinstance(current, ControlSuccess) and current.data.storage.retention_days == 30
        assert isinstance(controller.stop(), ControlSuccess)
        again = controller.start()
        assert isinstance(again, ControlSuccess), again
        assert again.data.storage.retention_days == 7
        assert again.data.identity != first.data.identity
        assert layout.settings.read_bytes() == payload
    finally:
        assert isinstance(controller.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


def test_failed_settings_start_delivers_safe_reason_and_exits_without_rewrite(
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    layout.data_dir.mkdir(mode=0o700)
    payload = b"schema_version: 1\nhistory: {retention_days: 30}\nunknown: SYNTHETIC_PRIVATE\n"
    layout.settings.write_bytes(payload)
    layout.settings.chmod(0o600)
    controller = RuntimeController(layout)
    try:
        result = controller.start()
        assert isinstance(result, Failure) and result.error.code == "settings_invalid", result
        assert "SYNTHETIC_PRIVATE" not in result.model_dump_json()
        assert layout.settings.read_bytes() == payload
        stopped = controller.status()
        assert isinstance(stopped, ControlSuccess) and stopped.data.state.value == "stopped"
        assert not layout.database.exists() and not layout.socket.exists()
    finally:
        assert isinstance(controller.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "packet", ["ready_without_auth", "oversized", "unknown_error", "truncated"]
)
def test_startup_pipe_is_bounded_diagnostic_not_readiness_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, packet: str
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    original = subprocess.Popen

    def fake_child(command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        descriptor = command[command.index("--startup-fd") + 1]
        identity = command[command.index("--instance-id") + 1]
        script = """
import json,os,struct,sys
fd=int(sys.argv[1]);kind=sys.argv[3]
error='SYNTHETIC_PRIVATE' if kind=='unknown_error' else None
payload=json.dumps({'revision':3,'instance_id':sys.argv[2],'error':error}).encode()
frame=struct.pack('!I',len(payload))+payload
if kind=='oversized':frame=b'\\xff\\xff\\xff\\xff'
if kind=='truncated':frame=b'\\x00'
os.write(fd,frame)
"""
        return original([*command[:3], "-c", script, descriptor, identity, packet], **kwargs)  # type: ignore[call-overload,no-any-return]

    controller = RuntimeController(layout)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", fake_child)
            failed = controller.start()
        assert isinstance(failed, Failure)
        assert failed.error.code in ("runtime_start_failed", "runtime_protocol_error")
        assert "SYNTHETIC_PRIVATE" not in failed.model_dump_json()
        assert not layout.database.exists() and not layout.settings.exists()
    finally:
        assert isinstance(controller.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


def test_start_retries_when_a_real_short_lived_reader_lease_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    busy = threading.Event()
    original = fcntl.flock

    def lock(fd: int, flags: int) -> None:
        try:
            original(fd, flags)
        except BlockingIOError:
            busy.set()
            raise

    controller = RuntimeController(layout)
    try:
        with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as caller:
            patch.setattr(fcntl, "flock", lock)
            starting = caller.submit(controller.start)
            assert busy.wait(2)
            owner.close()
            result = starting.result(timeout=5)
            assert isinstance(result, ControlSuccess) and result.data.storage.state == "ready", (
                result
            )
    finally:
        owner.close()
        assert isinstance(controller.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


@pytest.mark.parametrize("stop_early", [False, True])
def test_real_sqlite_busy_initialization_keeps_control_responsive(
    tmp_path: Path, stop_early: bool
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
    controller = RuntimeController(layout)
    try:
        # Controlled SQLite lock boundary only; no uncommitted changes/sidecars.
        with closing(sqlite3.connect(layout.database, isolation_level=None)) as lock:
            lock.execute("BEGIN EXCLUSIVE")
            with ThreadPoolExecutor(max_workers=1) as caller:
                started = caller.submit(controller.start)
                deadline = time.monotonic() + 3
                while True:
                    current = controller.status()
                    if isinstance(current, ControlSuccess) and current.data.control_ready:
                        break
                    assert time.monotonic() < deadline and not started.done(), current
                    time.sleep(0.01)
                assert current.data.storage.state == "initializing"
                before = time.monotonic()
                current = controller.status()
                assert (
                    isinstance(current, ControlSuccess)
                    and current.data.storage.state == "initializing"
                )
                assert time.monotonic() - before < 1
                if stop_early:
                    stopped = controller.stop()
                    assert isinstance(stopped, ControlSuccess), stopped
                failed = started.result(timeout=5)
                assert isinstance(failed, Failure), failed
                if not stop_early:
                    assert failed.error.code == "storage_busy", failed
            lock.execute("ROLLBACK")
    finally:
        assert isinstance(controller.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)

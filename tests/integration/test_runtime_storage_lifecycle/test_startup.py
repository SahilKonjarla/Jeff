"""Actual storage-ready startup/settings/lease checks and diagnostic-only startup pipes."""

from __future__ import annotations

import fcntl
import json
import os
import struct
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.projects import Failure
from jeff.application.runtime import ControlFault, ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_process import RuntimeController, _read_startup_result
from jeff.infrastructure.runtime_state import RuntimeStateStore

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
payload=json.dumps({'revision':6,'instance_id':sys.argv[2],'error':error}).encode()
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


@pytest.mark.parametrize(
    "packet", ["absent", "wrong_instance", "oversized", "partial", "ready_only"]
)
def test_startup_diagnostic_drain_is_bounded_and_instance_scoped(packet: str) -> None:
    expected = uuid4()
    identity = uuid4() if packet == "wrong_instance" else expected
    payload = json.dumps({"revision": 6, "instance_id": str(identity), "error": None}).encode()
    frame = struct.pack("!I", len(payload)) + payload
    if packet == "oversized":
        frame = b"\xff\xff\xff\xff"
    elif packet == "partial":
        frame = frame[:3]
    read, write = os.pipe()
    try:
        os.set_blocking(read, False)
        if packet != "absent":
            os.write(write, frame)
        if packet in ("wrong_instance", "oversized"):
            code = (
                "runtime_ownership_unconfirmed"
                if packet == "wrong_instance"
                else "runtime_protocol_error"
            )
            with pytest.raises(ControlFault) as failed:
                _read_startup_result(read, bytearray(), expected)
            assert failed.value.code == code
        else:
            result = _read_startup_result(read, bytearray(), expected)
            if packet == "ready_only":
                assert result is not None and result.error is None  # not a readiness proof
            else:
                assert result is None
    finally:
        os.close(read)
        os.close(write)

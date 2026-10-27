"""Storage-ready startup and safe startup diagnostics through real owned children."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sqlite3
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.projects import Failure
from jeff.application.runtime import (
    ControlFault,
    ControlObservation,
    ControlOperation,
    ControlSuccess,
    RuntimeLayout,
)
from jeff.infrastructure import runtime_ipc
from jeff.infrastructure.runtime_process import RuntimeController, _read_startup_result
from jeff.infrastructure.runtime_state import Ownership, RuntimeStateStore
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


def test_busy_start_preserves_child_diagnostic_when_owner_exits_after_hello(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Actual SQL-busy child exits between authenticated hello and the flock probe."""
    original_ready = RuntimeController._ready
    original_acquire = RuntimeStateStore.acquire
    active = threading.local()
    gated = False

    def ready(
        self: RuntimeController,
        store: RuntimeStateStore,
        deadline: float,
        operation: ControlOperation = "hello",
    ) -> ControlObservation:
        active.controller = self if operation == "hello" else None
        active.deadline = deadline
        try:
            return original_ready(self, store, deadline, operation)
        finally:
            active.controller = None

    def acquire(self: RuntimeStateStore, *, create: bool) -> Ownership | None:
        nonlocal gated
        controller = getattr(active, "controller", None)
        if not create and not gated and controller is not None:
            child = controller._child
            assert child is not None  # genuine handle captured by the launching controller
            gated = True
            child.wait(timeout=max(0, active.deadline - time.monotonic()))
        return original_acquire(self, create=create)

    with monkeypatch.context() as patch:
        patch.setattr(RuntimeController, "_ready", ready)
        patch.setattr(RuntimeStateStore, "acquire", acquire)
        test_real_sqlite_busy_initialization_keeps_control_responsive(tmp_path, False)
    assert gated, "The real child-exit/flock boundary must actually be exercised"


@pytest.mark.parametrize(
    "mode",
    [
        "disconnected",
        "unclassified",
        "wrong_instance",
        "ready_only",
        "held_anchor",
        "unsafe_namespace",
    ],
)
def test_busy_start_preserves_diagnostic_when_native_peer_disconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """A real closed Unix peer, not a forged UID/PID or bypassed verifier."""
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
    launch = subprocess.Popen
    peer = runtime_ipc.peer_identity
    original_ready = RuntimeController._ready
    children: list[subprocess.Popen[bytes]] = []
    deadline = 0.0
    disconnected = False
    held: Ownership | None = None
    script = (
        "from jeff.infrastructure.runtime_ipc import ControlServer\n"
        "def closed_accept(self,reader,writer):writer.close()\n"
        "ControlServer._accept=closed_accept\n"
        "import jeff.bootstrap.runtime as runtime\n"
        f"mode={mode!r}\n"
        "original=runtime.encode_message\n"
        "def diagnostic(data,maximum):\n"
        " if mode=='wrong_instance':\n"
        "  from uuid import uuid4\n"
        "  data=dict(data,instance_id=str(uuid4()))\n"
        " if mode=='ready_only':data=dict(data,error=None)\n"
        " return original(data,maximum)\n"
        "runtime.encode_message=diagnostic\n"
        "raise SystemExit(runtime.main())\n"
    )

    def child(command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        assert command[3:5] == ["-m", "jeff.bootstrap.runtime"]
        captured: subprocess.Popen[bytes] = launch(  # type: ignore[call-overload]
            [*command[:3], "-c", script, *command[5:]], **kwargs
        )
        children.append(captured)
        return captured

    def ready(
        self: RuntimeController,
        store: RuntimeStateStore,
        end: float,
        operation: ControlOperation = "hello",
    ) -> ControlObservation:
        nonlocal deadline
        deadline = end
        return original_ready(self, store, end, operation)

    def exited_peer(connection: socket.socket) -> tuple[int, int]:
        nonlocal disconnected, held
        assert len(children) == 1
        children[0].wait(timeout=max(0, deadline - time.monotonic()))
        if mode == "held_anchor":
            held = RuntimeStateStore(layout).acquire(create=False)
            assert held is not None
        elif mode == "unsafe_namespace":
            layout.runtime_dir.chmod(0o755)
        try:
            return peer(connection)
        except ControlFault as error:
            assert error.code == "runtime_peer_unverified"
            disconnected = True
            if mode == "unclassified":
                raise ControlFault("runtime_peer_unverified") from None
            raise

    controller = RuntimeController(layout)
    try:
        with closing(sqlite3.connect(layout.database, isolation_level=None)) as lock:
            lock.execute("BEGIN EXCLUSIVE")
            with monkeypatch.context() as patch:
                patch.setattr(subprocess, "Popen", child)
                patch.setattr(RuntimeController, "_ready", ready)
                patch.setattr(runtime_ipc, "peer_identity", exited_peer)
                result = controller.start()
            assert disconnected, "Actual native verification must see the disconnected peer"
            expected = "storage_busy" if mode == "disconnected" else "runtime_peer_unverified"
            assert isinstance(result, Failure) and result.error.code == expected, result
            assert children[0].poll() is not None
            lock.execute("ROLLBACK")
    finally:
        if held is not None:
            held.close()
        if mode == "unsafe_namespace":
            layout.runtime_dir.chmod(0o700)
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

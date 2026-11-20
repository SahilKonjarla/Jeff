"""Real busy-SQL startup, responsive control and authenticated child-exit/peer races."""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

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
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import Ownership, RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage

pytestmark = pytest.mark.usefixtures("owned_runtime_launches")


@pytest.mark.parametrize("stop_early", [False, True])
def test_real_sqlite_busy_initialization_keeps_control_responsive(
    tmp_path: Path, stop_early: bool
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


def test_busy_start_preserves_failure_when_authenticated_reply_is_stopping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    marker = tmp_path / "actual-stopping"
    original_launch = subprocess.Popen
    original_request = runtime_ipc.ControlConnection.request
    gated = False
    script = (
        "import os\nfrom pathlib import Path\n"
        "from jeff.infrastructure.runtime_ipc import ControlServer\n"
        "original=ControlServer.request_stop\n"
        "def stop(self):\n"
        " original(self)\n"
        " if self.storage_snapshot().state=='failed':\n"
        "  Path(os.environ['JEFF_TEST_STOPPING_MARKER']).touch()\n"
        "ControlServer.request_stop=stop\n"
        "from jeff.bootstrap.runtime import main\nraise SystemExit(main())\n"
    )

    def launch(command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        assert command[3:5] == ["-m", "jeff.bootstrap.runtime"]
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        kwargs["env"] = {**environment, "JEFF_TEST_STOPPING_MARKER": str(marker)}
        captured: subprocess.Popen[bytes] = original_launch(  # type: ignore[call-overload]
            [*command[:3], "-c", script, *command[5:]], **kwargs
        )
        return captured

    def request(
        self: runtime_ipc.ControlConnection,
        operation: ControlOperation,
        expected_instance_id: object = None,
    ) -> ControlObservation:
        nonlocal gated
        if operation == "hello" and not gated:
            while not marker.exists():
                assert time.monotonic() < self.deadline
                time.sleep(0.01)
            gated = True
        return original_request(self, operation, expected_instance_id)  # type: ignore[arg-type]

    controller = RuntimeController(layout)
    try:
        with closing(sqlite3.connect(layout.database, isolation_level=None)) as lock:
            lock.execute("BEGIN EXCLUSIVE")
            with monkeypatch.context() as patch:
                patch.setattr(subprocess, "Popen", launch)
                patch.setattr(runtime_ipc.ControlConnection, "request", request)
                result = controller.start()
            assert gated
            assert isinstance(result, Failure) and result.error.code == "storage_busy", result
            lock.execute("ROLLBACK")
    finally:
        assert isinstance(controller.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


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
        owner.store.credential(owner)
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

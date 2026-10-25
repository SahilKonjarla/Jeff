"""Native control sockets and actual owned child processes, never user installations."""

import hashlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID

import pytest

from jeff.application.projects import Failure
from jeff.application.runtime import (
    ControlFault,
    ControlSuccess,
    RuntimeIdentityData,
    RuntimeLayout,
)
from jeff.infrastructure.runtime_ipc import ControlClient, encode_message, peer_identity
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore


@pytest.fixture
def owned_launches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    original = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def tracked(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original(command, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", tracked)
    yield
    forced = False
    for child in children:
        if child.poll() is None:
            forced = True
            child.terminate()  # actual captured Popen ownership, never a stored/guessed PID
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
    assert not forced, "A fixture required forced owned-child cleanup"


@pytest.fixture
def installation(
    tmp_path_factory: pytest.TempPathFactory, owned_launches: None
) -> Iterator[RuntimeLayout]:
    root = tmp_path_factory.mktemp("inst").resolve()
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    yield layout
    result = RuntimeController(layout).stop()
    # Unsafe/collision fixtures may intentionally refuse stop; captured handles still reap.
    if isinstance(result, Failure):
        assert result.error.code in (
            "unsafe_runtime_path",
            "runtime_endpoint_collision",
            "runtime_credentials_invalid",
        )
    layout.credential.unlink(missing_ok=True)
    layout.socket.unlink(missing_ok=True)


def test_controller_start_status_stop_and_repeat_start_use_one_real_owner(
    tmp_path_factory: pytest.TempPathFactory,
    owned_launches: None,
) -> None:
    root = tmp_path_factory.mktemp("life").resolve()
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    controller = RuntimeController(layout)
    try:
        before = controller.status()
        assert isinstance(before, ControlSuccess) and before.data.state.value == "stopped"
        assert not root.joinpath("r").exists()
        first = controller.start()
        assert isinstance(first, ControlSuccess), first
        assert first.data.control_ready and first.data.application_ready
        again = RuntimeController(layout).start()
        assert isinstance(again, ControlSuccess), again
        assert again.data.identity == first.data.identity
        assert controller.status().data == first.data
        stopped = RuntimeController(layout).stop()
        assert isinstance(stopped, ControlSuccess), stopped
        assert stopped.data.state.value == "stopped"
        assert layout.lock.exists() and layout.credential.exists()
        assert not layout.socket.exists()
        repeat = controller.stop()
        assert isinstance(repeat, ControlSuccess)
        assert layout.database.exists() and layout.settings.exists()
        assert stopped.data.storage.state == "not_checked"
    finally:
        result = controller.stop()
        if isinstance(result, Failure):
            pytest.fail("Owned lifecycle fixture cleanup was unconfirmed")
        layout.credential.unlink(missing_ok=True)


def test_concurrent_starts_converge_and_restart_preserves_anchor_and_credential(
    installation: RuntimeLayout,
) -> None:
    controllers = [RuntimeController(installation) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda controller: controller.start(), controllers))
    assert all(isinstance(result, ControlSuccess) for result in results), results
    identities = {result.data.identity for result in results if isinstance(result, ControlSuccess)}
    assert len(identities) == 1
    original_digest = hashlib.sha256(installation.credential.read_bytes()).digest()
    anchor = installation.lock.stat().st_ino
    assert isinstance(controllers[0].stop(), ControlSuccess)
    restarted = controllers[0].start()
    assert isinstance(restarted, ControlSuccess), restarted
    assert restarted.data.identity not in identities
    preserved = hashlib.sha256(installation.credential.read_bytes()).digest() == original_digest
    assert preserved
    assert installation.lock.stat().st_ino == anchor


def test_start_refuses_an_unowned_endpoint_without_minting_credentials(
    installation: RuntimeLayout,
) -> None:
    installation.runtime_dir.mkdir(mode=0o700)
    installation.socket.write_bytes(b"synthetic-collision")
    installation.socket.chmod(0o600)
    result = RuntimeController(installation).start()
    assert isinstance(result, Failure)
    assert result.error.code in ("unsafe_runtime_path", "runtime_endpoint_collision")
    assert not installation.credential.exists()
    assert installation.socket.read_bytes() == b"synthetic-collision"


def test_slow_unauthenticated_sender_does_not_block_control_and_expires_in_five_seconds(
    installation: RuntimeLayout,
) -> None:
    controller = RuntimeController(installation)
    assert isinstance(controller.start(), ControlSuccess)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as slow:
        slow.settimeout(7)
        slow.connect(str(installation.socket))
        slow.sendall(b"\x00")
        began = time.monotonic()
        status = controller.status()
        assert isinstance(status, ControlSuccess), status
        assert time.monotonic() - began < 2
        assert slow.recv(1) == b""
        elapsed = time.monotonic() - began
        assert 4.5 <= elapsed <= 6.5


def test_oversized_and_unauthenticated_shutdown_frames_never_dispatch(
    installation: RuntimeLayout,
) -> None:
    controller = RuntimeController(installation)
    assert isinstance(controller.start(), ControlSuccess)
    frames = [
        b"\xff\xff\xff\xff",
        encode_message(
            {
                "revision": 6,
                "request_id": "00000000-0000-4000-8000-000000000001",
                "operation": "stop",
            },
            4096,
        ),
    ]
    for frame in frames:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as caller:
            caller.settimeout(2)
            caller.connect(str(installation.socket))
            caller.sendall(frame)
            try:
                assert caller.recv(1) == b""
            except ConnectionResetError:
                pass
        status = controller.status()
        assert isinstance(status, ControlSuccess) and status.data.state.value == "running"


def test_capacity_is_bounded_and_excess_connections_are_closed(installation: RuntimeLayout) -> None:
    assert isinstance(RuntimeController(installation).start(), ControlSuccess)
    callers: list[socket.socket] = []
    try:
        for _ in range(16):
            caller = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            callers.append(caller)
            caller.settimeout(2)
            caller.connect(str(installation.socket))
            caller.sendall(b"\x00")
        time.sleep(0.05)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as excess:
            excess.settimeout(2)
            excess.connect(str(installation.socket))
            assert excess.recv(1) == b""
    finally:
        for caller in callers:
            caller.close()


def test_busy_unknown_owner_times_out_without_creating_replacement_authority(
    installation: RuntimeLayout,
) -> None:
    store = RuntimeStateStore(installation)
    owner = store.acquire(create=True)
    assert owner is not None
    try:
        began = time.monotonic()
        result = RuntimeController(installation).start()
        elapsed = time.monotonic() - began
        assert isinstance(result, Failure) and result.error.code == "runtime_start_timeout"
        assert 8.5 <= elapsed < 10.5
        assert not installation.credential.exists() and not installation.socket.exists()
    finally:
        owner.close()


def test_bound_but_not_listening_endpoint_is_unreachable_not_an_inspection_failure(
    installation: RuntimeLayout,
) -> None:
    installation.runtime_dir.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unpublished:
        unpublished.bind(str(installation.socket))
        installation.socket.chmod(0o600)
        with pytest.raises(ControlFault) as caught:
            ControlClient(installation, bytes(32)).request("hello", deadline=time.monotonic() + 1)
    assert caught.value.code == "runtime_unreachable"


def test_stale_socket_is_preserved_and_never_adopted_or_rekeyed(
    installation: RuntimeLayout,
) -> None:
    installation.runtime_dir.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(installation.socket))
        installation.socket.chmod(0o600)
    before = installation.socket.stat().st_ino
    result = RuntimeController(installation).start()
    assert isinstance(result, Failure) and result.error.code == "runtime_endpoint_collision"
    assert installation.socket.stat().st_ino == before
    assert not installation.credential.exists()


def test_spawn_failure_is_safe_and_does_not_replace_existing_private_state(
    installation: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise OSError("SYNTHETIC_PRIVATE_SPAWN_DETAIL")

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", denied)
        result = RuntimeController(installation).start()
    assert isinstance(result, Failure) and result.error.code == "runtime_start_failed"
    assert "SYNTHETIC_PRIVATE_SPAWN_DETAIL" not in result.model_dump_json()
    assert not installation.socket.exists()
    assert isinstance(RuntimeController(installation).status(), ControlSuccess)


def test_acknowledged_stop_without_process_exit_is_unconfirmed_at_real_deadline(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("hold").resolve()
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    store = RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    store.credential(owner)
    code = """
import asyncio,os,sys,time
from pathlib import Path
from uuid import uuid4
from jeff.application.runtime import RuntimeLayout,RuntimeIdentityData
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_ipc import ControlServer
os.umask(0o177)
store=RuntimeStateStore(RuntimeLayout(data_dir=Path(sys.argv[1]),runtime_dir=Path(sys.argv[2]),uid=os.geteuid()))
owner=store.adopt(int(sys.argv[3]))
asyncio.run(ControlServer(owner,store.read_credential(),RuntimeIdentityData(instance_id=uuid4(),pid=os.getpid())).serve())
time.sleep(40)  # socket gone, lifetime lock still held by the actual process
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            code,
            str(layout.data_dir),
            str(layout.runtime_dir),
            str(owner.fd),
        ],
        pass_fds=(owner.fd,),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    owner.close()
    try:
        deadline = time.monotonic() + 5
        controller = RuntimeController(layout)
        while not isinstance(controller.status(), ControlSuccess):
            assert child.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        began = time.monotonic()
        result = controller.stop()
        elapsed = time.monotonic() - began
        assert isinstance(result, Failure) and result.error.code == "runtime_stop_unconfirmed"
        assert 34.5 <= elapsed <= 38
        assert child.poll() is None
        assert not layout.socket.exists()
        assert store.acquire(create=False) is None
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
        layout.credential.unlink(missing_ok=True)
        layout.socket.unlink(missing_ok=True)


def test_native_peer_identity_is_verified_on_real_mac_unix_sockets(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("peer").resolve()
    path = root / "s"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            path.chmod(0o600)
            listener.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(path))
                accepted, _ = listener.accept()
                with accepted:
                    assert peer_identity(client) == (os.geteuid(), os.getpid())
                    assert peer_identity(accepted) == (os.geteuid(), os.getpid())
    finally:
        path.unlink(missing_ok=True)


def test_real_server_authenticates_and_stops_only_the_target_instance(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("ctrl").resolve()
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    store = RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    token = store.credential(owner)
    instance = UUID("00000000-0000-4000-8000-000000000002")
    code = """
import asyncio,os,sys
from pathlib import Path
from uuid import UUID
from jeff.application.runtime import RuntimeIdentityData,RuntimeLayout
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_ipc import ControlServer
os.umask(0o177)
store=RuntimeStateStore(RuntimeLayout(data_dir=Path(sys.argv[1]),runtime_dir=Path(sys.argv[2]),uid=os.geteuid()))
owner=store.adopt(int(sys.argv[3]))
asyncio.run(ControlServer(owner,store.read_credential(),RuntimeIdentityData(instance_id=UUID(sys.argv[4]),pid=os.getpid())).serve())
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            code,
            str(layout.data_dir),
            str(layout.runtime_dir),
            str(owner.fd),
            str(instance),
        ],
        pass_fds=(owner.fd,),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    owner.close()
    client = ControlClient(layout, token)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                observation = client.request("hello", deadline=deadline)
                break
            except ControlFault:
                assert child.poll() is None, "Owned control child exited before readiness"
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        assert observation.identity == RuntimeIdentityData(instance_id=instance, pid=child.pid)
        assert observation.control_ready and not observation.application_ready
        with pytest.raises(ControlFault) as denied:
            ControlClient(layout, bytes(32)).request(
                "stop", expected_instance_id=instance, deadline=time.monotonic() + 2
            )
        assert denied.value.code == "runtime_authentication_failed"
        assert child.poll() is None
        with pytest.raises(ControlFault):
            client.request("stop", expected_instance_id=UUID(int=3), deadline=time.monotonic() + 2)
        assert child.poll() is None
        stopping = client.request(
            "stop", expected_instance_id=instance, deadline=time.monotonic() + 5
        )
        assert stopping.state.value == "stopping"
        assert child.wait(timeout=10) == 0
        assert not layout.socket.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        layout.credential.unlink(missing_ok=True)
        layout.socket.unlink(missing_ok=True)

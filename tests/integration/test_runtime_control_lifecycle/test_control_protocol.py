"""Actual native peer identity, bounded admission and authenticated target-instance control."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from uuid import UUID

import pytest

from jeff.application.runtime import (
    ControlFault,
    ControlSuccess,
    RuntimeIdentityData,
    RuntimeLayout,
)
from jeff.infrastructure.runtime_ipc import ControlClient, encode_message, peer_identity
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore


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
                "revision": 14,
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

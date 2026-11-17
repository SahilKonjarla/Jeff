"""Original credential, peer/protocol/capability and bounded admission/response refusals."""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore

from .support_readonly import readonly_cli, snapshot


@pytest.mark.parametrize("fault", ["wrong", "missing", "unsafe"])
def test_live_doctor_credential_fault_never_rekeys_or_claims_health(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
    fault: str,
) -> None:
    layout, env, cwd = doctor_installation
    # Only synthetic fixture authority; restore it before authenticated normal teardown.
    original = layout.credential.read_bytes()
    saved = layout.credential.with_name("fixture-saved-key")
    if fault == "wrong":
        layout.credential.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    elif fault == "missing":
        layout.credential.rename(saved)
    else:
        layout.credential.chmod(0o640)
    try:
        prefix = readonly_cli(layout, env, cwd)
        before = snapshot(layout)
        result = subprocess.run(
            [*prefix, "-m", "jeff", "doctor", "--json"],
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=10,
        )
        payload = json.loads(result.stdout)
        assert result.returncode == 1 and payload["data"]["summary"] == "unhealthy"
        assert payload["data"]["runtime"] is None
        assert payload["data"]["configured_retention_days"] is None
        assert snapshot(layout) == before
        assert original.hex() not in result.stdout.decode() + result.stderr.decode()
        if fault == "wrong":
            assert any(
                c["code"] == "runtime_authentication_failed" for c in payload["data"]["checks"]
            )
        elif fault == "missing":
            assert not layout.credential.exists()
        else:
            assert any(c["status"] == "unsafe" for c in payload["data"]["checks"])
    finally:
        if fault == "missing":
            saved.rename(layout.credential)
        else:
            layout.credential.write_bytes(original)
            layout.credential.chmod(0o600)


@pytest.mark.parametrize(
    "fault,code,summary",
    [
        ("revision", "runtime_protocol_incompatible", "unsupported"),
        ("peer", "runtime_peer_unverified", "unhealthy"),
        ("capability", "runtime_capability_unavailable", "unsupported"),
    ],
)
def test_live_doctor_rejects_injected_protocol_peer_and_capability_faults(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
    fault: str,
    code: str,
    summary: str,
) -> None:
    layout, env, cwd = doctor_installation
    if fault == "revision":
        hook = """original=ipc.encode_message
def encode(value,maximum):
 if isinstance(value,dict) and 'token' in value:value={**value,'revision':5}
 return original(value,maximum)
ipc.encode_message=encode
"""
    elif fault == "peer":
        hook = """original=ipc.peer_identity
def peer(connection):
 uid,pid=original(connection)
 return uid,pid+1
ipc.peer_identity=peer
"""
    else:
        hook = """import json
original=ipc.decode_message
def decode(payload,model):
 if model is ipc.ControlResponse:
  value=json.loads(payload)
  if value.get('observation'):
   value['observation']['available_operations'].remove('activity.subscribe')
   payload=json.dumps(value).encode()
 return original(payload,model)
ipc.decode_message=decode
"""
    # Explicit auth-frame/native-peer/codec faults, not mocked DoctorResult or success.
    script = (
        "from jeff.infrastructure import runtime_ipc as ipc\n"
        + hook
        + (
            "from jeff.bootstrap.main import main\nimport sys\n"
            "sys.argv=['jeff','doctor','--json']\nmain()"
        )
    )
    prefix = readonly_cli(layout, env, cwd)
    before = snapshot(layout)
    result = subprocess.run(
        [*prefix, "-c", script], cwd=cwd, env=env, capture_output=True, timeout=10
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 1 and payload["data"]["summary"] == summary
    assert payload["data"]["runtime"] is None
    assert any(c["code"] == code for c in payload["data"]["checks"])
    assert snapshot(layout) == before


@pytest.mark.parametrize("fault", ["silent", "partial"])
def test_owned_unresponsive_peer_uses_shared_deadline_not_false_health(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
    fault: str,
) -> None:
    layout, env, cwd = doctor_installation
    assert isinstance(RuntimeController(layout).stop(), ControlSuccess)
    owner = RuntimeStateStore(layout).acquire(create=False)
    assert owner is not None
    stop = threading.Event()
    errors: list[BaseException] = []
    with owner, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(layout.socket))
        layout.socket.chmod(0o600)
        endpoint = layout.socket.lstat()
        listener.listen(4)
        listener.settimeout(0.1)

        def serve() -> None:
            try:
                while not stop.is_set():
                    try:
                        connection, _ = listener.accept()
                    except TimeoutError:
                        continue
                    with connection:
                        connection.settimeout(1)
                        if not connection.recv(4096):
                            continue  # selected-Unix-socket allow control closes without auth
                        if fault == "partial":
                            connection.sendall(b"\x00\x00\x00\x20{")
                        stop.wait(8)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            prefix = readonly_cli(layout, env, cwd)
            before = snapshot(layout)
            began = time.monotonic()
            result = subprocess.run(
                [*prefix, "-m", "jeff", "doctor", "--json"],
                cwd=cwd,
                env=env,
                capture_output=True,
                timeout=10,
            )
            elapsed = time.monotonic() - began
            payload = json.loads(result.stdout)
            assert result.returncode == 1 and payload["data"]["summary"] == "unavailable"
            assert payload["data"]["runtime"] is None
            assert any(
                c["code"] == "diagnostic_deadline_exceeded" for c in payload["data"]["checks"]
            )
            assert 4 <= elapsed < 7
            assert snapshot(layout) == before
        finally:
            stop.set()
            thread.join(timeout=2)
            assert not thread.is_alive() and not errors
            current = layout.socket.lstat()
            assert (current.st_dev, current.st_ino) == (endpoint.st_dev, endpoint.st_ino)
            layout.socket.unlink()  # fixture removes ONLY the socket it just bound/captured


def test_doctor_admission_exhaustion_is_unavailable_and_slots_recover(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
) -> None:
    layout, env, cwd = doctor_installation
    peers: list[socket.socket] = []
    try:
        for _ in range(16):
            peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            peers.append(peer)
            peer.settimeout(2)
            peer.connect(str(layout.socket))
            peer.sendall(b"\x00")  # real incomplete auth frames retain the bounded slots
        prefix = readonly_cli(layout, env, cwd)
        result = subprocess.run(
            [*prefix, "-m", "jeff", "doctor", "--json"],
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=10,
        )
        payload = json.loads(result.stdout)
        assert result.returncode == 1 and payload["data"]["summary"] == "unavailable"
        assert payload["data"]["runtime"] is None
    finally:
        for peer in peers:
            peer.close()
    again = subprocess.run(
        [*prefix, "-m", "jeff", "doctor", "--json"],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert again.returncode == 0 and json.loads(again.stdout)["data"]["summary"] == "healthy"

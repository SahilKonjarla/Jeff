"""Strict bounded control messages through the codec, no live credential fixtures."""

import select
import socket
import struct
import time
from pathlib import Path
from uuid import UUID

import pytest

from jeff.application.runtime import AuthAccepted, ControlFault, ControlRequest
from jeff.infrastructure.runtime_ipc import decode_message, encode_message, receive_message

REQUEST = "00000000-0000-4000-8000-000000000001"


def test_control_frame_roundtrip_has_big_endian_length_and_typed_request() -> None:
    framed = encode_message({"revision": 14, "request_id": REQUEST, "operation": "hello"}, 65536)
    assert struct.unpack("!I", framed[:4])[0] == len(framed) - 4
    request = decode_message(framed[4:], ControlRequest)
    assert request.request_id == UUID(REQUEST)
    assert request.operation == "hello" and request.revision == 14


@pytest.mark.parametrize(
    "payload",
    [
        b'{"revision":1,"revision":1}',
        b'{"revision":NaN}',
        b"\xff",
        b"[]",
        b'{"revision":true}',
        b'{"revision":12}',  # Older revisions must refuse; no legacy fallback.
        b'{"revision":13}',  # Immediately previous revision.
        b'{"revision":15}',  # Representative future revision.
        b'{"revision":1.0}',
        b'{"revision":1,"operation":"ask"}',
        b"{" + b'"x":[' * 1100 + b"0" + b"]" * 1100 + b"}",
    ],
)
def test_malformed_or_unbounded_message_is_safe_failure(payload: bytes) -> None:
    with pytest.raises(ControlFault) as caught:
        decode_message(payload, ControlRequest)
    assert caught.value.code in ("runtime_protocol_error", "runtime_protocol_incompatible")
    assert "NaN" not in str(caught.value)


@pytest.mark.parametrize("length,maximum", [(0, 4096), (4097, 4096), (16777217, 16777216)])
def test_advertised_length_is_rejected_without_waiting_for_or_allocating_body(
    length: int, maximum: int
) -> None:
    caller, receiver = socket.socketpair()
    with caller, receiver:
        caller.sendall(struct.pack("!I", length))
        began = time.monotonic()
        with pytest.raises(ControlFault):
            receive_message(receiver, maximum, time.monotonic() + 1, ControlRequest)
        assert time.monotonic() - began < 0.5


def test_truncated_frame_is_rejected_without_partial_success() -> None:
    caller, receiver = socket.socketpair()
    with caller, receiver:
        caller.sendall(struct.pack("!I", 100) + b"{}")
        caller.shutdown(socket.SHUT_WR)
        with pytest.raises(ControlFault):
            receive_message(receiver, 4096, time.monotonic() + 1, ControlRequest)


@pytest.mark.parametrize("revision", [12, 13, 15])
def test_v14_refuses_old_and_future_commands_with_current_revision_response(revision: int) -> None:
    refused = decode_message(
        b'{"revision":14,"authenticated":false,"error":"runtime_protocol_incompatible"}',
        AuthAccepted,
    )
    assert refused.error == "runtime_protocol_incompatible" and not refused.authenticated
    framed = encode_message(
        {"revision": revision, "request_id": REQUEST, "operation": "hello"}, 65536
    )
    with pytest.raises(ControlFault) as caught:
        decode_message(framed[4:], ControlRequest)
    assert caught.value.code == "runtime_protocol_incompatible"


@pytest.mark.parametrize("configured", (False, True))
@pytest.mark.parametrize("acknowledged", (False, True))
def test_native_stop_needs_job_confirmation_beyond_native_exit_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
    acknowledged: bool,
) -> None:
    import os
    from typing import Self
    from uuid import uuid4

    from jeff.application.projects import Failure
    from jeff.application.runtime import (
        ControlObservation,
        ControlSuccess,
        RuntimeIdentityData,
        RuntimeLayout,
    )
    from jeff.domain.runtime import RuntimeState
    from jeff.infrastructure import runtime_process as native

    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    identity = RuntimeIdentityData(
        instance_id=uuid4(), pid=12345
    )  # Synthetic; no kernel PID operations.
    observation = ControlObservation(
        state=RuntimeState.RUNNING,
        identity=identity,
        control_ready=True,
        execution_control_ready=configured,
    )
    requests: list[str] = []

    class Owner:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def require_no_endpoint(self) -> None:
            pass

    class Store:
        def read_credential(self) -> bytes:
            return bytes(32)  # Synthetic fixture value, not an installation credential.

        def acquire(self, *, create: bool) -> Owner:
            assert not create
            return Owner()

    class Connection(Owner):
        peer_pid = identity.pid

        def request(self, operation: str, instance: UUID) -> ControlObservation:
            assert instance == identity.instance_id
            requests.append(operation)
            if not acknowledged:
                raise ControlFault("runtime_stop_unconfirmed")
            return observation

    class Client:
        def __init__(self, *args: object) -> None:
            pass

        def connect(self, deadline: float) -> Connection:
            return Connection()

    class Events(Owner):
        def control(self, changes: object, maximum: int, timeout: float) -> list[object]:
            return [object()] if maximum else []  # Native exit observed by the fake adapter.

        def close(self) -> None:
            pass

    monkeypatch.setattr(native, "_stop_store", lambda *_: Store())
    monkeypatch.setattr(native, "ControlClient", Client)
    monkeypatch.setattr(select, "kqueue", Events, raising=False)
    monkeypatch.setattr(select, "kevent", lambda *_, **__: object(), raising=False)
    for name in ("KQ_FILTER_PROC", "KQ_EV_ADD", "KQ_EV_ENABLE", "KQ_EV_ONESHOT", "KQ_NOTE_EXIT"):
        monkeypatch.setattr(select, name, 1, raising=False)
    controller = native.RuntimeController(layout)
    monkeypatch.setattr(controller, "status", lambda: ControlSuccess(data=observation))
    result = controller.stop()
    assert isinstance(result, Failure) == (configured and not acknowledged)
    if isinstance(result, Failure):
        assert result.error.code == "runtime_stop_unconfirmed"
    assert requests == ["stop"]


def test_output_budget_is_checked_before_a_frame_is_published() -> None:
    with pytest.raises(ControlFault):
        encode_message({"revision": 1, "content": "SYNTHETIC" * 100}, 32)

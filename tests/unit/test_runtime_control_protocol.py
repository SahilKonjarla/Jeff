"""Strict bounded control messages through the codec, no live credential fixtures."""

import socket
import struct
import time
from uuid import UUID

import pytest

from jeff.application.runtime import ControlFault, ControlRequest
from jeff.infrastructure.runtime_ipc import decode_message, encode_message, receive_message

REQUEST = "00000000-0000-4000-8000-000000000001"


def test_control_frame_roundtrip_has_big_endian_length_and_typed_request() -> None:
    framed = encode_message({"revision": 1, "request_id": REQUEST, "operation": "hello"}, 65536)
    assert struct.unpack("!I", framed[:4])[0] == len(framed) - 4
    request = decode_message(framed[4:], ControlRequest)
    assert request.request_id == UUID(REQUEST)
    assert request.operation == "hello"


@pytest.mark.parametrize(
    "payload",
    [
        b'{"revision":1,"revision":1}',
        b'{"revision":NaN}',
        b"\xff",
        b"[]",
        b'{"revision":true}',
        b'{"revision":2}',
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


def test_output_budget_is_checked_before_a_frame_is_published() -> None:
    with pytest.raises(ControlFault):
        encode_message({"revision": 1, "content": "SYNTHETIC" * 100}, 32)

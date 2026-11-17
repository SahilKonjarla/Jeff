"""Private worker policy/framing on local sockets only; no helper or Git process."""

import json
import os
import socket
import struct
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from jeff.integrations import git
from jeff.integrations.git import worker


@pytest.mark.parametrize(
    "body", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b"\xff", b"[] trailing"]
)
def test_private_json_rejects_ambiguous_or_nonfinite_values(body: bytes) -> None:
    with pytest.raises((ValueError, UnicodeError)):
        worker._json(body)


@pytest.mark.parametrize(
    "update",
    [
        {"version": 2},
        {"operation": "arbitrary"},
        {"directory": "relative"},
        {"directory": "//synthetic/repo"},
        {"directory": "/synthetic/../repo"},
        {"directory": "/synthetic/\0"},
        {"deadline_ns": True},
        {"deadline_ns": 0},
        {"drivers": ["b", "a"]},
        {"drivers": ["a", "a"]},
        {"drivers": ["bad name"]},
        {"operation": "status.changes", "drivers": ["a"]},
        {"extra": "forbidden"},
    ],
)
def test_private_request_constraints_remain_strict(update: dict[str, object]) -> None:
    value = {
        "version": 1,
        "operation": "status.branch",
        "directory": "/synthetic/repo",
        "deadline_ns": 1,
        **update,
    }
    with pytest.raises(ValidationError):
        worker._Request.model_validate(value)


@pytest.mark.parametrize(
    "arguments",
    [
        ("status",),
        ("-c", "arbitrary=value"),
        ("rev-parse", "--show-toplevel", "extra"),
        (
            "-c",
            "filter.a.clean=command",
        ),
    ],
)
def test_unrecognized_argv_cannot_become_command_authority(arguments: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        worker._request(Path("/synthetic/repo"), arguments, 1.0)


def test_frame_round_trip_uses_root_wait_and_json_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[bool] = []
    decoded: list[bytes] = []
    original_wait, original_json = worker._wait, worker._json

    def wait(channel: socket.socket, deadline: float, watch: int | None, *, writing: bool) -> None:
        waits.append(writing)
        original_wait(channel, deadline, watch, writing=writing)

    def decode(body: bytes) -> object:
        decoded.append(body)
        return original_json(body)

    monkeypatch.setattr(worker, "_wait", wait)
    monkeypatch.setattr(worker, "_json", decode)
    left, right = socket.socketpair()
    with left, right:
        deadline = time.monotonic() + 1
        worker._send(left, {"unicode": "\u2603", "number": 1}, deadline, 256)
        assert worker._receive(right, deadline, 256) == {"unicode": "\u2603", "number": 1}
    assert waits == [True, False, False]
    assert decoded == [b'{"unicode":"\\u2603","number":1}']


@pytest.mark.parametrize(
    "body",
    [
        struct.pack("!I", 0),
        struct.pack("!I", 257),
        struct.pack("!I", 2) + b"{",
        struct.pack("!I", 13) + b'{"a":1,"a":2}',
    ],
)
def test_invalid_or_truncated_frame_is_rejected(body: bytes) -> None:
    left, right = socket.socketpair()
    with left, right:
        left.sendall(body)
        left.shutdown(socket.SHUT_WR)
        with pytest.raises(ValueError):
            worker._receive(right, time.monotonic() + 1, 256)


def test_readable_watch_aborts_before_channel_consumption() -> None:
    read, write = os.pipe()
    left, right = socket.socketpair()
    try:
        os.close(write)
        write = -1
        with pytest.raises(git._GitParentGone):
            worker._wait(left, time.monotonic() + 1, read, writing=False)
    finally:
        left.close()
        right.close()
        os.close(read)
        if write >= 0:
            os.close(write)


def test_expired_and_oversize_send_never_write() -> None:
    left, right = socket.socketpair()
    with left, right:
        with pytest.raises(TimeoutError):
            worker._send(left, {}, time.monotonic() - 1, 256)
        with pytest.raises(ValueError):
            worker._send(left, "x" * 256, time.monotonic() + 1, 256)
        right.setblocking(False)
        with pytest.raises(BlockingIOError):
            right.recv(1)


def test_request_driver_budget_uses_current_adapter_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git, "GIT_OUTPUT_LIMIT", 1)
    with pytest.raises(ValidationError):
        worker._Request(
            version=1,
            operation="status.branch",
            directory="/synthetic/repo",
            deadline_ns=1,
            drivers=["aa"],
        )


def test_frame_rejects_nonfinite_before_write() -> None:
    left, right = socket.socketpair()
    with left, right:
        with pytest.raises(ValueError):
            worker._send(left, json.loads('{"value":NaN}'), time.monotonic() + 1, 256)

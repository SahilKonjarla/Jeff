"""Existing IPC namespace fault hooks remain live after moving consumer classes."""

import asyncio
import os
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from pydantic import BaseModel

from jeff.application.dispatch import StreamFrame, SubscriptionQuery, SubscriptionRequest
from jeff.application.runtime import (
    AuthRequest,
    ControlFault,
    ControlRequest,
    RuntimeIdentityData,
    RuntimeLayout,
)
from jeff.infrastructure import runtime_ipc as ipc
from jeff.infrastructure.runtime_state import Ownership


def test_receive_message_still_uses_namespace_decode_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    original = ipc.decode_message
    seen: list[bytes] = []

    def decode(payload: bytes, model: type[BaseModel]) -> BaseModel:
        seen.append(payload)
        return original(payload, model)

    request = ControlRequest(revision=13, request_id=UUID(int=1), operation="hello")
    frame = ipc.encode_message(request.model_dump(mode="json"), 4096)
    monkeypatch.setattr(ipc, "decode_message", decode)
    sender, receiver = socket.socketpair()
    with sender, receiver:
        sender.sendall(frame)
        assert ipc.receive_message(receiver, 4096, time.monotonic() + 1, ControlRequest) == request
    assert seen == [frame[4:]]


def test_unary_exchange_still_uses_namespace_codec_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    original = ipc.encode_message
    sent: list[dict[str, object]] = []
    received: list[socket.socket] = []
    request = ControlRequest(revision=13, request_id=UUID(int=1), operation="hello")

    def encode(message: dict[str, object], maximum: int) -> bytes:
        sent.append(message)
        return original(message, maximum)

    def receive(
        connection: socket.socket, maximum: int, deadline: float, model: type[BaseModel]
    ) -> BaseModel:
        received.append(connection)
        assert model is ControlRequest
        return request

    monkeypatch.setattr(ipc, "encode_message", encode)
    monkeypatch.setattr(ipc, "receive_message", receive)
    sender, receiver = socket.socketpair()
    with sender, receiver:
        connection = cast(
            ipc.ControlConnection,
            SimpleNamespace(connection=sender, deadline=time.monotonic() + 1, used=False),
        )
        assert ipc.RuntimeProjectClient._exchange(connection, request, ControlRequest) == request
        assert connection.used
        assert receiver.recv(4096) == original(request.model_dump(mode="json"), 65536)
    assert sent == [request.model_dump(mode="json")] and received == [sender]


def test_control_connection_still_uses_namespace_peer_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    layout.runtime_dir.mkdir(mode=0o700)
    seen: list[socket.socket] = []

    def peer(connection: socket.socket) -> tuple[int, int]:
        seen.append(connection)
        raise ControlFault("runtime_peer_unverified")

    monkeypatch.setattr(ipc, "peer_identity", peer)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(layout.socket))
        layout.socket.chmod(0o600)
        listener.listen(1)
        with pytest.raises(ControlFault) as caught:
            ipc.ControlConnection(layout, bytes(32), time.monotonic() + 1)
        assert caught.value.code == "runtime_peer_unverified"
    assert len(seen) == 1 and seen[0].fileno() == -1


class Writer:
    def __init__(self) -> None:
        self.closed = False
        self.waited = False
        self.peer = object()

    def get_extra_info(self, name: str) -> object:
        assert name == "socket"
        return self.peer

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


def _server() -> ipc.ControlServer:
    owner = cast(
        Ownership, SimpleNamespace(store=SimpleNamespace(layout=SimpleNamespace(uid=os.geteuid())))
    )
    identity = RuntimeIdentityData(instance_id=UUID(int=1), pid=os.getpid())
    return ipc.ControlServer(owner, bytes(32), identity)


def test_server_checks_namespace_peer_hook_before_authentication_and_closes_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, writer = _server(), Writer()
    seen: list[object] = []

    def peer(connection: socket.socket) -> tuple[int, int]:
        seen.append(connection)
        raise ControlFault("runtime_peer_unverified")

    monkeypatch.setattr(ipc, "peer_identity", peer)

    async def exercise() -> None:
        await server._handle(asyncio.StreamReader(), cast(asyncio.StreamWriter, writer))

    asyncio.run(exercise())
    assert seen == [writer.peer] and writer.closed and writer.waited
    assert server._project_connections == 0


def test_server_subscription_refusal_still_uses_namespace_stream_sender_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, writer = _server(), Writer()
    query = SubscriptionQuery(query_id=UUID(int=2), directory=Path("/synthetic/project"))
    messages = [
        AuthRequest.model_validate({"revision": 13, "token": "00" * 32}),
        ipc.RpcRequest(SubscriptionRequest(expected_instance_id=UUID(int=3), query=query)),
    ]
    authenticated: list[BaseModel] = []
    frames: list[StreamFrame] = []

    async def receive(
        reader: asyncio.StreamReader, maximum: int, seconds: float, model: type[BaseModel]
    ) -> BaseModel:
        result = messages.pop(0)
        assert isinstance(result, model)
        return result

    async def send(writer: asyncio.StreamWriter, message: BaseModel, maximum: int) -> None:
        authenticated.append(message)

    async def stream_send(writer: asyncio.StreamWriter, frame: StreamFrame) -> None:
        frames.append(frame)

    monkeypatch.setattr(ipc, "peer_identity", lambda _: (os.geteuid(), os.getpid()))
    monkeypatch.setattr(ipc, "_async_receive", receive)
    monkeypatch.setattr(ipc, "_async_send", send)
    monkeypatch.setattr(ipc, "_stream_send", stream_send)

    async def exercise() -> None:
        await server._handle(asyncio.StreamReader(), cast(asyncio.StreamWriter, writer))

    asyncio.run(exercise())
    assert len(authenticated) == 1 and len(frames) == 1
    assert frames[0].kind == "closed" and frames[0].request_id == query.query_id
    assert frames[0].error is not None and frames[0].error.error.code == "runtime_busy"
    assert writer.closed and writer.waited and server._project_connections == 0

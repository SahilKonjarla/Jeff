"""Strict bounded control framing. No project dispatch or storage readiness."""

import asyncio
import ctypes
import hmac
import json
import os
import socket
import stat
import struct
import sys
import time
from typing import Self, cast
from uuid import UUID, uuid4

from pydantic import BaseModel

from jeff.application.runtime import (
    DEFAULT_RUNTIME_LIMITS,
    PROTOCOL_REVISION,
    AuthAccepted,
    AuthRequest,
    ControlFault,
    ControlIssueCode,
    ControlObservation,
    ControlOperation,
    ControlRequest,
    ControlResponse,
    RuntimeIdentityData,
    RuntimeLayout,
)
from jeff.domain.runtime import RuntimeState
from jeff.infrastructure.runtime_state import Ownership, RuntimeStateStore


def peer_identity(connection: socket.socket) -> tuple[int, int]:
    """Darwin getpeereid(int, uint32_t*, uint32_t*) and LOCAL_PEERPID=2 (SDK sys/un.h).

    No opaque native struct/claimed JSON identity, and no unsupported-platform fallback.
    """
    if sys.platform != "darwin":
        raise ControlFault("runtime_peer_unverified")
    try:
        function = ctypes.CDLL(None, use_errno=True).getpeereid
        function.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        function.restype = ctypes.c_int
        uid, gid = ctypes.c_uint32(), ctypes.c_uint32()
        if function(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise ControlFault("runtime_peer_unverified")
        pid = connection.getsockopt(0, 2)
        if pid <= 0:
            raise ControlFault("runtime_peer_unverified")
        return uid.value, pid
    except (AttributeError, OSError):
        raise ControlFault("runtime_peer_unverified") from None


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ValueError("Non-finite constant")


def decode_message[Message: BaseModel](payload: bytes, model: type[Message]) -> Message:
    try:
        data = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_object,
            parse_constant=_nonfinite,
        )
        if not isinstance(data, dict):
            raise ValueError("Message must be an object")
        revision = data.get("revision")
        if type(revision) is int and revision != PROTOCOL_REVISION:
            raise ControlFault("runtime_protocol_incompatible")
        return model.model_validate(data)
    except (ValueError, TypeError, RecursionError):
        raise ControlFault("runtime_protocol_error") from None


def encode_message(message: dict[str, object], maximum: int) -> bytes:
    try:
        payload = json.dumps(
            message, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if not 0 < len(payload) <= maximum <= 4294967295:
            raise ValueError("Frame budget")
        return struct.pack("!I", len(payload)) + payload
    except (ValueError, TypeError, RecursionError):
        raise ControlFault("runtime_protocol_error") from None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ControlFault("runtime_unreachable")
    return remaining


def _read_exact(connection: socket.socket, size: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        connection.settimeout(_remaining(deadline))
        part = connection.recv(size - len(chunks))
        if not part:
            raise ControlFault("runtime_protocol_error")
        chunks.extend(part)
    return bytes(chunks)


def receive_message[Message: BaseModel](
    connection: socket.socket, maximum: int, deadline: float, model: type[Message]
) -> Message:
    size = int(struct.unpack("!I", _read_exact(connection, 4, deadline))[0])
    if not 0 < size <= maximum:
        raise ControlFault("runtime_protocol_error")
    return decode_message(_read_exact(connection, size, deadline), model)


class ControlConnection:
    """One authenticated unary connection. No replay, no credential-bearing repr/result."""

    def __init__(self, layout: RuntimeLayout, token: bytes, deadline: float) -> None:
        self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.deadline = deadline
        try:
            store = RuntimeStateStore(layout)
            with store.directory(layout.runtime_dir, create=False) as root:
                try:
                    before = os.stat("daemon.sock", dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    raise ControlFault("runtime_unreachable") from None
                if not (
                    stat.S_ISSOCK(before.st_mode)
                    and before.st_uid == layout.uid
                    and stat.S_IMODE(before.st_mode) == 0o600
                    and before.st_nlink == 1
                ):
                    raise ControlFault("unsafe_runtime_path")
                self.connection.settimeout(_remaining(deadline))
                try:
                    self.connection.connect(str(layout.socket))
                except OSError:
                    raise ControlFault("runtime_unreachable") from None
                uid, self.peer_pid = peer_identity(self.connection)
                if uid != layout.uid:
                    raise ControlFault("runtime_peer_unverified")
                after = os.stat("daemon.sock", dir_fd=root, follow_symlinks=False)
                if (before.st_dev, before.st_ino, before.st_mode, before.st_uid) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_mode,
                    after.st_uid,
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
        except (OSError, ControlFault) as error:
            self.connection.close()
            if isinstance(error, ControlFault):
                raise
            raise ControlFault("runtime_unreachable") from None
        try:
            auth_deadline = min(deadline, time.monotonic() + DEFAULT_RUNTIME_LIMITS.auth_seconds)
            self.connection.settimeout(_remaining(auth_deadline))
            self.connection.sendall(
                encode_message(
                    {"revision": PROTOCOL_REVISION, "token": token.hex()},
                    DEFAULT_RUNTIME_LIMITS.auth_bytes,
                )
            )
            receive_message(
                self.connection, DEFAULT_RUNTIME_LIMITS.auth_bytes, auth_deadline, AuthAccepted
            )
        except (OSError, ControlFault) as error:
            self.connection.close()
            if isinstance(error, ControlFault) and error.code == "runtime_protocol_incompatible":
                raise
            raise ControlFault("runtime_authentication_failed") from None
        self.used = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.connection.close()

    def request(
        self, operation: ControlOperation, expected_instance_id: UUID | None = None
    ) -> ControlObservation:
        if self.used:
            raise ControlFault("runtime_protocol_error")
        self.used = True
        request = ControlRequest(
            revision=PROTOCOL_REVISION,
            request_id=uuid4(),
            operation=operation,
            expected_instance_id=expected_instance_id,
        )
        try:
            deadline = min(self.deadline, time.monotonic() + DEFAULT_RUNTIME_LIMITS.frame_seconds)
            self.connection.settimeout(_remaining(deadline))
            self.connection.sendall(
                encode_message(
                    request.model_dump(mode="json"), DEFAULT_RUNTIME_LIMITS.request_bytes
                )
            )
            response = receive_message(
                self.connection, DEFAULT_RUNTIME_LIMITS.response_bytes, deadline, ControlResponse
            )
            if response.request_id != request.request_id:
                raise ControlFault("runtime_protocol_error")
            if response.error is not None:
                raise ControlFault(response.error)
            observation = response.observation
            if (
                observation is None
                or observation.identity is None
                or observation.identity.pid != self.peer_pid
            ):
                raise ControlFault("runtime_ownership_unconfirmed")
            if (
                expected_instance_id is not None
                and observation.identity.instance_id != expected_instance_id
            ):
                raise ControlFault("runtime_ownership_unconfirmed")
            return observation
        except OSError:
            raise ControlFault("runtime_unreachable") from None


class ControlClient:
    def __init__(self, layout: RuntimeLayout, token: bytes) -> None:
        if len(token) != 32:
            raise ControlFault("runtime_credentials_invalid")
        self.layout = layout
        self._token = token

    def connect(self, deadline: float) -> ControlConnection:
        return ControlConnection(self.layout, self._token, deadline)

    def request(
        self,
        operation: ControlOperation,
        *,
        deadline: float,
        expected_instance_id: UUID | None = None,
    ) -> ControlObservation:
        with self.connect(deadline) as connection:
            return connection.request(operation, expected_instance_id)


async def _async_receive[Message: BaseModel](
    reader: asyncio.StreamReader, maximum: int, seconds: float, model: type[Message]
) -> Message:
    async def frame() -> bytes:
        size = int(struct.unpack("!I", await reader.readexactly(4))[0])
        if not 0 < size <= maximum:
            raise ControlFault("runtime_protocol_error")
        return await reader.readexactly(size)

    return decode_message(await asyncio.wait_for(frame(), timeout=seconds), model)


async def _async_send(writer: asyncio.StreamWriter, message: BaseModel, maximum: int) -> None:
    writer.write(encode_message(message.model_dump(mode="json"), maximum))
    await asyncio.wait_for(writer.drain(), timeout=DEFAULT_RUNTIME_LIMITS.frame_seconds)


class ControlServer:
    def __init__(self, owner: Ownership, token: bytes, identity: RuntimeIdentityData) -> None:
        if len(token) != 32 or identity.pid != os.getpid():
            raise ControlFault("runtime_ownership_unconfirmed")
        self.owner = owner
        self._token = token
        self.identity = identity
        self.stopping = False
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.stopping or len(self._tasks) >= DEFAULT_RUNTIME_LIMITS.connections:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            uid, _ = peer_identity(cast(socket.socket, writer.get_extra_info("socket")))
            if uid != self.owner.store.layout.uid:
                raise ControlFault("runtime_peer_unverified")
            auth = await _async_receive(
                reader,
                DEFAULT_RUNTIME_LIMITS.auth_bytes,
                DEFAULT_RUNTIME_LIMITS.auth_seconds,
                AuthRequest,
            )
            if not hmac.compare_digest(auth.token.get_secret_value(), self._token.hex()):
                raise ControlFault("runtime_authentication_failed")
            await _async_send(
                writer,
                AuthAccepted(revision=PROTOCOL_REVISION, authenticated=True),
                DEFAULT_RUNTIME_LIMITS.auth_bytes,
            )
            request = await _async_receive(
                reader,
                DEFAULT_RUNTIME_LIMITS.request_bytes,
                DEFAULT_RUNTIME_LIMITS.frame_seconds,
                ControlRequest,
            )
            error: ControlIssueCode | None = None
            if (
                request.expected_instance_id is not None
                and request.expected_instance_id != self.identity.instance_id
            ):
                error = "runtime_ownership_unconfirmed"
            elif self.stopping:
                error = "runtime_busy"
            if error:
                response = ControlResponse(
                    revision=PROTOCOL_REVISION, request_id=request.request_id, error=error
                )
            else:
                if request.operation == "stop":
                    self.stopping = True
                    self._stop.set()  # accepted shutdown persists even if reply delivery fails
                observation = ControlObservation(
                    state=RuntimeState.STOPPING if self.stopping else RuntimeState.RUNNING,
                    identity=self.identity,
                    control_ready=not self.stopping,
                )
                response = ControlResponse(
                    revision=PROTOCOL_REVISION,
                    request_id=request.request_id,
                    observation=observation,
                )
            await _async_send(writer, response, DEFAULT_RUNTIME_LIMITS.response_bytes)
        except (ControlFault, OSError, ValueError, asyncio.IncompleteReadError, TimeoutError):
            pass  # Never log/echo auth, raw frames, parser or OS details.
        finally:
            writer.close()
            try:
                await asyncio.wait_for(
                    writer.wait_closed(), timeout=DEFAULT_RUNTIME_LIMITS.frame_seconds
                )
            except (OSError, TimeoutError):
                pass

    async def serve(self) -> None:
        self.owner.require_no_endpoint()
        endpoint = None
        server = None
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.owner.store.layout.socket))
            endpoint = self.owner.endpoint_identity()
            listener.setblocking(False)
            server = await asyncio.start_unix_server(
                self._accept,
                sock=listener,
                backlog=DEFAULT_RUNTIME_LIMITS.connections,
                limit=DEFAULT_RUNTIME_LIMITS.request_bytes + 4,
            )
            await self._stop.wait()
        finally:
            self.stopping = True
            if server is not None:
                server.close()
                await server.wait_closed()
            listener.close()
            if self._tasks:
                _, pending = await asyncio.wait(
                    self._tasks, timeout=2 * DEFAULT_RUNTIME_LIMITS.frame_seconds
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            if endpoint is not None:
                self.owner.remove_endpoint(endpoint)

"""Bounded native/authenticated unary transport; no SQL or Git execution on the loop."""

import asyncio
import ctypes
import errno
import hmac
import json
import math
import os
import select
import socket
import stat
import struct
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Self, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, RootModel

from jeff.application.dispatch import (
    PROJECT_OPERATIONS,
    ActivityFault,
    DispatchReply,
    EvidenceQuery,
    EvidenceReply,
    EvidenceRequest,
    ProjectCall,
    ProjectRequest,
    ProjectStatusService,
    ProjectStatusSuccess,
    ReceiptQuery,
    ReceiptReply,
    ReceiptRequest,
    StreamFrame,
    StreamReply,
    SubscriptionQuery,
    SubscriptionRequest,
    dispatch_failure,
)
from jeff.application.history import StreamCursor
from jeff.application.projects import Failure
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
from jeff.application.storage import StorageReadiness
from jeff.domain.runtime import RuntimeState
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher
from jeff.infrastructure.runtime_events import RuntimeEvents
from jeff.infrastructure.runtime_state import Ownership, RuntimeStateStore


class RpcRequest(
    RootModel[
        Annotated[
            ControlRequest
            | ProjectRequest
            | ReceiptRequest
            | EvidenceRequest
            | SubscriptionRequest,
            Field(discriminator="kind"),
        ]
    ]
):
    pass


class _PeerDisconnected(ControlFault):
    """Kernel reports a gone connection, not a verified or mismatched identity."""

    def __init__(self) -> None:
        super().__init__("runtime_peer_unverified")


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
            if ctypes.get_errno() == errno.ENOTCONN:
                raise _PeerDisconnected()
            raise ControlFault("runtime_peer_unverified")
        pid = connection.getsockopt(0, 2)
        if pid <= 0:
            raise ControlFault("runtime_peer_unverified")
        return uid.value, pid
    except OSError as error:
        if error.errno == errno.ENOTCONN:
            raise _PeerDisconnected() from None
        raise ControlFault("runtime_peer_unverified") from None
    except AttributeError:
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
            reply = receive_message(
                self.connection, DEFAULT_RUNTIME_LIMITS.auth_bytes, auth_deadline, AuthAccepted
            )
            if reply.error is not None:
                raise ControlFault(reply.error)
            if reply.identity is None or reply.identity.pid != self.peer_pid:
                raise ControlFault("runtime_peer_unverified")
            self.identity = reply.identity
        except (OSError, ControlFault) as error:
            self.connection.close()
            if isinstance(error, ControlFault) and error.code in (
                "runtime_protocol_incompatible",
                "runtime_authentication_failed",
                "runtime_peer_unverified",
            ):
                raise
            raise ControlFault("runtime_handshake_unconfirmed") from None
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
                or observation.identity != self.identity
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


def inspect_control(layout: RuntimeLayout, deadline: float) -> ControlObservation:
    """Diagnostic status via existing safe credential/native client; no lifecycle/SQL I/O."""
    _remaining(deadline)
    store = RuntimeStateStore(layout)
    client = ControlClient(layout, store.read_credential())
    _remaining(deadline)
    return client.request("status", deadline=deadline)


class OwnedActivityStream:
    """Caller owns one authenticated socket; one reader, independent idempotent close."""

    def __init__(
        self, connection: ControlConnection, ready: StreamFrame, query: SubscriptionQuery
    ) -> None:
        self.connection, self.ready, self.query = connection, ready, query
        self._closed = threading.Event()
        self._reader = threading.Lock()
        if ready.cursor is None or ready.metadata is None:
            self.close()
            raise ControlFault("runtime_protocol_error")
        try:
            self._position = StreamCursor.decode(ready.cursor)
        except ValueError:
            self.close()
            raise ControlFault("runtime_protocol_error") from None
        if (
            self._position.database_epoch != ready.metadata.database_epoch
            or self._position.retention_generation != ready.metadata.retention_generation
            or self._position.after > ready.metadata.high_water
            or self._position.after < ready.metadata.replay_floor
            or (
                query.expected_root is not None
                and query.expected_root != self._position.canonical_root
            )
            or (
                query.expected_project_id is not None
                and query.expected_project_id != self._position.project_id
            )
            or (query.cursor is not None and query.cursor != ready.cursor)
        ):
            self.close()
            raise ControlFault("runtime_protocol_error")
        self.last_received_cursor = ready.cursor

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed.is_set():
            self._closed.set()
            try:
                self.connection.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.connection.close()

    def receive(self, wait_seconds: float | None = None) -> StreamFrame | None:
        if wait_seconds is not None and (not math.isfinite(wait_seconds) or wait_seconds < 0):
            raise ValueError("Idle wait must be finite and nonnegative.")
        if not self._reader.acquire(blocking=False):
            raise ControlFault("runtime_protocol_error")
        try:
            if self._closed.is_set():
                raise ControlFault("activity_stream_closed")
            if not select.select([self.connection.connection], [], [], wait_seconds)[0]:
                return None
            frame = receive_message(
                self.connection.connection,
                DEFAULT_RUNTIME_LIMITS.event_bytes,
                time.monotonic() + DEFAULT_RUNTIME_LIMITS.frame_seconds,
                StreamFrame,
            )
            if (
                frame.request_id != self.query.query_id
                or frame.identity != self.connection.identity
                or frame.kind == "ready"
            ):
                raise ControlFault("runtime_protocol_error")
            if frame.cursor is not None:
                position = StreamCursor.decode(frame.cursor)
                if (
                    position.project_id != self._position.project_id
                    or position.canonical_root != self._position.canonical_root
                    or position.database_epoch != self._position.database_epoch
                    or position.retention_generation != self._position.retention_generation
                    or position.after < self._position.after
                    or (frame.kind != "closed" and position.after == self._position.after)
                ):
                    raise ControlFault("runtime_protocol_error")
                if frame.event is not None and (
                    frame.event.sequence != position.after
                    or frame.event.project_id != position.project_id
                    or frame.event.canonical_root != position.canonical_root
                ):
                    raise ControlFault("runtime_protocol_error")
                self._position, self.last_received_cursor = position, frame.cursor
            if frame.kind == "closed":
                self.close()
            return frame
        except (OSError, ValueError, ControlFault):
            self.close()
            raise ControlFault("activity_stream_closed") from None
        finally:
            self._reader.release()


class RuntimeProjectClient:
    """One authenticated unary call; no autostart, response replay or local fallback."""

    def __init__(
        self,
        layout: RuntimeLayout | Callable[[], RuntimeLayout],
        *,
        diagnostics: ProjectStatusService | None = None,
    ) -> None:
        self._layout = layout
        self._diagnostics = diagnostics

    def status(self, directory: Path) -> ProjectStatusSuccess | Failure:
        if self._diagnostics is None:
            raise ValueError("Local diagnostics were not composed for this RPC-only client.")
        return self._diagnostics.execute(directory)

    def _connect(self, deadline: float) -> ControlConnection:
        layout = self._layout() if callable(self._layout) else self._layout
        try:
            store = RuntimeStateStore(layout)
            owner = store.acquire(create=False)
            if owner is not None:
                with owner:
                    owner.require_no_endpoint()
                raise ControlFault("daemon_not_running")
        except ControlFault as error:
            if error.code == "runtime_absent":
                raise ControlFault("daemon_not_running") from None
            raise
        token = store.read_credential()
        return ControlClient(layout, token).connect(deadline)

    @staticmethod
    def _exchange[Reply: BaseModel](
        connection: ControlConnection, request: BaseModel, model: type[Reply]
    ) -> Reply:
        if connection.used:
            raise ControlFault("runtime_protocol_error")
        connection.used = True
        connection.connection.settimeout(_remaining(connection.deadline))
        connection.connection.sendall(
            encode_message(request.model_dump(mode="json"), DEFAULT_RUNTIME_LIMITS.request_bytes)
        )
        return receive_message(
            connection.connection, DEFAULT_RUNTIME_LIMITS.response_bytes, connection.deadline, model
        )

    def subscribe(self, query: SubscriptionQuery) -> OwnedActivityStream:
        deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.request_seconds
        with self._connect(deadline) as preflight:
            observation = preflight.request("status", preflight.identity.instance_id)
            if (
                not observation.application_ready
                or "activity.subscribe" not in observation.available_operations
            ):
                raise ControlFault("runtime_capability_unavailable")
            identity = preflight.identity
        connection = self._connect(deadline)
        try:
            if connection.identity != identity:
                raise ControlFault("runtime_ownership_unconfirmed")
            request = SubscriptionRequest(
                query=query,
                expected_instance_id=identity.instance_id,
                remaining_ms=max(1, int(_remaining(deadline) * 1000)),
            )
            connection.used = True
            connection.connection.settimeout(_remaining(deadline))
            connection.connection.sendall(
                encode_message(
                    request.model_dump(mode="json"), DEFAULT_RUNTIME_LIMITS.request_bytes
                )
            )
            ready = receive_message(
                connection.connection, DEFAULT_RUNTIME_LIMITS.event_bytes, deadline, StreamFrame
            )
            if ready.request_id != query.query_id or ready.identity != identity:
                raise ControlFault("runtime_ownership_unconfirmed")
            if ready.kind == "closed" and ready.error is not None:
                raise ActivityFault(ready.error)
            if ready.kind != "ready":
                raise ControlFault("runtime_protocol_error")
            return OwnedActivityStream(connection, ready, query)
        except BaseException:
            connection.connection.close()
            raise

    def execute(self, call: ProjectCall) -> DispatchReply:
        deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.request_seconds
        with self._connect(deadline) as connection:
            try:
                request = ProjectRequest(
                    call=call,
                    expected_instance_id=connection.identity.instance_id,
                    remaining_ms=max(1, int(_remaining(deadline) * 1000)),
                )
                reply = self._exchange(connection, request, DispatchReply)
                if reply.request_id != call.request_id or reply.identity != connection.identity:
                    raise ControlFault("runtime_ownership_unconfirmed")
                return reply
            except (OSError, ControlFault):
                return DispatchReply(
                    request_id=call.request_id,
                    identity=connection.identity,
                    result=dispatch_failure("request_outcome_unknown"),
                    accepted=None,
                )

    def inspect_evidence(self, query: EvidenceQuery) -> EvidenceReply:
        deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.request_seconds
        with self._connect(deadline) as connection:
            try:
                request = EvidenceRequest(
                    query=query,
                    expected_instance_id=connection.identity.instance_id,
                    remaining_ms=max(1, int(_remaining(deadline) * 1000)),
                )
                reply = self._exchange(connection, request, EvidenceReply)
                if (
                    reply.request_id != query.query_id
                    or reply.identity != connection.identity
                    or reply.operation != query.operation
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
                return reply
            except (OSError, ControlFault):
                return EvidenceReply(
                    request_id=query.query_id,
                    identity=connection.identity,
                    operation=query.operation,
                    error=dispatch_failure("request_outcome_unknown"),
                )

    def inspect_receipt(self, query: ReceiptQuery) -> ReceiptReply:
        deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.request_seconds
        with self._connect(deadline) as connection:
            try:
                request = ReceiptRequest(
                    query=query,
                    expected_instance_id=connection.identity.instance_id,
                    remaining_ms=max(1, int(_remaining(deadline) * 1000)),
                )
                reply = self._exchange(connection, request, ReceiptReply)
                if reply.request_id != query.query_id or reply.identity != connection.identity:
                    raise ControlFault("runtime_ownership_unconfirmed")
                return reply
            except (OSError, ControlFault):
                return ReceiptReply(
                    request_id=query.query_id,
                    identity=connection.identity,
                    error=dispatch_failure("request_outcome_unknown"),
                )


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


async def _stream_send(writer: asyncio.StreamWriter, frame: StreamFrame) -> None:
    await _async_send(writer, frame, DEFAULT_RUNTIME_LIMITS.event_bytes)


class ControlServer:
    def __init__(
        self,
        owner: Ownership,
        token: bytes,
        identity: RuntimeIdentityData,
        storage_snapshot: Callable[[], StorageReadiness] = StorageReadiness,
        *,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        if len(token) != 32 or identity.pid != os.getpid():
            raise ControlFault("runtime_ownership_unconfirmed")
        self.owner = owner
        self._token = token
        self.identity = identity
        self.storage_snapshot = storage_snapshot
        self._on_stop = on_stop
        self.dispatcher: RuntimeDispatcher | None = None
        self.events: RuntimeEvents | None = None
        self._stop_deadline: float | None = None
        self._project_connections = 0
        self.stopping = False
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._tasks) >= DEFAULT_RUNTIME_LIMITS.connections:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        project_slot = False
        response: ControlResponse | DispatchReply | ReceiptReply | EvidenceReply
        try:
            uid, _ = peer_identity(cast(socket.socket, writer.get_extra_info("socket")))
            if uid != self.owner.store.layout.uid:
                raise ControlFault("runtime_peer_unverified")
            try:
                auth = await _async_receive(
                    reader,
                    DEFAULT_RUNTIME_LIMITS.auth_bytes,
                    DEFAULT_RUNTIME_LIMITS.auth_seconds,
                    AuthRequest,
                )
            except ControlFault as fault:
                if fault.code == "runtime_protocol_incompatible":
                    await _async_send(
                        writer,
                        AuthAccepted(
                            revision=PROTOCOL_REVISION,
                            authenticated=False,
                            error="runtime_protocol_incompatible",
                        ),
                        DEFAULT_RUNTIME_LIMITS.auth_bytes,
                    )
                return
            if not hmac.compare_digest(auth.token.get_secret_value(), self._token.hex()):
                await _async_send(
                    writer,
                    AuthAccepted(
                        revision=PROTOCOL_REVISION,
                        authenticated=False,
                        error="runtime_authentication_failed",
                    ),
                    DEFAULT_RUNTIME_LIMITS.auth_bytes,
                )
                return
            await _async_send(
                writer,
                AuthAccepted(
                    revision=PROTOCOL_REVISION, authenticated=True, identity=self.identity
                ),
                DEFAULT_RUNTIME_LIMITS.auth_bytes,
            )
            began = time.monotonic()
            request = (
                await _async_receive(
                    reader,
                    DEFAULT_RUNTIME_LIMITS.request_bytes,
                    DEFAULT_RUNTIME_LIMITS.frame_seconds,
                    RpcRequest,
                )
            ).root
            if isinstance(request, SubscriptionRequest):
                if (
                    self.stopping
                    or self.events is None
                    or not self.events.ready
                    or self._project_connections >= DEFAULT_RUNTIME_LIMITS.connections - 1
                    or request.expected_instance_id != self.identity.instance_id
                ):
                    from jeff.application.runtime import control_failure

                    await _stream_send(
                        writer,
                        StreamFrame(
                            kind="closed",
                            request_id=request.query.query_id,
                            identity=self.identity,
                            error=control_failure("runtime_busy"),
                        ),
                    )
                    return
                project_slot = True
                self._project_connections += 1
                writer.transport.set_write_buffer_limits(high=65536, low=32768)
                await self.events.open(
                    request.query,
                    reader,
                    writer,
                    began
                    + min(request.remaining_ms / 1000, DEFAULT_RUNTIME_LIMITS.request_seconds),
                )
                return
            if isinstance(request, (ProjectRequest, ReceiptRequest, EvidenceRequest)):
                if self._project_connections >= DEFAULT_RUNTIME_LIMITS.connections - 1:
                    response = self._project_refusal(request, busy=True)
                else:
                    project_slot = True
                    self._project_connections += 1
                    response = await self._project(request, began)
                await _async_send(writer, response, DEFAULT_RUNTIME_LIMITS.response_bytes)
                return
            error: ControlIssueCode | None = None
            if (
                request.expected_instance_id is not None
                and request.expected_instance_id != self.identity.instance_id
            ):
                error = "runtime_ownership_unconfirmed"
            if error:
                response = ControlResponse(
                    revision=PROTOCOL_REVISION, request_id=request.request_id, error=error
                )
            else:
                if request.operation == "stop":
                    self.request_stop()  # shutdown persists even if reply delivery fails
                storage = self.storage_snapshot()
                observation = ControlObservation(
                    state=RuntimeState.STOPPING if self.stopping else RuntimeState.RUNNING,
                    identity=self.identity,
                    control_ready=not self.stopping,
                    application_ready=(
                        not self.stopping
                        and storage.state == "ready"
                        and self.dispatcher is not None
                        and self.dispatcher.ready
                        and self.events is not None
                        and self.events.ready
                    ),
                    storage=storage,
                    available_operations=(*PROJECT_OPERATIONS, "activity.subscribe")
                    if not self.stopping
                    and storage.state == "ready"
                    and self.dispatcher is not None
                    and self.dispatcher.ready
                    and self.events is not None
                    and self.events.ready
                    else (),
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
            if project_slot:
                self._project_connections -= 1
            writer.close()
            try:
                await asyncio.wait_for(
                    writer.wait_closed(), timeout=DEFAULT_RUNTIME_LIMITS.frame_seconds
                )
            except (OSError, TimeoutError):
                pass

    def _project_refusal(
        self,
        request: ProjectRequest | ReceiptRequest | EvidenceRequest,
        *,
        busy: bool = False,
        unknown: bool = False,
    ) -> DispatchReply | ReceiptReply | EvidenceReply:
        error = dispatch_failure(
            "request_outcome_unknown" if unknown else "runtime_busy" if busy else "runtime_stopping"
        )
        if isinstance(request, ProjectRequest):
            return DispatchReply(
                request_id=request.call.request_id,
                identity=self.identity,
                result=error,
                accepted=None if unknown else False,
            )
        if isinstance(request, EvidenceRequest):
            return EvidenceReply(
                request_id=request.query.query_id,
                identity=self.identity,
                operation=request.query.operation,
                error=error,
            )
        return ReceiptReply(request_id=request.query.query_id, identity=self.identity, error=error)

    async def _project(
        self, request: ProjectRequest | ReceiptRequest | EvidenceRequest, began: float
    ) -> DispatchReply | ReceiptReply | EvidenceReply:
        if request.expected_instance_id != self.identity.instance_id:
            error = self._project_refusal(request)
            failure = dispatch_failure("project_target_mismatch")
            return (
                error.model_copy(update={"result": failure})
                if isinstance(error, DispatchReply)
                else error.model_copy(update={"error": failure})
            )
        if self.stopping:
            return self._project_refusal(request)
        if self.storage_snapshot().state != "ready":
            if self.dispatcher is not None:
                self.dispatcher.request_close()
            return self._project_refusal(request, busy=True)
        if self.dispatcher is None or not self.dispatcher.ready:
            return self._project_refusal(request, busy=True)
        deadline = began + min(request.remaining_ms / 1000, DEFAULT_RUNTIME_LIMITS.request_seconds)
        try:
            work = request.call if isinstance(request, ProjectRequest) else request.query
            future = self.dispatcher.submit(work, deadline)
        except ControlFault:
            return self._project_refusal(request, busy=True)
        try:
            reply = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)),
                timeout=max(0, deadline - time.monotonic()),
            )
            if isinstance(reply, StreamReply):
                raise ControlFault("runtime_protocol_error")
            return reply
        except TimeoutError:
            return self._project_refusal(request, unknown=True)

    def request_stop(self) -> None:
        if self._stop_deadline is None:
            self._stop_deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.stop_seconds
        if not self.stopping and self._on_stop is not None:
            self._on_stop()
        self.stopping = True
        if self.events is not None:
            self.events.begin_stop()
        if self.dispatcher is not None:
            self.dispatcher.begin_drain()
        self._stop.set()

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
            if self.dispatcher is not None:
                self.dispatcher.begin_drain()
                while not self.dispatcher.actions_drained:
                    await asyncio.sleep(0.01)
            if self.events is not None:
                await self.events.drain(
                    min(
                        time.monotonic() + 5,
                        self._stop_deadline
                        if self._stop_deadline is not None
                        else time.monotonic() + 5,
                    )
                )
            if self.dispatcher is not None:
                self.dispatcher.request_close()
                while not self.dispatcher.closed:
                    await asyncio.sleep(0.01)
                self.dispatcher.close(timeout=0)
        finally:
            self.request_stop()
            if self.events is not None:
                await self.events.drain(time.monotonic())
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

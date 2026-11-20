"""Native trust/framing primitives and compatible authenticated IPC exports.

Consumer modules resolve these primitives through this namespace at call time.
This intentionally preserves existing native-peer, codec and stream fault hooks;
no consumer accesses partially initialized exports during import.
"""

import asyncio
import ctypes
import errno
import json
import socket
import struct
import sys
import time

from pydantic import BaseModel

from jeff.application.dispatch import StreamFrame
from jeff.application.runtime import (
    DEFAULT_RUNTIME_LIMITS,
    PROTOCOL_REVISION,
    ControlFault,
    ControlResponse,
)

from .activity import OwnedActivityStream
from .connection import ControlClient, ControlConnection, inspect_control
from .project_client import RuntimeProjectClient
from .requests import RpcRequest
from .server import ControlServer


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
    """Reject ambiguous JSON before typed validation; never echo invalid payloads."""
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
    """Bound the encoded UTF-8 body before publishing its network-order length prefix."""
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
    """Validate advertised size before reading the body, sharing one absolute deadline."""
    size = int(struct.unpack("!I", _read_exact(connection, 4, deadline))[0])
    if not 0 < size <= maximum:
        raise ControlFault("runtime_protocol_error")
    return decode_message(_read_exact(connection, size, deadline), model)


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


__all__ = [
    "ControlClient",
    "ControlConnection",
    "ControlResponse",
    "ControlServer",
    "OwnedActivityStream",
    "RpcRequest",
    "RuntimeProjectClient",
    "decode_message",
    "encode_message",
    "inspect_control",
    "peer_identity",
    "receive_message",
]

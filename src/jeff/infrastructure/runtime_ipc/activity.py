"""One caller-owned authenticated activity stream with bounded cursor advancement."""

import math
import select
import socket
import threading
import time
from typing import Self

from jeff.application.dispatch import StreamFrame, SubscriptionQuery
from jeff.application.history import StreamCursor
from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS, ControlFault

from .. import runtime_ipc as ipc
from .connection import ControlConnection


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
        """Return None only for an idle wait; partial/invalid frames close this stream.

        Exactly one reader may advance the received cursor. That cursor is not a
        consumer processing acknowledgement or permission to replay an action.
        """
        if wait_seconds is not None and (not math.isfinite(wait_seconds) or wait_seconds < 0):
            raise ValueError("Idle wait must be finite and nonnegative.")
        if not self._reader.acquire(blocking=False):
            raise ControlFault("runtime_protocol_error")
        try:
            if self._closed.is_set():
                raise ControlFault("activity_stream_closed")
            if not select.select([self.connection.connection], [], [], wait_seconds)[0]:
                return None
            frame = ipc.receive_message(
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

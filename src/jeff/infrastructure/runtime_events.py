"""Bounded committed-journal delivery. One pump/read, one immutable page/writer per peer.

No connection/SQL owner, generic bus or volatile source of truth. Hints carry no events.
"""

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID

from jeff.application.dispatch import (
    StreamFrame,
    StreamQuery,
    StreamReply,
    SubscriptionQuery,
)
from jeff.application.history import ActivityMeta, StreamPage
from jeff.application.projects import Failure
from jeff.application.runtime import (
    DEFAULT_RUNTIME_LIMITS,
    ControlFault,
    RuntimeIdentityData,
    control_failure,
)
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher

Send = Callable[[asyncio.StreamWriter, StreamFrame], Awaitable[None]]


@dataclass
class _Subscriber:
    query: StreamQuery
    writer: asyncio.StreamWriter
    cursor: str | None = None
    page: StreamPage | None = None
    final_page: bool = False
    error: Failure | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)


class RuntimeEvents:
    def __init__(
        self, identity: RuntimeIdentityData, send: Send, healthy: Callable[[], bool]
    ) -> None:
        self.identity, self.send, self.healthy = identity, send, healthy
        self.dispatcher: RuntimeDispatcher | None = None
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._hint_lock = threading.Lock()
        self._hint_pending = False
        self._closed = False
        self._stopping = False
        self._final = False
        self._healthy = True
        self._read_lock = asyncio.Lock()
        self._streams: dict[UUID, _Subscriber] = {}
        self._pump_task: asyncio.Task[None] | None = None

    @property
    def ready(self) -> bool:
        return (
            self._healthy
            and not self._stopping
            and not self._closed
            and self._pump_task is not None
            and not self._pump_task.done()
            and self.healthy()
        )

    def start(self, dispatcher: RuntimeDispatcher) -> None:
        if self._pump_task is not None:
            raise ControlFault("runtime_ownership_unconfirmed")
        self.dispatcher = dispatcher
        self._pump_task = asyncio.create_task(self._pump())

    def signal(self) -> None:
        """SQL-thread safe: at most one loop callback; never an event payload or wait."""
        with self._hint_lock:
            if self._closed or self._hint_pending:
                return
            self._hint_pending = True
        try:
            self._loop.call_soon_threadsafe(self._deliver_hint)
        except RuntimeError:
            with self._hint_lock:
                self._hint_pending = False
            raise

    def _deliver_hint(self) -> None:
        with self._hint_lock:
            self._hint_pending = False
        if not self._closed:
            self._wake.set()

    async def _read(self, query: StreamQuery, deadline: float) -> StreamReply:
        async with self._read_lock:
            if time.monotonic() >= deadline:
                raise ControlFault("runtime_busy")
            if self.dispatcher is None:
                raise ControlFault("runtime_busy")
            future = asyncio.wrap_future(self.dispatcher.submit(query, deadline))
            try:
                reply = await asyncio.shield(future)
            except asyncio.CancelledError:
                # Keep the global read slot until the actual owned worker finishes.
                await asyncio.shield(future)
                raise
            if not isinstance(reply, StreamReply):
                raise ControlFault("runtime_protocol_error")
            return reply

    def _frame(self, sub: _Subscriber, kind: str, **fields: object) -> StreamFrame:
        return StreamFrame.model_validate(
            {
                "kind": kind,
                "request_id": sub.query.query_id,
                "identity": self.identity,
                "cursor": sub.cursor,
                **fields,
            }
        )

    async def open(
        self,
        query: SubscriptionQuery,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        deadline: float,
    ) -> None:
        if (
            not self.ready
            or len(self._streams) >= DEFAULT_RUNTIME_LIMITS.subscriptions
            or query.query_id in self._streams
        ):
            await self.send(
                writer,
                StreamFrame(
                    kind="closed",
                    request_id=query.query_id,
                    identity=self.identity,
                    error=control_failure("runtime_busy"),
                ),
            )
            return
        sub = _Subscriber(StreamQuery(**query.model_dump()), writer)
        self._streams[query.query_id] = sub  # registered BEFORE snapshot/handoff
        disconnected = asyncio.create_task(reader.read(1))
        disconnected.add_done_callback(lambda task: sub.wake.set())
        try:
            reply = await self._read(sub.query, deadline)
            if reply.error is not None:
                sub.error = reply.error
                return
            assert reply.page is not None
            page = reply.page
            if time.monotonic() >= deadline or self._stopping:
                sub.error = control_failure("activity_stream_closed")
                return
            sub.cursor = page.position(page.start_after).encode()
            sub.query = StreamQuery(
                directory=page.canonical_root,
                expected_root=page.canonical_root,
                expected_project_id=page.project_id,
                query_id=query.query_id,
                cursor=sub.cursor,
            )
            sub.page = page
            metadata = ActivityMeta(
                database_epoch=page.database_epoch,
                coverage_started_at_ms=page.coverage_started_at_ms,
                high_water=page.high_water,
                retention_generation=page.retention_generation,
            )
            await asyncio.wait_for(
                self.send(writer, self._frame(sub, "ready", metadata=metadata)),
                max(0, deadline - time.monotonic()),
            )
            while sub.error is None:
                if disconnected.done():
                    sub.error = control_failure("activity_stream_closed")
                    break
                current = sub.page
                if current is None:
                    sub.wake.clear()
                    if sub.page is None and sub.error is None:
                        await sub.wake.wait()
                    continue
                # The page is retained until every event/write finishes: max64 TOTAL credits.
                for event in current.events:
                    if sub.error is not None:
                        break
                    cursor = current.position(event.sequence).encode()
                    await self.send(writer, self._frame(sub, "event", cursor=cursor, event=event))
                    sub.cursor = cursor
                if sub.error is not None:
                    break
                cursor = current.position(current.scanned_through).encode()
                if cursor != sub.cursor:
                    await self.send(writer, self._frame(sub, "progress", cursor=cursor))
                    sub.cursor = cursor
                final = sub.final_page and not current.has_more
                sub.page = None
                sub.query = sub.query.model_copy(update={"cursor": sub.cursor})
                if current.has_more or (self._final and not final):
                    self._wake.set()
                if final:
                    sub.error = control_failure("activity_stream_closed")
        except ControlFault as error:
            sub.error = control_failure(error.code)
        except (OSError, TimeoutError):
            sub.error = control_failure("activity_slow_reader")
            writer.transport.abort()
        finally:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)
            try:
                if sub.error is not None and not writer.is_closing():
                    await self.send(writer, self._frame(sub, "closed", error=sub.error))
            except (ControlFault, OSError, TimeoutError):
                pass
            finally:
                writer.close()
                # Keep the reservation until the owned transport really closes (bounded).
                try:
                    await asyncio.wait_for(writer.wait_closed(), 5)
                except (OSError, TimeoutError):
                    writer.transport.abort()
                self._streams.pop(query.query_id, None)
                sub.done.set()

    async def _pump(self) -> None:
        try:
            while not self._closed:
                self._wake.clear()  # clear BEFORE scan: a concurrent hint remains visible
                for sub in tuple(self._streams.values()):
                    if not self.healthy():
                        sub.error = control_failure("activity_stream_closed")
                        sub.wake.set()
                    if sub.page is not None or sub.error is not None or sub.cursor is None:
                        continue
                    final = self._final
                    try:
                        reply = await self._read(sub.query, time.monotonic() + 30)
                    except ControlFault as error:
                        sub.error = control_failure(error.code)
                        sub.wake.set()
                        continue
                    if sub.done.is_set() or sub.error is not None:
                        continue
                    if reply.error is not None:
                        sub.error = reply.error
                    else:
                        sub.page, sub.final_page = reply.page, final
                    sub.wake.set()
                # A quiet unchanged interval must not busy-spin on writer's completion hint.
                try:
                    await asyncio.wait_for(self._wake.wait(), 1)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            self._healthy = False
            for sub in tuple(self._streams.values()):
                sub.error = control_failure("activity_stream_closed")
                sub.wake.set()

    def begin_stop(self) -> None:
        self._stopping = True

    async def drain(self, deadline: float) -> None:
        self.begin_stop()
        self._final = True
        self._wake.set()
        while self._streams and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        for sub in tuple(self._streams.values()):
            sub.error = control_failure("activity_stream_closed")
            sub.writer.transport.abort()  # captured owned transport only, no process signalling
            sub.wake.set()
        with self._hint_lock:
            self._closed = True
        if self._pump_task is not None:
            self._pump_task.cancel()
            await asyncio.gather(self._pump_task, return_exceptions=True)

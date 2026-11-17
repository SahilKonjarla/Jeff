"""Single-owner native listener lifecycle and finite connection/task admission."""

import asyncio
import os
import socket
import time
from collections.abc import Callable

from jeff.application.dispatch import (
    DispatchReply,
    EvidenceReply,
    EvidenceRequest,
    ExecutionCancelRequest,
    ExecutionRequest,
    FreshExecutionRequest,
    ProjectRequest,
    ReceiptReply,
    ReceiptRequest,
)
from jeff.application.execution import ExecutionCancelReply, ExecutionReply, FreshExecutionReply
from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS, ControlFault, RuntimeIdentityData
from jeff.application.storage import StorageReadiness
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher
from jeff.infrastructure.runtime_events import RuntimeEvents
from jeff.infrastructure.runtime_state import Ownership, _EndpointReceipt

from . import server_project, server_requests


class ControlServer:
    """Authenticate before dispatch and share finite admission with control/stream work."""

    def __init__(
        self,
        owner: Ownership,
        token: bytes,
        identity: RuntimeIdentityData,
        storage_snapshot: Callable[[], StorageReadiness] = StorageReadiness,
        *,
        on_stop: Callable[[], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if len(token) != 32 or identity.pid != os.getpid():
            raise ControlFault("runtime_ownership_unconfirmed")
        self.owner = owner
        self._token = token
        self.identity = identity
        self.storage_snapshot = storage_snapshot
        self._on_stop = on_stop
        self._clock = monotonic
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
        """Verify the kernel peer before secrets, then decode one bounded request."""
        return await server_requests._handle(self, reader, writer)

    async def _cancel(self, request: ExecutionCancelRequest, began: float) -> ExecutionCancelReply:
        return await server_project._cancel(self, request, began)

    def _project_refusal(
        self,
        request: ProjectRequest
        | ReceiptRequest
        | EvidenceRequest
        | ExecutionRequest
        | FreshExecutionRequest,
        *,
        busy: bool = False,
        unknown: bool = False,
    ) -> DispatchReply | ReceiptReply | EvidenceReply | ExecutionReply | FreshExecutionReply:
        return server_project._project_refusal(self, request, busy=busy, unknown=unknown)

    async def _project(
        self,
        request: ProjectRequest
        | ReceiptRequest
        | EvidenceRequest
        | ExecutionRequest
        | FreshExecutionRequest,
        began: float,
    ) -> DispatchReply | ReceiptReply | EvidenceReply | ExecutionReply | FreshExecutionReply:
        return await server_project._project(self, request, began)

    @property
    def stop_deadline(self) -> float:
        if self._stop_deadline is None:
            raise ControlFault("runtime_stop_unconfirmed")
        return self._stop_deadline

    def check_stop_budget(self) -> None:
        if self._clock() >= self.stop_deadline:
            raise ControlFault("runtime_stop_unconfirmed")

    def request_stop(self) -> None:
        """Seal admission once; a lost stop reply cannot reverse accepted shutdown."""
        if self._stop_deadline is None:
            self._stop_deadline = self._clock() + DEFAULT_RUNTIME_LIMITS.stop_seconds
        if not self.stopping and self._on_stop is not None:
            self._on_stop()
        self.stopping = True
        if self.events is not None:
            self.events.begin_stop()
        if self.dispatcher is not None:
            self.dispatcher.begin_drain(self._stop_deadline)
        self._stop.set()

    async def serve(self) -> None:
        """Publish owned endpoint proof before accepting, then drain before retiring it."""
        self.owner.require_no_endpoint()
        endpoint = None
        receipt: _EndpointReceipt | None = None
        server = None
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.owner.store.layout.socket))
            endpoint = self.owner.endpoint_identity()
            receipt = self.owner.publish_endpoint(
                self.identity.instance_id, self._token, endpoint=endpoint
            )
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
                    self.check_stop_budget()
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
                    self.check_stop_budget()
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
                    self._tasks,
                    timeout=min(
                        2 * DEFAULT_RUNTIME_LIMITS.frame_seconds,
                        max(0, self.stop_deadline - self._clock()),
                    ),
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            if endpoint is not None:
                self.owner.remove_endpoint(endpoint, receipt=receipt, token=self._token)

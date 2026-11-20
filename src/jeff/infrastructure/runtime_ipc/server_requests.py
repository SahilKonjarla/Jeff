"""Authentication, bounded request routing and connection cleanup."""

import asyncio
import hmac
import socket
import time
from typing import TYPE_CHECKING, cast

from jeff.application.dispatch import (
    PROJECT_OPERATIONS,
    DispatchReply,
    EvidenceReply,
    EvidenceRequest,
    ExecutionCancelRequest,
    ExecutionRequest,
    FreshExecutionRequest,
    ProjectRequest,
    ReceiptReply,
    ReceiptRequest,
    StreamFrame,
    SubscriptionRequest,
)
from jeff.application.execution import ExecutionReply, FreshExecutionReply
from jeff.application.runtime import (
    DEFAULT_RUNTIME_LIMITS,
    PROTOCOL_REVISION,
    AuthAccepted,
    AuthRequest,
    ControlFault,
    ControlIssueCode,
    ControlObservation,
    ControlResponse,
)
from jeff.domain.runtime import RuntimeState

from .. import runtime_ipc as ipc
from .requests import RpcRequest

if TYPE_CHECKING:
    from .server import ControlServer


async def _handle(
    self: "ControlServer", reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Verify the kernel peer before secrets, then decode one bounded request."""
    project_slot = False
    response: (
        ControlResponse
        | DispatchReply
        | ReceiptReply
        | EvidenceReply
        | ExecutionReply
        | FreshExecutionReply
    )
    try:
        uid, _ = ipc.peer_identity(cast(socket.socket, writer.get_extra_info("socket")))
        if uid != self.owner.store.layout.uid:
            raise ControlFault("runtime_peer_unverified")
        try:
            auth = await ipc._async_receive(
                reader,
                DEFAULT_RUNTIME_LIMITS.auth_bytes,
                DEFAULT_RUNTIME_LIMITS.auth_seconds,
                AuthRequest,
            )
        except ControlFault as fault:
            if fault.code == "runtime_protocol_incompatible":
                await ipc._async_send(
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
            await ipc._async_send(
                writer,
                AuthAccepted(
                    revision=PROTOCOL_REVISION,
                    authenticated=False,
                    error="runtime_authentication_failed",
                ),
                DEFAULT_RUNTIME_LIMITS.auth_bytes,
            )
            return
        await ipc._async_send(
            writer,
            AuthAccepted(revision=PROTOCOL_REVISION, authenticated=True, identity=self.identity),
            DEFAULT_RUNTIME_LIMITS.auth_bytes,
        )
        began = time.monotonic()
        request = (
            await ipc._async_receive(
                reader,
                DEFAULT_RUNTIME_LIMITS.request_bytes,
                DEFAULT_RUNTIME_LIMITS.frame_seconds,
                RpcRequest,
            )
        ).root
        if isinstance(request, ExecutionCancelRequest):
            # The existing reserved control connection is not an ordinary project slot.
            await ipc._async_send(
                writer,
                await self._cancel(request, began),
                DEFAULT_RUNTIME_LIMITS.response_bytes,
            )
            return
        if isinstance(request, SubscriptionRequest):
            if (
                self.stopping
                or self.events is None
                or not self.events.ready
                or self._project_connections >= DEFAULT_RUNTIME_LIMITS.connections - 1
                or request.expected_instance_id != self.identity.instance_id
            ):
                from jeff.application.runtime import control_failure

                await ipc._stream_send(
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
                began + min(request.remaining_ms / 1000, DEFAULT_RUNTIME_LIMITS.request_seconds),
            )
            return
        if isinstance(
            request,
            (
                ProjectRequest,
                ReceiptRequest,
                EvidenceRequest,
                ExecutionRequest,
                FreshExecutionRequest,
            ),
        ):
            if self._project_connections >= DEFAULT_RUNTIME_LIMITS.connections - 1:
                response = self._project_refusal(request, busy=True)
            else:
                project_slot = True
                self._project_connections += 1
                response = await self._project(request, began)
            await ipc._async_send(writer, response, DEFAULT_RUNTIME_LIMITS.response_bytes)
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
                if self.dispatcher is not None and self.dispatcher.cancellation_available:
                    try:
                        while not self.dispatcher.actions_drained:
                            self.check_stop_budget()
                            await asyncio.sleep(0.01)
                        if not self.dispatcher.shutdown_confirmed:
                            raise ControlFault("runtime_stop_unconfirmed")
                    except ControlFault:
                        await ipc._async_send(
                            writer,
                            ControlResponse(
                                revision=PROTOCOL_REVISION,
                                request_id=request.request_id,
                                error="runtime_stop_unconfirmed",
                            ),
                            DEFAULT_RUNTIME_LIMITS.response_bytes,
                        )
                        return
            storage = self.storage_snapshot()
            observation = ControlObservation(
                state=RuntimeState.STOPPING if self.stopping else RuntimeState.RUNNING,
                identity=self.identity,
                control_ready=not self.stopping,
                execution_control_ready=self.dispatcher is not None
                and self.dispatcher.cancellation_available,
                application_ready=(
                    not self.stopping
                    and storage.state == "ready"
                    and self.dispatcher is not None
                    and self.dispatcher.ready
                    and self.events is not None
                    and self.events.ready
                ),
                storage=storage,
                available_operations=(
                    *PROJECT_OPERATIONS,
                    "activity.subscribe",
                    *(
                        ("execution.review",)
                        if self.dispatcher is not None and self.dispatcher.execution is not None
                        else ()
                    ),
                    *(
                        ("execution.v1",)
                        if self.dispatcher is not None and self.dispatcher.fresh is not None
                        else ()
                    ),
                    *(
                        ("execution.cancel.v1",)
                        if self.dispatcher is not None and self.dispatcher.cancellation_available
                        else ()
                    ),
                )
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
        await ipc._async_send(writer, response, DEFAULT_RUNTIME_LIMITS.response_bytes)
    except (ControlFault, OSError, ValueError, asyncio.IncompleteReadError, TimeoutError):
        pass  # Never log/echo auth, raw frames, parser or OS details.
    finally:
        if project_slot:
            self._project_connections -= 1
        writer.close()
        try:
            await asyncio.wait_for(
                writer.wait_closed(),
                timeout=min(
                    DEFAULT_RUNTIME_LIMITS.frame_seconds,
                    max(0, self.stop_deadline - self._clock()),
                )
                if self.stopping
                else DEFAULT_RUNTIME_LIMITS.frame_seconds,
            )
        except (OSError, TimeoutError):
            pass

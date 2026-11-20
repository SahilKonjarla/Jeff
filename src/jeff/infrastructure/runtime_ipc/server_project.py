"""Instance-bound project/cancellation routing through existing worker ports."""

import asyncio
import time
from typing import TYPE_CHECKING

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
    dispatch_failure,
)
from jeff.application.execution import (
    DockerBackendFault,
    ExecutionCancelReply,
    ExecutionReply,
    ExecutionStopView,
    FreshExecutionReply,
    ReviewFault,
    fresh_execution_failure,
)
from jeff.application.projects import Failure
from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS, ControlFault, control_failure
from jeff.application.storage import StorageFault

if TYPE_CHECKING:
    from .server import ControlServer


async def _cancel(
    self: "ControlServer", request: ExecutionCancelRequest, began: float
) -> ExecutionCancelReply:
    call = request.call

    def reply(
        error: Failure | None = None, stop: ExecutionStopView | None = None
    ) -> ExecutionCancelReply:
        return ExecutionCancelReply(
            request_id=call.request_id,
            approval_id=call.approval_id,
            reviewed_manifest_digest=call.reviewed_manifest_digest,
            identity=self.identity.instance_id,
            ok=error is None,
            error=error,
            stop=stop,
        )

    if request.expected_instance_id != self.identity.instance_id:
        return reply(control_failure("runtime_ownership_unconfirmed"))
    if self.dispatcher is None or self.storage_snapshot().state != "ready":
        return reply(fresh_execution_failure("execution_unavailable"))
    deadline = began + min(request.remaining_ms / 1000, DEFAULT_RUNTIME_LIMITS.request_seconds)
    try:
        future = self.dispatcher.submit_cancel(call, deadline)
        record = await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(future)),
            timeout=max(0, deadline - time.monotonic()),
        )
        return reply(stop=ExecutionStopView.from_record(record))
    except TimeoutError:
        return reply(dispatch_failure("request_outcome_unknown"))
    except (ControlFault, DockerBackendFault, ReviewFault, StorageFault) as error:
        return reply(fresh_execution_failure(error.code))


def _project_refusal(
    self: "ControlServer",
    request: ProjectRequest
    | ReceiptRequest
    | EvidenceRequest
    | ExecutionRequest
    | FreshExecutionRequest,
    *,
    busy: bool = False,
    unknown: bool = False,
) -> DispatchReply | ReceiptReply | EvidenceReply | ExecutionReply | FreshExecutionReply:
    error = dispatch_failure(
        "request_outcome_unknown" if unknown else "runtime_busy" if busy else "runtime_stopping"
    )
    if isinstance(request, FreshExecutionRequest):
        return FreshExecutionReply(
            ok=False,
            request_id=request.call.request_id,
            operation=request.call.operation,
            identity=self.identity.instance_id,
            error=error,
        )
    if isinstance(request, ExecutionRequest):
        return ExecutionReply(
            request_id=request.call.request_id, identity=self.identity.instance_id, result=error
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
    self: "ControlServer",
    request: ProjectRequest
    | ReceiptRequest
    | EvidenceRequest
    | ExecutionRequest
    | FreshExecutionRequest,
    began: float,
) -> DispatchReply | ReceiptReply | EvidenceReply | ExecutionReply | FreshExecutionReply:
    if request.expected_instance_id != self.identity.instance_id:
        error = self._project_refusal(request)
        failure = dispatch_failure("project_target_mismatch")
        return (
            error.model_copy(update={"result": failure})
            if isinstance(error, (DispatchReply, ExecutionReply))
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
        work = (
            request.call
            if isinstance(request, (ProjectRequest, ExecutionRequest, FreshExecutionRequest))
            else request.query
        )
        future = self.dispatcher.submit(work, deadline)
    except ControlFault as error:
        if (
            isinstance(request, FreshExecutionRequest)
            and error.code == "runtime_capability_unavailable"
        ):
            return FreshExecutionReply(
                ok=False,
                request_id=request.call.request_id,
                operation=request.call.operation,
                identity=self.identity.instance_id,
                error=self.dispatcher.fresh_issue or fresh_execution_failure(error.code),
            )
        return self._project_refusal(request, busy=True)
    try:
        reply = await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(future)),
            timeout=max(0, deadline - time.monotonic()),
        )
        expected = (
            FreshExecutionReply
            if isinstance(request, FreshExecutionRequest)
            else ExecutionReply
            if isinstance(request, ExecutionRequest)
            else DispatchReply
            if isinstance(request, ProjectRequest)
            else EvidenceReply
            if isinstance(request, EvidenceRequest)
            else ReceiptReply
        )
        if not isinstance(reply, expected):
            # Fresh replies must never leak into the permanently review-only route.
            raise ControlFault("runtime_protocol_error")
        return reply
    except TimeoutError:
        return self._project_refusal(request, unknown=True)

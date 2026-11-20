"""Explicit authenticated application RPCs without replay or local fallback."""

import time
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel

from jeff.application.dispatch import (
    ActivityFault,
    DispatchReply,
    EvidenceQuery,
    EvidenceReply,
    EvidenceRequest,
    ExecutionCancelRequest,
    ExecutionRequest,
    FreshExecutionRequest,
    ProjectCall,
    ProjectRequest,
    ProjectStatusService,
    ProjectStatusSuccess,
    ReceiptQuery,
    ReceiptReply,
    ReceiptRequest,
    StreamFrame,
    SubscriptionQuery,
    SubscriptionRequest,
    dispatch_failure,
)
from jeff.application.execution import (
    ExecutionCall,
    ExecutionCancelCall,
    ExecutionCancelReply,
    ExecutionReply,
    FreshExecutionCall,
    FreshExecutionReply,
    execution_failure,
    fresh_execution_failure,
)
from jeff.application.execution.pytest_report import pytest_retry_token, pytest_run_token
from jeff.application.projects import Failure
from jeff.application.runtime import (
    DEFAULT_RUNTIME_LIMITS,
    ControlFault,
    RuntimeLayout,
    control_failure,
)
from jeff.infrastructure.runtime_state import RuntimeStateStore

from .. import runtime_ipc as ipc
from .activity import OwnedActivityStream
from .connection import ControlClient, ControlConnection


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
        connection.connection.settimeout(ipc._remaining(connection.deadline))
        connection.connection.sendall(
            ipc.encode_message(
                request.model_dump(mode="json"), DEFAULT_RUNTIME_LIMITS.request_bytes
            )
        )
        return ipc.receive_message(
            connection.connection, DEFAULT_RUNTIME_LIMITS.response_bytes, connection.deadline, model
        )

    def subscribe(self, query: SubscriptionQuery) -> OwnedActivityStream:
        """Bind capability preflight and ready frame to the same authenticated instance."""
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
                remaining_ms=max(1, int(ipc._remaining(deadline) * 1000)),
            )
            connection.used = True
            connection.connection.settimeout(ipc._remaining(deadline))
            connection.connection.sendall(
                ipc.encode_message(
                    request.model_dump(mode="json"), DEFAULT_RUNTIME_LIMITS.request_bytes
                )
            )
            ready = ipc.receive_message(
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

    def cancel(self, call: ExecutionCancelCall) -> ExecutionCancelReply:
        """One authenticated control exchange; no cwd discovery, fallback or retry."""
        deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.request_seconds

        def refused(error: Failure, identity: UUID | None = None) -> ExecutionCancelReply:
            return ExecutionCancelReply(
                request_id=call.request_id,
                approval_id=call.approval_id,
                reviewed_manifest_digest=call.reviewed_manifest_digest,
                identity=identity,
                ok=False,
                error=error,
            )

        try:
            ipc.encode_message(
                ExecutionCancelRequest(call=call, expected_instance_id=UUID(int=0)).model_dump(
                    mode="json"
                ),
                DEFAULT_RUNTIME_LIMITS.request_bytes,
            )
        except ControlFault:
            return refused(fresh_execution_failure("execution_unconfirmed"))
        try:
            with self._connect(deadline) as connection:
                try:
                    reply = self._exchange(
                        connection,
                        ExecutionCancelRequest(
                            call=call,
                            expected_instance_id=connection.identity.instance_id,
                            remaining_ms=max(1, int(ipc._remaining(deadline) * 1000)),
                        ),
                        ExecutionCancelReply,
                    )
                    if (
                        reply.request_id != call.request_id
                        or reply.approval_id != call.approval_id
                        or reply.reviewed_manifest_digest != call.reviewed_manifest_digest
                        or reply.identity != connection.identity.instance_id
                    ):
                        raise ControlFault("runtime_ownership_unconfirmed")
                    return reply
                except (ControlFault, OSError):
                    return refused(
                        dispatch_failure("request_outcome_unknown"), connection.identity.instance_id
                    )
        except ControlFault as error:
            return refused(control_failure(error.code))

    def fresh(self, call: FreshExecutionCall) -> FreshExecutionReply:
        deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.request_seconds

        def refused(error: Failure, identity: UUID | None = None) -> FreshExecutionReply:
            return FreshExecutionReply(
                ok=False,
                request_id=call.request_id,
                operation=call.operation,
                identity=identity,
                error=error,
            )

        try:
            ipc.encode_message(
                FreshExecutionRequest(call=call, expected_instance_id=UUID(int=0)).model_dump(
                    mode="json"
                ),
                DEFAULT_RUNTIME_LIMITS.request_bytes,
            )
        except ControlFault:
            return refused(fresh_execution_failure("execution_unconfirmed"))
        try:
            with self._connect(deadline) as connection:
                try:
                    request = FreshExecutionRequest(
                        call=call,
                        expected_instance_id=connection.identity.instance_id,
                        remaining_ms=max(1, int(ipc._remaining(deadline) * 1000)),
                    )
                    reply = self._exchange(connection, request, FreshExecutionReply)
                    if (
                        reply.request_id != call.request_id
                        or reply.identity != connection.identity.instance_id
                        or reply.operation != call.operation
                    ):
                        raise ControlFault("runtime_ownership_unconfirmed")
                    if reply.page is not None and not (
                        reply.page.within(after=call.after, limit=call.limit)
                        and reply.page.matches(call.claim_filter, call.kind_filter)
                    ):
                        raise ControlFault("runtime_ownership_unconfirmed")
                    if (
                        reply.log_page is not None
                        and call.log_after is not None
                        and (
                            reply.log_page.offset < call.log_after
                            or (
                                reply.log_page.offset > call.log_after
                                and not reply.log_page.dropped
                            )
                        )
                    ):
                        raise ControlFault("runtime_ownership_unconfirmed")
                    if reply.admission is not None and (
                        reply.admission.approval_id != call.approval_id
                        or reply.admission.manifest_digest != call.reviewed_manifest_digest
                    ):
                        raise ControlFault("runtime_ownership_unconfirmed")
                    if reply.view is not None:
                        if (
                            call.approval_id is not None
                            and reply.view.approval_id != call.approval_id
                        ) or (
                            call.operation in {"tests.results", "jobs.logs"}
                            and reply.view.job_id != call.job_id
                        ):
                            raise ControlFault("runtime_ownership_unconfirmed")
                        if (
                            call.reviewed_manifest_digest is not None
                            and reply.view.manifest_digest != call.reviewed_manifest_digest
                        ):
                            raise ControlFault("runtime_ownership_unconfirmed")
                        if call.operation == "tests.retry-failed" and (
                            call.job_id is None
                            or reply.view.job_id == call.job_id
                            or pytest_run_token(reply.view.manifest.argv)
                            != pytest_retry_token(call.request_id, call.job_id)
                        ):
                            raise ControlFault("runtime_ownership_unconfirmed")
                        if call.operation == "propose" and (
                            reply.view.manifest.profile.name != call.profile_name
                            or reply.view.manifest.argv != call.argv
                            or tuple(item.path for item in reply.view.manifest.inputs.files)
                            != tuple(sorted(call.paths))
                        ):
                            raise ControlFault("runtime_ownership_unconfirmed")
                    return reply
                except (OSError, ControlFault):
                    return refused(
                        dispatch_failure("request_outcome_unknown"), connection.identity.instance_id
                    )
        except ControlFault as error:
            return refused(control_failure(error.code))

    def review(self, call: ExecutionCall) -> ExecutionReply:
        deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.request_seconds
        try:
            # Enforce complete-envelope size before location/credential discovery.
            ipc.encode_message(
                ExecutionRequest(call=call, expected_instance_id=UUID(int=0)).model_dump(
                    mode="json"
                ),
                DEFAULT_RUNTIME_LIMITS.request_bytes,
            )
        except ControlFault:
            return ExecutionReply(
                request_id=call.request_id, result=execution_failure("execution_invalid")
            )
        try:
            with self._connect(deadline) as connection:
                try:
                    request = ExecutionRequest(
                        call=call,
                        expected_instance_id=connection.identity.instance_id,
                        remaining_ms=max(1, int(ipc._remaining(deadline) * 1000)),
                    )
                    reply = self._exchange(connection, request, ExecutionReply)
                    if (
                        reply.request_id != call.request_id
                        or reply.identity != connection.identity.instance_id
                    ):
                        raise ControlFault("runtime_ownership_unconfirmed")
                    return reply
                except (OSError, ControlFault):
                    return ExecutionReply(
                        request_id=call.request_id,
                        identity=connection.identity.instance_id,
                        result=dispatch_failure("request_outcome_unknown"),
                    )
        except ControlFault as error:
            return ExecutionReply(request_id=call.request_id, result=control_failure(error.code))

    def execute(self, call: ProjectCall) -> DispatchReply:
        """Exchange once; loss after connection leaves outcome unknown, never retried."""
        deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.request_seconds
        with self._connect(deadline) as connection:
            try:
                request = ProjectRequest(
                    call=call,
                    expected_instance_id=connection.identity.instance_id,
                    remaining_ms=max(1, int(ipc._remaining(deadline) * 1000)),
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
                    remaining_ms=max(1, int(ipc._remaining(deadline) * 1000)),
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
                    remaining_ms=max(1, int(ipc._remaining(deadline) * 1000)),
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

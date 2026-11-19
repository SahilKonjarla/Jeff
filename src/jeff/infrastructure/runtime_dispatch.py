"""Finite owned workers with distinct admission replies and actual action drainage."""

import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass

from jeff.application.dispatch import (
    DispatchReply,
    EvidenceQuery,
    EvidenceReply,
    ProjectCall,
    ProjectDispatcher,
    ReceiptQuery,
    ReceiptReply,
    StreamQuery,
    StreamReply,
    dispatch_failure,
)
from jeff.application.execution import (
    AuthorityClaimRefused,
    ExecutionAdmission,
    ExecutionCall,
    ExecutionCancelCall,
    ExecutionReply,
    ExecutionService,
    ExecutionStartCall,
    ExecutionStarter,
    ExecutionStartRefused,
    ExecutionStartReply,
    ExecutionStopRecord,
    FreshExecutionCall,
    FreshExecutionControl,
    FreshExecutionReply,
    FreshExecutionService,
    fresh_execution_failure,
)
from jeff.application.projects import Failure
from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS, ControlFault

Call = (
    ProjectCall
    | ReceiptQuery
    | EvidenceQuery
    | StreamQuery
    | ExecutionCall
    | ExecutionStartCall
    | FreshExecutionCall
)
Reply = (
    DispatchReply
    | ReceiptReply
    | EvidenceReply
    | StreamReply
    | ExecutionReply
    | ExecutionStartReply
    | FreshExecutionReply
)


def _is_start(call: Call) -> bool:
    return (
        isinstance(call, ExecutionStartCall)
        or isinstance(call, FreshExecutionCall)
        and call.operation == "start"
    )


@dataclass
class _Work:
    call: Call
    deadline: float
    result: Future[Reply]


class RuntimeDispatcher:
    """Keep blocking use cases off the control loop with finite, owned worker lifetimes."""

    def __init__(
        self,
        dispatch: ProjectDispatcher,
        execution: ExecutionService | None = None,
        *,
        starter: ExecutionStarter | None = None,
        fresh: FreshExecutionService | None = None,
        fresh_factory: Callable[[], FreshExecutionService | Failure] | None = None,
    ) -> None:
        self.dispatch = dispatch
        self.execution = execution
        self.starter = starter
        if fresh is not None and fresh_factory is not None:
            raise ValueError("Supply a fresh service or a startup factory, not both.")
        self.fresh = fresh
        self.fresh_issue: Failure | None = None
        self._fresh_factory = fresh_factory
        self.initialization: Future[None] = Future()
        # Cancellation of an observer must not make live initialization look drained.
        self.initialization.set_running_or_notify_cancel()
        if fresh_factory is None:
            self.initialization.set_result(None)
        self._execution_reserved = False
        self._queue: queue.Queue[_Work] = queue.Queue(DEFAULT_RUNTIME_LIMITS.pending_requests)
        self._closing = threading.Event()
        self._draining = False
        self._outstanding_actions = 0
        self._mutex = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._ready = False

    @property
    def ready(self) -> bool:
        with self._mutex:
            return (
                self._ready
                and self.initialization.done()
                and self.initialization.exception() is None
                and not self._draining
                and not self._closing.is_set()
                and len(self._threads) == DEFAULT_RUNTIME_LIMITS.active_requests
                and all(thread.is_alive() for thread in self._threads)
            )

    @property
    def actions_drained(self) -> bool:
        with self._mutex:
            return self._outstanding_actions == 0 and self.initialization.done()

    @property
    def closed(self) -> bool:
        return all(not thread.is_alive() for thread in self._threads)

    def start(self) -> None:
        with self._mutex:
            if self._threads or self._closing.is_set():
                raise ControlFault("runtime_ownership_unconfirmed")
            try:
                for number in range(DEFAULT_RUNTIME_LIMITS.active_requests):
                    thread = threading.Thread(
                        target=self._run,
                        kwargs={"initialize": number == 0},
                        name=f"jeff-project-{number}",
                        daemon=False,
                    )
                    thread.start()
                    self._threads.append(thread)
                self._ready = True
            except Exception:
                self._closing.set()
                if not self._threads and not self.initialization.done():
                    self.initialization.set_exception(ControlFault("runtime_start_failed"))
                raise ControlFault("runtime_start_failed") from None

    def submit(self, call: Call, deadline: float) -> Future[Reply]:
        """Queue work without implying durable acceptance or cancelling running work."""
        result: Future[Reply] = Future()
        with self._mutex:
            if (
                not self._ready
                or not self.initialization.done()
                or self.initialization.exception() is not None
                or self._closing.is_set()
                or (self._draining and not isinstance(call, StreamQuery))
            ):
                raise ControlFault("runtime_busy")
            if isinstance(call, FreshExecutionCall) and self.fresh is None:
                raise ControlFault("runtime_capability_unavailable")
            if _is_start(call):
                if isinstance(call, ExecutionStartCall) and self.starter is None:
                    raise ControlFault("runtime_capability_unavailable")
                if self._execution_reserved:
                    raise ControlFault("runtime_busy")
            try:
                self._queue.put_nowait(_Work(call, deadline, result))
            except queue.Full:
                raise ControlFault("runtime_busy") from None
            if _is_start(call):
                self._execution_reserved = True
            if not isinstance(call, StreamQuery):
                self._outstanding_actions += 1
        return result

    @property
    def shutdown_confirmed(self) -> bool:
        with self._mutex:
            fresh = self.fresh
        return not isinstance(fresh, FreshExecutionControl) or fresh.shutdown_confirmed

    @property
    def cancellation_available(self) -> bool:
        with self._mutex:
            fresh = self.fresh
        return isinstance(fresh, FreshExecutionControl) and fresh.cancellation_available

    def submit_cancel(
        self, call: ExecutionCancelCall, deadline: float
    ) -> Future[ExecutionStopRecord]:
        """Direct control submission: no ordinary worker reservation or execution mutex."""
        with self._mutex:
            fresh = self.fresh
            closing = self._closing.is_set()
        if closing:
            raise ControlFault("runtime_busy")
        if not isinstance(fresh, FreshExecutionControl) or not fresh.cancellation_available:
            raise ControlFault("runtime_capability_unavailable")
        return fresh.submit_cancel(call, deadline=deadline)

    def begin_drain(self, deadline: float | None = None) -> None:
        """Seal admission first, then signal the same live owner without waiting for I/O."""
        with self._mutex:
            self._draining = True
            fresh = self.fresh
        if deadline is not None and isinstance(fresh, FreshExecutionControl):
            fresh.begin_shutdown(deadline)

    def request_close(self) -> None:
        """Seal admission; queued refusals and active workers still require real drain."""
        with self._mutex:
            self._draining = True
            self._closing.set()
            self._ready = False
            if not self._threads and not self.initialization.done():
                self.initialization.set_exception(ControlFault("runtime_start_failed"))

    def close(self, timeout: float = DEFAULT_RUNTIME_LIMITS.stop_seconds) -> None:
        """Join owned threads within one budget, reporting unconfirmed exit on timeout."""
        self.request_close()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
        if not self.closed:
            raise ControlFault("runtime_stop_unconfirmed")

    def _refused(self, call: Call, *, closing: bool = False, unknown: bool = False) -> Reply:
        error = dispatch_failure(
            "request_outcome_unknown"
            if unknown
            else "runtime_stopping"
            if closing
            else "request_timeout"
        )
        if isinstance(call, FreshExecutionCall):
            return FreshExecutionReply(
                ok=False,
                request_id=call.request_id,
                operation=call.operation,
                identity=self.dispatch.identity.instance_id,
                error=error,
            )
        if isinstance(call, ExecutionStartCall):
            return ExecutionStartReply(
                request_id=call.request_id, identity=self.dispatch.identity.instance_id, error=error
            )
        if isinstance(call, ExecutionCall):
            return ExecutionReply(
                request_id=call.request_id,
                identity=self.dispatch.identity.instance_id,
                result=error,
            )
        if isinstance(call, StreamQuery):
            return StreamReply(error=error)
        if isinstance(call, ProjectCall):
            return DispatchReply(
                request_id=call.request_id,
                identity=self.dispatch.identity,
                result=error,
                accepted=None if unknown else False,
            )
        if isinstance(call, EvidenceQuery):
            return EvidenceReply(
                request_id=call.query_id,
                identity=self.dispatch.identity,
                operation=call.operation,
                error=error,
            )
        return ReceiptReply(request_id=call.query_id, identity=self.dispatch.identity, error=error)

    def _start_execution(self, work: _Work) -> None:
        call = work.call
        if not isinstance(call, (ExecutionStartCall, FreshExecutionCall)):
            raise ControlFault("runtime_capability_unavailable")
        service = self.fresh if isinstance(call, FreshExecutionCall) else self.starter
        if service is None or call.approval_id is None or call.reviewed_manifest_digest is None:
            raise ControlFault("runtime_capability_unavailable")

        def admitted(observation: ExecutionAdmission) -> None:
            if not work.result.done():
                try:
                    reply: Reply = (
                        FreshExecutionReply(
                            ok=True,
                            operation=call.operation,
                            request_id=call.request_id,
                            identity=self.dispatch.identity.instance_id,
                            admission=observation,
                        )
                        if isinstance(call, FreshExecutionCall)
                        else ExecutionStartReply(
                            request_id=call.request_id,
                            identity=self.dispatch.identity.instance_id,
                            admission=observation,
                        )
                    )
                    work.result.set_result(reply)
                except InvalidStateError:
                    pass  # Lost observer does not cancel the worker owning the claim.

        try:
            service.start(
                call.directory,
                call.approval_id,
                call.reviewed_manifest_digest,
                request_id=call.request_id,
                admission_deadline=work.deadline,
                on_admitted=admitted,
            )
        except (ExecutionStartRefused, AuthorityClaimRefused) as error:
            if not isinstance(call, FreshExecutionCall):
                raise  # Preserve the legacy Start reply contract.
            if not work.result.done():
                work.result.set_result(
                    FreshExecutionReply(
                        ok=False,
                        operation=call.operation,
                        request_id=call.request_id,
                        identity=self.dispatch.identity.instance_id,
                        error=fresh_execution_failure(error.code),
                    )
                )
        if not work.result.done():
            work.result.set_result(self._refused(call, unknown=True))

    def _initialize_backend(self) -> None:
        if self._fresh_factory is None:
            return
        try:
            if not self._closing.is_set():
                observed = (
                    self._fresh_factory()
                )  # Same owned worker; no loop blocking/new executor.
                with self._mutex:
                    if not self._draining and not self._closing.is_set():
                        if isinstance(observed, Failure):
                            self.fresh_issue = observed
                        else:
                            self.fresh = observed
        except Exception as error:
            self._closing.set()
            self.initialization.set_exception(error)
        else:
            self.initialization.set_result(None)

    def _run(self, *, initialize: bool = False) -> None:
        if initialize:
            self._initialize_backend()
        while not self._closing.is_set() or not self._queue.empty():
            try:
                work = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                if not work.result.set_running_or_notify_cancel():
                    continue
                if (
                    self._closing.is_set()
                    or (self._draining and not isinstance(work.call, StreamQuery))
                    or time.monotonic() >= work.deadline
                ):
                    result = self._refused(work.call, closing=self._draining)
                elif isinstance(work.call, ExecutionStartCall) or (
                    isinstance(work.call, FreshExecutionCall) and work.call.operation == "start"
                ):
                    # The same existing worker remains counted through completion;
                    # only its observation future may finish at durable admission.
                    self._start_execution(work)
                    continue
                elif isinstance(work.call, FreshExecutionCall):
                    result = (
                        self.fresh.execute(work.call, work.deadline)
                        if self.fresh is not None
                        else self._refused(work.call)
                    )
                elif isinstance(work.call, ExecutionCall):
                    result = (
                        self.execution.execute(work.call, work.deadline)
                        if self.execution is not None
                        else self._refused(work.call)
                    )
                elif isinstance(work.call, StreamQuery):
                    result = self.dispatch.read_stream(work.call, work.deadline)
                elif isinstance(work.call, ProjectCall):
                    result = self.dispatch.execute(work.call, work.deadline)
                elif isinstance(work.call, EvidenceQuery):
                    result = self.dispatch.inspect_evidence(work.call, work.deadline)
                else:
                    result = self.dispatch.inspect_receipt(work.call, work.deadline)
                work.result.set_result(result)
            except Exception:
                if not work.result.done():
                    work.result.set_result(self._refused(work.call, unknown=True))
            finally:
                with self._mutex:
                    if _is_start(work.call):
                        self._execution_reserved = False
                    if not isinstance(work.call, StreamQuery):
                        self._outstanding_actions -= 1
                self._queue.task_done()

"""Finite worker admission and staged actual drain; no SQL/transport/lifetime executor."""

import queue
import threading
import time
from concurrent.futures import Future
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
from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS, ControlFault

Call = ProjectCall | ReceiptQuery | EvidenceQuery | StreamQuery
Reply = DispatchReply | ReceiptReply | EvidenceReply | StreamReply


@dataclass
class _Work:
    call: Call
    deadline: float
    result: Future[Reply]


class RuntimeDispatcher:
    def __init__(self, dispatch: ProjectDispatcher) -> None:
        self.dispatch = dispatch
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
                and not self._draining
                and not self._closing.is_set()
                and len(self._threads) == DEFAULT_RUNTIME_LIMITS.active_requests
                and all(thread.is_alive() for thread in self._threads)
            )

    @property
    def actions_drained(self) -> bool:
        with self._mutex:
            return self._outstanding_actions == 0

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
                        target=self._run, name=f"jeff-project-{number}", daemon=False
                    )
                    thread.start()
                    self._threads.append(thread)
                self._ready = True
            except Exception:
                self._closing.set()
                raise ControlFault("runtime_start_failed") from None

    def submit(self, call: Call, deadline: float) -> Future[Reply]:
        result: Future[Reply] = Future()
        with self._mutex:
            if (
                not self._ready
                or self._closing.is_set()
                or (self._draining and not isinstance(call, StreamQuery))
            ):
                raise ControlFault("runtime_busy")
            try:
                self._queue.put_nowait(_Work(call, deadline, result))
            except queue.Full:
                raise ControlFault("runtime_busy") from None
            if not isinstance(call, StreamQuery):
                self._outstanding_actions += 1
        return result

    def begin_drain(self) -> None:
        """Seal external work, retain the same workers for admitted stream final reads."""
        with self._mutex:
            self._draining = True

    def request_close(self) -> None:
        with self._mutex:
            self._draining = True
            self._closing.set()
            self._ready = False

    def close(self, timeout: float = DEFAULT_RUNTIME_LIMITS.stop_seconds) -> None:
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

    def _run(self) -> None:
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
                    if not isinstance(work.call, StreamQuery):
                        self._outstanding_actions -= 1
                self._queue.task_done()

"""Finite queue admission and caller deadlines without abandoning running SQL."""

import math
import queue
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from jeff.application.execution import (
    AuthorityOperation,
    DockerCompletion,
    DockerLaunchEvent,
    DockerLaunchScope,
    ExecutionCancelCall,
    ExecutionInspectionScope,
    ExecutionStopCapture,
    ExecutionStopObservation,
    ExecutionStopRequest,
    ProposalRequestScope,
    ReviewOperation,
)
from jeff.application.history import ActivityRecord, HistoryScope, ReplayScope
from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS
from jeff.application.storage import (
    STORAGE_PENDING_LIMIT,
    ReceiptClaim,
    ReceiptScope,
    ReceiptTransition,
    StorageFault,
)
from jeff.domain.projects import Project

from .binding import _method
from .work import _Work

if TYPE_CHECKING:
    from .store import SQLiteStorage


@_method
def _submit(
    self: "SQLiteStorage",
    operation: Literal[
        "index",
        "lookup",
        "claim",
        "finish",
        "inspect",
        "root_claimed",
        "observe",
        "trace",
        "replay",
        "debug",
        "history",
        "stream",
        "retire",
        "review",
        "authority",
        "proposal_lookup",
        "execution_inspect",
        "launch_record",
        "launch_status",
        "execution_complete",
        "execution_result",
        "stop_request",
        "stop_inspect",
        "stop_finish",
        "cancel",
    ],
    payload: Project
    | ReceiptClaim
    | ReceiptTransition
    | ReceiptScope
    | Path
    | ActivityRecord
    | ReplayScope
    | HistoryScope
    | ReviewOperation
    | AuthorityOperation
    | ProposalRequestScope
    | ExecutionInspectionScope
    | DockerLaunchEvent
    | DockerLaunchScope
    | DockerCompletion
    | ExecutionStopRequest
    | ExecutionStopObservation
    | ExecutionStopCapture
    | ExecutionCancelCall
    | None,
    deadline: float | None,
) -> object:
    """Bound admission and caller waiting without abandoning already running SQL."""
    if isinstance(payload, Project) and (
        not isinstance(payload.project_id, UUID)
        or not payload.root.is_absolute()
        or "\0" in str(payload.root)
    ):
        raise StorageFault("registry_conflict")
    deadline = min(
        deadline if deadline is not None else float("inf"),
        time.monotonic() + DEFAULT_RUNTIME_LIMITS.request_seconds,
    )
    if deadline <= time.monotonic():
        raise StorageFault("storage_busy")
    result: Future[object] = Future()
    self._enqueue(
        _Work(operation, payload, deadline, result), control=operation.startswith("stop_")
    )
    try:
        return result.result(timeout=max(0, deadline - time.monotonic()))
    except FutureTimeout:
        # Only queued work can be cancelled safely; running SQL keeps its slot.
        raise StorageFault(
            "storage_busy" if result.cancel() else "storage_outcome_unconfirmed"
        ) from None


@_method
def _enqueue(self: "SQLiteStorage", work: _Work, *, control: bool = False) -> Future[object]:
    if not math.isfinite(work.deadline) or work.deadline <= time.monotonic():
        raise StorageFault("storage_busy")
    with self._mutex:
        if not self._accepting:
            raise StorageFault("storage_closing")
        # One reserved control position WITHIN the existing finite pending bound.
        if not control and self._queue.qsize() >= STORAGE_PENDING_LIMIT - 1:
            raise StorageFault("storage_busy")
        try:
            self._queue.put_nowait(work)
        except queue.Full:
            raise StorageFault("storage_busy") from None
    return work.result

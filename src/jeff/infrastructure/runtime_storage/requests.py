"""Public registry, evidence and execution submissions on the original store instance."""

import time
from concurrent.futures import Future
from pathlib import Path
from typing import TYPE_CHECKING, cast

from jeff.application.execution import (
    AuthorityOperation,
    AuthorityRecord,
    AuthorityResult,
    DockerBackendFault,
    DockerCompletion,
    DockerExecutionResult,
    DockerLaunchEvent,
    DockerLaunchScope,
    DockerLaunchSnapshot,
    DockerOutputSnapshot,
    ExecutionCancelCall,
    ExecutionData,
    ExecutionInspection,
    ExecutionInspectionScope,
    ExecutionStopCapture,
    ExecutionStopObservation,
    ExecutionStopRecord,
    ExecutionStopRequest,
    ProposalRequestScope,
    ReviewFault,
    ReviewOperation,
)
from jeff.application.history import (
    ActivityEvent,
    ActivityPage,
    ActivityRecord,
    ActivityTrace,
    HistoryPage,
    HistoryRead,
    HistoryScope,
    ReplayScope,
    StreamPage,
    StreamRead,
)
from jeff.application.storage import (
    Receipt,
    ReceiptClaim,
    ReceiptClaimResult,
    ReceiptScope,
    ReceiptTransition,
    RetirementBatch,
    StorageFault,
)
from jeff.domain.projects import Project

from .binding import _method
from .work import _Work

if TYPE_CHECKING:
    from .store import SQLiteStorage


@_method
def index(
    self: "SQLiteStorage", project: Project, *, deadline: float | None = None
) -> tuple[Project, bool]:
    return project, cast(bool, self._submit("index", project, deadline))


@_method
def lookup(
    self: "SQLiteStorage", project: Project, *, deadline: float | None = None
) -> Project | None:
    return project if self._submit("lookup", project, deadline) else None


@_method
def root_claimed(self: "SQLiteStorage", root: Path, *, deadline: float | None = None) -> bool:
    if not root.is_absolute() or "\0" in str(root):
        raise StorageFault("registry_conflict")
    return cast(bool, self._submit("root_claimed", root, deadline))


@_method
def claim(self: "SQLiteStorage", claim: ReceiptClaim, *, deadline: float) -> ReceiptClaimResult:
    return cast(ReceiptClaimResult, self._submit("claim", claim, deadline))


@_method
def finish(self: "SQLiteStorage", transition: ReceiptTransition, *, deadline: float) -> Receipt:
    return cast(Receipt, self._submit("finish", transition, deadline))


@_method
def inspect(self: "SQLiteStorage", scope: ReceiptScope, *, deadline: float) -> Receipt | None:
    return cast(Receipt | None, self._submit("inspect", scope, deadline))


@_method
def observe(self: "SQLiteStorage", record: ActivityRecord, *, deadline: float) -> ActivityEvent:
    return cast(ActivityEvent, self._submit("observe", record, deadline))


@_method
def trace(
    self: "SQLiteStorage", scope: ReceiptScope, *, deadline: float
) -> tuple[Receipt | None, ActivityTrace | None]:
    return cast(tuple[Receipt | None, ActivityTrace | None], self._submit("trace", scope, deadline))


@_method
def replay(self: "SQLiteStorage", scope: ReplayScope, *, deadline: float) -> ActivityPage:
    return cast(ActivityPage, self._submit("replay", scope, deadline))


@_method
def debug(
    self: "SQLiteStorage", scope: HistoryScope, *, deadline: float
) -> tuple[Receipt, ActivityTrace]:
    return cast(tuple[Receipt, ActivityTrace], self._submit("debug", scope, deadline))


@_method
def history(self: "SQLiteStorage", scope: HistoryRead, *, deadline: float) -> HistoryPage:
    return cast(HistoryPage, self._submit("history", scope, deadline))


@_method
def stream_page(self: "SQLiteStorage", scope: StreamRead, *, deadline: float) -> StreamPage:
    return cast(StreamPage, self._submit("stream", scope, deadline))


@_method
def retire(self: "SQLiteStorage", *, deadline: float) -> RetirementBatch:
    """Installation-internal bounded maintenance; no cutoff/SQL or wire operation."""
    return cast(RetirementBatch, self._submit("retire", None, deadline))


@_method
def review(self: "SQLiteStorage", operation: ReviewOperation, *, deadline: float) -> ExecutionData:
    try:
        return cast(ExecutionData, self._submit("review", operation, deadline))
    except StorageFault as error:
        raise ReviewFault(error.code) from None


@_method
def authority(
    self: "SQLiteStorage", operation: AuthorityOperation, *, deadline: float
) -> AuthorityResult:
    """Internal verified-worker seam only; deliberately absent from RPC/CLI routing."""
    try:
        return cast(AuthorityResult, self._submit("authority", operation, deadline))
    except StorageFault as error:
        raise ReviewFault(error.code) from None


@_method
def submit_cancel(
    self: "SQLiteStorage", call: ExecutionCancelCall, *, deadline: float
) -> Future[ExecutionStopRecord]:
    """Nonblocking actor submission; only a completed Future confirms durable intent."""
    call = ExecutionCancelCall.model_validate(call.model_dump(mode="json"))
    result: Future[object] = Future()
    return cast(
        Future[ExecutionStopRecord],
        self._enqueue(
            _Work("cancel", call, min(deadline, time.monotonic() + 30), result), control=True
        ),
    )


@_method
def request_stop(
    self: "SQLiteStorage", request: ExecutionStopRequest, *, deadline: float
) -> ExecutionStopRecord:
    """Internal durable intent only; never sends a signal or grants a new claim."""
    request = ExecutionStopRequest.model_validate(request.model_dump(mode="json"))
    try:
        return cast(ExecutionStopRecord, self._submit("stop_request", request, deadline))
    except StorageFault as error:
        raise ReviewFault(error.code) from None


@_method
def finish_stop(
    self: "SQLiteStorage", observation: ExecutionStopObservation, *, deadline: float
) -> ExecutionStopRecord:
    observation = ExecutionStopObservation.model_validate(observation.model_dump(mode="json"))
    try:
        return cast(ExecutionStopRecord, self._submit("stop_finish", observation, deadline))
    except StorageFault as error:
        raise ReviewFault(error.code) from None


@_method
def finish_stop_with_capture(
    self: "SQLiteStorage",
    observation: ExecutionStopObservation,
    capture: DockerOutputSnapshot,
    *,
    deadline: float,
) -> ExecutionStopRecord:
    request = ExecutionStopCapture.model_validate(
        {
            "observation": observation.model_dump(mode="json"),
            "output": capture.model_dump(mode="json"),
        }
    )
    try:
        return cast(ExecutionStopRecord, self._submit("stop_finish", request, deadline))
    except StorageFault as error:
        raise ReviewFault(error.code) from None


@_method
def inspect_stop(
    self: "SQLiteStorage", scope: ExecutionInspectionScope, *, deadline: float
) -> ExecutionStopRecord | None:
    try:
        return cast(ExecutionStopRecord | None, self._submit("stop_inspect", scope, deadline))
    except StorageFault as error:
        raise ReviewFault(error.code) from None


@_method
def inspect_execution(
    self: "SQLiteStorage", scope: ExecutionInspectionScope, *, deadline: float
) -> ExecutionInspection:
    try:
        return cast(ExecutionInspection, self._submit("execution_inspect", scope, deadline))
    except StorageFault as error:
        raise ReviewFault(error.code) from None


@_method
def lookup_proposal(
    self: "SQLiteStorage", scope: ProposalRequestScope, *, deadline: float
) -> AuthorityRecord | None:
    """Scoped DB8 operation facts only, never permission or a new preparation."""
    try:
        return cast(AuthorityRecord | None, self._submit("proposal_lookup", scope, deadline))
    except StorageFault as error:
        raise ReviewFault(error.code) from None


@_method
def record(self: "SQLiteStorage", event: DockerLaunchEvent, *, deadline: float) -> None:
    """Private journal seam. Unconfirmed SQL never authorizes a repeated mutation."""
    try:
        self._submit("launch_record", event, deadline)
    except StorageFault:
        raise DockerBackendFault("docker_journal_unconfirmed") from None


@_method
def launch_status(
    self: "SQLiteStorage", scope: DockerLaunchScope, *, deadline: float
) -> DockerLaunchSnapshot:
    try:
        return cast(DockerLaunchSnapshot, self._submit("launch_status", scope, deadline))
    except StorageFault:
        raise DockerBackendFault("docker_journal_unconfirmed") from None


@_method
def complete_execution(
    self: "SQLiteStorage", completion: DockerCompletion, *, deadline: float
) -> DockerExecutionResult:
    try:
        completion = DockerCompletion.model_validate(completion.model_dump(mode="json"))
    except ValueError:
        raise DockerBackendFault("docker_output_failed") from None
    try:
        return cast(DockerExecutionResult, self._submit("execution_complete", completion, deadline))
    except StorageFault:
        raise DockerBackendFault("docker_journal_unconfirmed") from None


@_method
def execution_result(
    self: "SQLiteStorage", scope: DockerLaunchScope, *, deadline: float
) -> DockerExecutionResult:
    try:
        return cast(DockerExecutionResult, self._submit("execution_result", scope, deadline))
    except StorageFault:
        raise DockerBackendFault("docker_journal_unconfirmed") from None

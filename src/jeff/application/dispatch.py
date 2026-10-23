"""Finite, project-bound calls and transient replies; no general execution authority."""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol, Self
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from jeff.application.history import (
    ActivityPage,
    ActivityRecord,
    ActivityTrace,
    GitObservation,
    ReplayScope,
    activity_code,
)
from jeff.application.projects import ErrorData, Failure, ProjectData, ProjectService, Success
from jeff.application.questions import QuestionService, QuestionSuccess, question_intent
from jeff.application.runtime import (
    PROTOCOL_REVISION,
    ControlFault,
    ControlObservation,
    ProjectCapability,
    RuntimeControl,
    RuntimeIdentityData,
    control_failure,
)
from jeff.application.storage import (
    ProjectRegistry,
    Receipt,
    ReceiptClaim,
    ReceiptScope,
    ReceiptTransition,
    RequestLedger,
    StorageFault,
    storage_failure,
)
from jeff.domain.projects import Project

PROJECT_OPERATIONS: tuple[ProjectCapability, ...] = (
    "ask.branch",
    "ask.changes",
    "init",
    "config",
    "receipt.inspect",
    "activity.trace",
    "activity.replay",
)


class _DispatchModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class ProjectScope(_DispatchModel):
    directory: Path
    expected_root: Path | None = None
    expected_project_id: UUID | None = None

    @field_validator("directory", "expected_root")
    @classmethod
    def absolute(cls, value: Path | None) -> Path | None:
        if value is not None:
            text = str(value)
            if (
                not value.is_absolute()
                or "\0" in text
                or ".." in value.parts
                or text.startswith("//")
                or len(text.encode("utf-8")) > 1023
            ):
                raise ValueError("A valid bounded absolute directory is required.")
        return value


class ProjectCall(ProjectScope):
    request_id: UUID = Field(default_factory=uuid4)
    operation: Literal["init", "config", "ask"]
    question: str | None = Field(default=None, max_length=65536)

    @model_validator(mode="after")
    def question_only_for_ask(self) -> Self:
        if (self.operation == "ask") != (self.question is not None):
            raise ValueError("Only ask carries a transient question.")
        return self


class ReceiptQuery(ProjectScope):
    query_id: UUID = Field(default_factory=uuid4)
    request_id: UUID


class EvidenceQuery(ProjectScope):
    query_id: UUID = Field(default_factory=uuid4)
    operation: Literal["trace", "replay"]
    request_id: UUID | None = None
    after: StrictInt = Field(default=0, ge=0, le=9223372036854775807)
    limit: StrictInt = Field(default=64, gt=0, le=64)
    expected_epoch: UUID | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.operation == "trace":
            if (
                self.request_id is None
                or self.after != 0
                or self.limit != 64
                or self.expected_epoch is not None
            ):
                raise ValueError("Trace accepts one scoped request ID only.")
        elif self.request_id is not None:
            raise ValueError("Replay uses an epoch/sequence, not a request ID.")
        return self


class EvidenceRequest(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    kind: Literal["evidence"] = "evidence"
    expected_instance_id: UUID
    remaining_ms: StrictInt = Field(default=30000, gt=0, le=30000)
    query: EvidenceQuery


class EvidenceReply(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    request_id: UUID
    identity: RuntimeIdentityData
    operation: Literal["trace", "replay"]
    receipt: Receipt | None = None
    trace: ActivityTrace | None = None
    page: ActivityPage | None = None
    error: Failure | None = None
    missing_is_not_nonexecution_proof: Literal[True] = True

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.error is not None and any(
            x is not None for x in (self.receipt, self.trace, self.page)
        ):
            raise ValueError("An error cannot carry evidence.")
        if (self.receipt is None) != (self.trace is None):
            raise ValueError("Trace and receipt are one owner-thread snapshot.")
        if self.operation == "trace" and self.page is not None:
            raise ValueError("Trace cannot carry a replay page.")
        if self.operation == "replay" and (self.receipt is not None or self.trace is not None):
            raise ValueError("Replay cannot carry a trace snapshot.")
        if self.operation == "replay" and self.error is None and self.page is None:
            raise ValueError("Replay requires a page or fixed failure.")
        return self


class ProjectRequest(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    kind: Literal["project"] = "project"
    expected_instance_id: UUID
    remaining_ms: StrictInt = Field(default=30000, gt=0, le=30000)
    call: ProjectCall


class ReceiptRequest(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    kind: Literal["receipt"] = "receipt"
    expected_instance_id: UUID
    remaining_ms: StrictInt = Field(default=30000, gt=0, le=30000)
    query: ReceiptQuery


class DispatchReply(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    request_id: UUID
    identity: RuntimeIdentityData
    accepted: StrictBool | None = False
    result: Success | QuestionSuccess | Failure
    receipt: Receipt | None = None


class ReceiptReply(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    request_id: UUID
    identity: RuntimeIdentityData
    receipt: Receipt | None = None
    error: Failure | None = None


def dispatch_failure(
    code: Literal[
        "unsupported_operation",
        "unsupported_intent",
        "project_target_mismatch",
        "duplicate_request",
        "request_conflict",
        "request_timeout",
        "request_outcome_unknown",
        "runtime_busy",
        "runtime_stopping",
        "receipt_unavailable",
    ],
) -> Failure:
    messages = {
        "unsupported_operation": (
            "This dispatcher supports only project init/config and the two Git questions."
        ),
        "unsupported_intent": "Supported questions: 'what branch am I on?' and 'what changed?'.",
        "project_target_mismatch": "The validated project does not match the requested target.",
        "duplicate_request": "This request ID already has a receipt; no action was replayed.",
        "request_conflict": "The request ID conflicts with its recorded scope or state.",
        "request_timeout": "The request budget expired; no automatic retry was performed.",
        "request_outcome_unknown": (
            "Request outcome unconfirmed; inspect its receipt. Do not replay automatically."
        ),
        "runtime_busy": "Project admission is full or storage is busy; no work was replayed.",
        "runtime_stopping": "The dispatcher is closing admission.",
        "receipt_unavailable": "No receipt is available in the validated scope.",
    }
    return Failure(error=ErrorData(code=code, message=messages[code]))


class DispatchStore(RequestLedger, ProjectRegistry, Protocol):
    pass


class _RequestObserver:
    def __init__(
        self, store: DispatchStore, request_id: UUID, owner: UUID, deadline: float
    ) -> None:
        self.store, self.request_id, self.owner, self.deadline = store, request_id, owner, deadline

    def record(self, observation: GitObservation) -> None:
        self.store.observe(
            ActivityRecord(
                request_id=self.request_id, owner_instance_id=self.owner, git=observation
            ),
            deadline=max(self.deadline, time.monotonic() + 2),
        )


class ProjectDispatcher:
    """Synchronous project dispatch; scheduling, transport and SQL ownership live elsewhere."""

    def __init__(
        self,
        projects: ProjectService,
        questions: QuestionService,
        store: DispatchStore,
        identity: RuntimeIdentityData,
    ) -> None:
        self.projects, self.questions, self.store, self.identity = (
            projects,
            questions,
            store,
            identity,
        )

    def _bind(self, scope: ProjectScope, deadline: float) -> Project | Failure:
        if time.monotonic() >= deadline:
            return dispatch_failure("request_timeout")
        result = self.projects.execute("config", scope.directory, deadline=deadline)
        if isinstance(result, Failure):
            return result
        project = Project(UUID(result.data.project_id), Path(result.data.root))
        if (
            scope.expected_project_id is not None
            and scope.expected_project_id != project.project_id
        ) or (scope.expected_root is not None and scope.expected_root != project.root):
            return dispatch_failure("project_target_mismatch")
        return project

    def _config(self, call: ProjectCall, deadline: float) -> Success | Failure:
        """Diagnostic only: no indexing, claim, timestamp or project-file writes."""
        project = self._bind(call, deadline)
        if isinstance(project, Failure):
            return project
        try:
            existing = self.store.inspect(
                ReceiptScope(
                    request_id=call.request_id,
                    canonical_root=project.root,
                    project_id=project.project_id,
                ),
                deadline=deadline,
            )
            if existing is not None:
                return dispatch_failure("request_conflict")
            self.store.lookup(project, deadline=deadline)
            configured = self.projects.execute("config", project.root, deadline=deadline)
            if isinstance(configured, Failure):
                return configured
            if (
                configured.data.project_id != str(project.project_id)
                or Path(configured.data.root) != project.root
            ):
                return dispatch_failure("project_target_mismatch")
            return configured
        except StorageFault as error:
            return (
                dispatch_failure("request_conflict")
                if error.code == "receipt_scope_mismatch"
                else storage_failure(error.code)
            )

    def execute(self, call: ProjectCall, absolute_deadline: float) -> DispatchReply:
        def reply(
            result: Success | QuestionSuccess | Failure,
            accepted: bool | None = False,
            receipt: Receipt | None = None,
        ) -> DispatchReply:
            return DispatchReply(
                request_id=call.request_id,
                identity=self.identity,
                result=result,
                accepted=accepted,
                receipt=receipt,
            )

        if call.operation == "config":
            return reply(self._config(call, absolute_deadline))
        intent: Literal["init", "ask.branch", "ask.changes"] | None = (
            "init" if call.operation == "init" else question_intent(call.question or "")
        )
        if intent is None:
            return reply(dispatch_failure("unsupported_intent"))
        bound = self._bind(call, absolute_deadline)
        project: Project | None
        if isinstance(bound, Failure):
            if call.operation != "init" or bound.error.code != "project_not_initialized":
                return reply(bound)
            root_result = self.projects.resolve_root(call.directory, deadline=absolute_deadline)
            if isinstance(root_result, Failure):
                return reply(root_result)
            if call.expected_project_id is not None or (
                call.expected_root is not None and call.expected_root != root_result
            ):
                return reply(dispatch_failure("project_target_mismatch"))
            try:
                if self.store.root_claimed(root_result, deadline=absolute_deadline):
                    return reply(storage_failure("registry_conflict"))
            except StorageFault as error:
                return reply(storage_failure(error.code))
            root, project = root_result, None
        else:
            root, project = bound.root, bound
        accepted: bool | None = False
        initialization_attempted = False
        try:
            claim = self.store.claim(
                ReceiptClaim(
                    request_id=call.request_id,
                    owner_instance_id=self.identity.instance_id,
                    operation=intent,
                    canonical_root=root,
                    project_id=project.project_id if project is not None else None,
                ),
                deadline=absolute_deadline,
            )
            if not claim.created:
                return reply(dispatch_failure("duplicate_request"), True, claim.receipt)
            accepted = True
            self.store.finish(
                ReceiptTransition(
                    request_id=call.request_id,
                    owner_instance_id=self.identity.instance_id,
                    state="running",
                ),
                deadline=absolute_deadline,
            )
            result: Success | QuestionSuccess | Failure
            if call.operation == "init":
                initialization_attempted = True
                initialized = self.projects.execute("init", root, deadline=absolute_deadline)
                result = initialized
                if not isinstance(initialized, Failure):
                    actual = Project(UUID(initialized.data.project_id), Path(initialized.data.root))
                    if actual.root != root or (project is not None and actual != project):
                        result = dispatch_failure("project_target_mismatch")
                    else:
                        if project is None:
                            self.store.finish(
                                ReceiptTransition(
                                    request_id=call.request_id,
                                    owner_instance_id=self.identity.instance_id,
                                    state="running",
                                    project_id=actual.project_id,
                                ),
                                deadline=absolute_deadline,
                            )
                        project = actual
                        self.store.observe(
                            ActivityRecord(
                                request_id=call.request_id,
                                owner_instance_id=self.identity.instance_id,
                                registration_created=initialized.data.created,
                            ),
                            deadline=absolute_deadline,
                        )
                        self.store.index(project, deadline=absolute_deadline)
            else:
                assert project is not None
                self.store.index(project, deadline=absolute_deadline)
                result = self.questions.execute(
                    call.question or "",
                    root,
                    deadline=absolute_deadline,
                    observer=_RequestObserver(
                        self.store, call.request_id, self.identity.instance_id, absolute_deadline
                    ),
                )
            if not isinstance(result, Failure):
                rebound = self._bind(call, absolute_deadline)
                if isinstance(rebound, Failure):
                    result = rebound
                elif (
                    rebound != project
                    or result.data.project_id != str(rebound.project_id)
                    or Path(result.data.root) != root
                ):
                    result = dispatch_failure("project_target_mismatch")
            final = self.store.finish(
                ReceiptTransition(
                    request_id=call.request_id,
                    owner_instance_id=self.identity.instance_id,
                    error_code=activity_code(result.error.code)
                    if isinstance(result, Failure)
                    else None,
                    state=("unknown" if initialization_attempted else "failed")
                    if isinstance(result, Failure)
                    else "succeeded",
                    outcome_code=("partial" if initialization_attempted else "operation_failed")
                    if isinstance(result, Failure)
                    else "ok",
                ),
                deadline=time.monotonic() + 2,
            )
            return reply(result, True, final)
        except StorageFault as error:
            if error.code == "registry_conflict" and accepted:
                try:
                    final = self.store.finish(
                        ReceiptTransition(
                            request_id=call.request_id,
                            owner_instance_id=self.identity.instance_id,
                            state="unknown" if initialization_attempted else "failed",
                            outcome_code="partial"
                            if initialization_attempted
                            else "target_mismatch",
                        ),
                        deadline=time.monotonic() + 2,
                    )
                except StorageFault:
                    return reply(dispatch_failure("request_outcome_unknown"), True)
                return reply(storage_failure(error.code), True, final)
            if error.code == "receipt_conflict":
                return reply(dispatch_failure("request_conflict"), accepted)
            if not accepted and error.code == "storage_busy":
                return reply(dispatch_failure("runtime_busy"))
            if not accepted:
                accepted = None  # a lost claim acknowledgement cannot establish non-acceptance
            if accepted:
                try:
                    self.store.finish(
                        ReceiptTransition(
                            request_id=call.request_id,
                            owner_instance_id=self.identity.instance_id,
                            state="unknown",
                            outcome_code="partial",
                        ),
                        deadline=time.monotonic() + 2,
                    )
                except StorageFault:
                    pass
            return reply(dispatch_failure("request_outcome_unknown"), accepted)
        except Exception:
            if accepted:
                try:
                    self.store.finish(
                        ReceiptTransition(
                            request_id=call.request_id,
                            owner_instance_id=self.identity.instance_id,
                            state="unknown",
                            outcome_code="partial",
                        ),
                        deadline=time.monotonic() + 2,
                    )
                except Exception:
                    pass
            return reply(dispatch_failure("request_outcome_unknown"), accepted)

    def inspect_evidence(self, query: EvidenceQuery, absolute_deadline: float) -> EvidenceReply:
        def failed(error: Failure) -> EvidenceReply:
            return EvidenceReply(
                request_id=query.query_id,
                identity=self.identity,
                operation=query.operation,
                error=error,
            )

        bound = self._bind(query, absolute_deadline)
        if isinstance(bound, Failure):
            if query.operation != "trace" or bound.error.code != "project_not_initialized":
                return failed(bound)
            root_result = self.projects.resolve_root(query.directory, deadline=absolute_deadline)
            if isinstance(root_result, Failure):
                return failed(root_result)
            if query.expected_project_id is not None or (
                query.expected_root is not None and query.expected_root != root_result
            ):
                return failed(dispatch_failure("project_target_mismatch"))
            root, project_id = root_result, None
        else:
            root, project_id = bound.root, bound.project_id
        try:
            if query.operation == "trace":
                assert query.request_id is not None
                receipt, trace = self.store.trace(
                    ReceiptScope(
                        request_id=query.request_id,
                        canonical_root=root,
                        project_id=project_id,
                        allow_unbound_init=True,
                    ),
                    deadline=absolute_deadline,
                )
                return EvidenceReply(
                    request_id=query.query_id,
                    identity=self.identity,
                    operation=query.operation,
                    receipt=receipt,
                    trace=trace,
                )
            assert project_id is not None
            page = self.store.replay(
                ReplayScope(
                    project_id=project_id,
                    canonical_root=root,
                    after=query.after,
                    limit=query.limit,
                    expected_epoch=query.expected_epoch,
                ),
                deadline=absolute_deadline,
            )
            return EvidenceReply(
                request_id=query.query_id,
                identity=self.identity,
                operation=query.operation,
                page=page,
            )
        except StorageFault as error:
            return failed(
                dispatch_failure("receipt_unavailable")
                if error.code == "receipt_scope_mismatch"
                else storage_failure(error.code)
            )

    def inspect_receipt(self, query: ReceiptQuery, absolute_deadline: float) -> ReceiptReply:
        bound = self._bind(query, absolute_deadline)
        if isinstance(bound, Failure):
            if bound.error.code != "project_not_initialized":
                return ReceiptReply(request_id=query.query_id, identity=self.identity, error=bound)
            root_result = self.projects.resolve_root(query.directory, deadline=absolute_deadline)
            if isinstance(root_result, Failure):
                return ReceiptReply(
                    request_id=query.query_id, identity=self.identity, error=root_result
                )
            if query.expected_project_id is not None or (
                query.expected_root is not None and query.expected_root != root_result
            ):
                return ReceiptReply(
                    request_id=query.query_id,
                    identity=self.identity,
                    error=dispatch_failure("project_target_mismatch"),
                )
            root, project_id = root_result, None
        else:
            root, project_id = bound.root, bound.project_id
        try:
            receipt = self.store.inspect(
                ReceiptScope(
                    request_id=query.request_id,
                    canonical_root=root,
                    project_id=project_id,
                    allow_unbound_init=True,
                ),
                deadline=absolute_deadline,
            )
            return ReceiptReply(request_id=query.query_id, identity=self.identity, receipt=receipt)
        except StorageFault:
            return ReceiptReply(
                request_id=query.query_id,
                identity=self.identity,
                error=dispatch_failure("receipt_unavailable"),
            )


class ProjectStatusData(ProjectData):
    runtime: ControlObservation


class ProjectStatusSuccess(_DispatchModel):
    ok: Literal[True] = True
    data: ProjectStatusData
    error: None = None


class ProjectStatusService:
    """Local read-only registration, then lazily selected runtime diagnostics; no action path."""

    def __init__(self, projects: ProjectService, control: Callable[[], RuntimeControl]) -> None:
        self.projects, self.control = projects, control

    def execute(self, directory: Path) -> ProjectStatusSuccess | Failure:
        project = self.projects.execute("status", directory)
        if isinstance(project, Failure):
            return project
        try:
            runtime = self.control().status()
        except ControlFault as error:
            return control_failure(error.code)
        if isinstance(runtime, Failure):
            return runtime
        return ProjectStatusSuccess(
            data=ProjectStatusData(**project.data.model_dump(), runtime=runtime.data)
        )


class EvidenceClient(Protocol):
    def inspect_evidence(self, query: EvidenceQuery) -> EvidenceReply: ...


class ProjectClient(Protocol):
    def execute(self, call: ProjectCall) -> DispatchReply: ...
    def inspect_receipt(self, query: ReceiptQuery) -> ReceiptReply: ...
    def status(self, directory: Path) -> ProjectStatusSuccess | Failure: ...

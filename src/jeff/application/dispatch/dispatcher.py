"""Synchronous durable project actions; scheduling and ownership stay elsewhere."""

import time
from pathlib import Path
from typing import Literal
from uuid import UUID

from jeff.application.compose_status import (
    ComposeStatusReader,
    ComposeStatusSuccess,
    compose_status_failure,
)
from jeff.application.history import ActivityRecord, activity_code
from jeff.application.projects import Failure, ProjectService, Success
from jeff.application.questions import QuestionService, QuestionSuccess, question_intent
from jeff.application.runtime import RuntimeIdentityData
from jeff.application.storage import (
    Receipt,
    ReceiptClaim,
    ReceiptScope,
    ReceiptTransition,
    StorageFault,
    storage_failure,
)
from jeff.domain.projects import Project

from . import evidence
from .failures import dispatch_failure
from .observation import _RequestObserver
from .ports import DispatchStore
from .replies import DispatchReply, EvidenceReply, ReceiptReply
from .requests import EvidenceQuery, ProjectCall, ReceiptQuery
from .scope import ProjectScope
from .subscriptions import StreamQuery, StreamReply


class ProjectDispatcher:
    """Synchronous project dispatch; scheduling, transport and SQL ownership live elsewhere."""

    def __init__(
        self,
        projects: ProjectService,
        questions: QuestionService,
        store: DispatchStore,
        identity: RuntimeIdentityData,
        *,
        compose_status: ComposeStatusReader | None = None,
    ) -> None:
        self.compose_status = compose_status
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
        """Claim actions durably before mutation; config remains a read-only exception.

        Queue/transport admission is not the claim. A lost claim acknowledgement
        leaves acceptance unknown; missing replies never authorize action replay.
        """

        def reply(
            result: Success | QuestionSuccess | ComposeStatusSuccess | Failure,
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
        if call.operation == "docker.status":
            if self.compose_status is None:
                return reply(compose_status_failure("compose_status_not_configured"))
            target = self._bind(call, absolute_deadline)
            if isinstance(target, Failure):
                return reply(target)
            try:
                if self.store.lookup(target, deadline=absolute_deadline) is None:
                    return reply(compose_status_failure("compose_status_not_configured"))
            except StorageFault as error:
                return reply(storage_failure(error.code))
            return reply(self.compose_status(target, absolute_deadline))
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
            if accepted is False and error.code == "request_retired":
                return reply(storage_failure("request_retired"))
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

    def read_stream(self, query: StreamQuery, absolute_deadline: float) -> StreamReply:
        """Rebind the current project before reading a committed, cursor-checked page."""
        return evidence.read_stream(self, query, absolute_deadline)

    def inspect_evidence(self, query: EvidenceQuery, absolute_deadline: float) -> EvidenceReply:
        """Read scoped evidence without registration repair or invented nonexecution proof."""
        return evidence.inspect_evidence(self, query, absolute_deadline)

    def inspect_receipt(self, query: ReceiptQuery, absolute_deadline: float) -> ReceiptReply:
        return evidence.inspect_receipt(self, query, absolute_deadline)

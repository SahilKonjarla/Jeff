"""Inspect imported execution facts through native IPC without enabling a launcher."""

from collections.abc import Callable
from pathlib import Path
from typing import Never, cast
from uuid import UUID

from jeff.application.execution import (
    DockerBackendFault,
    ExecutionAdmission,
    ExecutionInspectionScope,
    ExecutionStartRefused,
    FreshExecutionCall,
    FreshExecutionCompletion,
    FreshExecutionReply,
    FreshExecutionView,
    ReviewFault,
    fresh_execution_failure,
)
from jeff.application.execution.pytest_report import pytest_run_token
from jeff.application.execution.watch import WatchOperation, WatchScope
from jeff.application.projects import ProjectService
from jeff.application.storage import StorageFault
from jeff.domain.projects import Project
from jeff.infrastructure.postgres_project_actor import PostgresProjectActor


class PostgresReadback:
    """The proof daemon may inspect jobs or manage an inbox, never start a job."""

    def __init__(
        self, projects: ProjectService, actor: PostgresProjectActor, instance: UUID
    ) -> None:
        self._projects, self._actor, self._instance = projects, actor, instance

    def start(
        self,
        directory: Path,
        approval_id: UUID,
        expected_manifest_digest: str,
        *,
        request_id: UUID,
        admission_deadline: float,
        on_admitted: Callable[[ExecutionAdmission], None],
    ) -> Never:
        raise ExecutionStartRefused("docker_authority_required")

    def execute(self, call: FreshExecutionCall, deadline: float) -> FreshExecutionReply:
        common = dict(request_id=call.request_id, operation=call.operation, identity=self._instance)
        try:
            operations = {"show", "logs", "tests.results", "tests.latest"}
            watches = {"watch.job", "watch.pending", "watch.ack"}
            if call.operation not in operations | watches:
                raise DockerBackendFault("docker_authority_required")
            found = self._projects.execute("config", call.directory, deadline=deadline)
            if not found.ok:
                raise ReviewFault("execution_not_found")
            project = Project(UUID(found.data.project_id), Path(found.data.root))
            if self._actor.lookup(project, deadline=deadline) != project:
                raise ReviewFault("execution_not_found")
            scope = dict(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=self._instance,
            )
            if call.operation in watches:
                inbox = self._actor.watch(
                    WatchScope.model_validate(
                        scope
                        | dict(
                            operation=cast(WatchOperation, call.operation),
                            target=call.watch_target,
                        )
                    ),
                    deadline=deadline,
                )
                return FreshExecutionReply.model_validate(common | dict(ok=True, watch=inbox))
            observed = self._actor.inspect_execution(
                ExecutionInspectionScope.model_validate(
                    scope
                    | dict(
                        approval_id=call.approval_id,
                        job_id=call.job_id,
                        latest_pytest=call.operation == "tests.latest",
                    )
                ),
                deadline=deadline,
            )
            record, result = observed.record, observed.result
            report = None
            if call.operation.startswith("tests."):
                token = pytest_run_token(record.manifest.argv)
                if token is None or (call.job_id is not None and record.job_id != call.job_id):
                    raise ReviewFault("execution_not_found")
                report = None if result is None else result.pytest_result
                if (
                    report is not None
                    and report.report is not None
                    and report.report.run_token != token
                ):
                    raise DockerBackendFault("docker_output_failed")
            summary = (
                None
                if result is None
                else FreshExecutionCompletion(
                    disposition=result.disposition,
                    outcome=result.outcome,
                    output_facts=result.output_facts,
                    payload_state=result.payload_state,
                    retirement_reason=result.retirement_reason,
                    recorded_at_ms=result.recorded_at_ms,
                )
            )
            return FreshExecutionReply.model_validate(
                common
                | dict(
                    ok=True,
                    view=FreshExecutionView.from_record(record),
                    completion=summary,
                    completion_state=observed.completion_state,
                    stop=observed.stop,
                    pytest_result=report,
                    execution_phase=(
                        "finished"
                        if record.claim_state == "finished"
                        else "unconfirmed"
                        if record.claim_state == "unknown"
                        else None
                    ),
                    output=(result.output if result is not None else observed.stop_output)
                    if call.operation == "logs"
                    else None,
                )
            )
        except (StorageFault, ReviewFault, DockerBackendFault) as error:
            return FreshExecutionReply.model_validate(
                common | dict(ok=False, error=fresh_execution_failure(error.code))
            )

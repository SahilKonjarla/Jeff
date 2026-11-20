"""Public intent routing and scoped facts-only inspection projections."""

from typing import TYPE_CHECKING

from jeff.application.execution import (
    DockerBackendFault,
    ExecutionInspectionScope,
    ExecutionListScope,
    FreshExecutionCall,
    FreshExecutionCompletion,
    FreshExecutionReply,
    FreshExecutionView,
    PreparedInputFault,
    ReviewFault,
    fresh_execution_failure,
)
from jeff.application.execution.pytest_report import pytest_run_token
from jeff.application.storage import StorageFault

from .binding import _deadline, _project

if TYPE_CHECKING:
    from .coordinator import ExecutionCoordinator


def execute(
    self: "ExecutionCoordinator", call: FreshExecutionCall, deadline: float
) -> FreshExecutionReply:
    """Public intent gateway. Starts must use the existing owned-worker observer seam."""
    try:
        _deadline(deadline)
        if call.operation in {"approvals.list", "jobs.list"}:
            if self.listing is None:
                raise DockerBackendFault("docker_authority_required")
            project = _project(self, call.directory, deadline)
            page = self.listing.list_executions(
                ExecutionListScope(
                    project_id=project.project_id,
                    canonical_root=project.root,
                    owner_instance_id=self.instance_id,
                    after=call.after,
                    limit=call.limit,
                ),
                deadline=deadline,
            )
            if (page.project_id, page.canonical_root) != (
                project.project_id,
                project.root,
            ) or not page.within(after=call.after, limit=call.limit):
                raise DockerBackendFault("docker_authority_required")
            return FreshExecutionReply(
                ok=True,
                request_id=call.request_id,
                operation=call.operation,
                identity=self.instance_id,
                page=page,
            )
        if call.operation == "propose":
            assert call.profile_name is not None
            record = self.propose(
                call.directory,
                call.profile_name,
                call.argv,
                call.paths,
                request_id=call.request_id,
                deadline=deadline,
            )
        elif call.operation in {"approve", "deny"}:
            assert call.approval_id is not None and call.reviewed_manifest_digest is not None
            record = self.decide(
                call.directory,
                call.approval_id,
                call.reviewed_manifest_digest,
                approve=call.operation == "approve",
                request_id=call.request_id,
                deadline=deadline,
            )
        elif call.operation in {"show", "logs", "tests.results"}:
            if self.inspection is None:
                raise DockerBackendFault("docker_authority_required")
            project = _project(self, call.directory, deadline)
            observed = self.inspection.inspect_execution(
                ExecutionInspectionScope(
                    project_id=project.project_id,
                    canonical_root=project.root,
                    owner_instance_id=self.instance_id,
                    approval_id=call.approval_id,
                    job_id=call.job_id,
                ),
                deadline=deadline,
            )
            record = observed.record
            result = observed.result
            pytest_result = None
            if call.operation == "tests.results":
                token = pytest_run_token(record.manifest.argv)
                if token is None or record.job_id != call.job_id:
                    raise ReviewFault("execution_target_mismatch")
                if result is not None:
                    pytest_result = result.pytest_result
                    if (
                        pytest_result is not None
                        and pytest_result.report is not None
                        and pytest_result.report.run_token != token
                    ):
                        raise DockerBackendFault("docker_output_failed")
            completion = (
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
            return FreshExecutionReply(
                ok=True,
                request_id=call.request_id,
                operation=call.operation,
                identity=self.instance_id,
                view=FreshExecutionView.from_record(record),
                completion=completion,
                completion_state=observed.completion_state,
                stop=observed.stop,
                pytest_result=pytest_result,
                execution_phase=self.execution_phase(record.approval_id)
                or (
                    "finished"
                    if record.claim_state == "finished"
                    or (
                        observed.stop is not None
                        and observed.stop.outcome in {"cancelled", "timed_out", "completed"}
                    )
                    else "unconfirmed"
                    if record.claim_state == "unknown"
                    else None
                ),
                output=(result.output if result is not None else observed.stop_output)
                if call.operation == "logs"
                else None,
            )
        else:
            raise DockerBackendFault("docker_authority_required")
        return FreshExecutionReply(
            ok=True,
            request_id=call.request_id,
            operation=call.operation,
            identity=self.instance_id,
            view=FreshExecutionView.from_record(record),
        )
    except (DockerBackendFault, PreparedInputFault, ReviewFault, StorageFault) as error:
        failure = fresh_execution_failure(error.code)
    except Exception:
        failure = fresh_execution_failure("execution_unconfirmed")
    return FreshExecutionReply(
        ok=False,
        request_id=call.request_id,
        operation=call.operation,
        identity=self.instance_id,
        error=failure,
    )

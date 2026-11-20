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
    GuardedExecutionManifest,
    PreparedInputFault,
    ReviewFault,
    fresh_execution_failure,
)
from jeff.application.execution.log_tail import LiveLogPage, LiveLogTail
from jeff.application.execution.pytest_report import (
    PYTEST_RUNNER_ARGV,
    pytest_retry_token,
    pytest_run_token,
)
from jeff.application.storage import StorageFault
from jeff.domain.execution import ARGV_BYTES, ARGV_COUNT

from . import proposal
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
                    claim_filter=call.claim_filter,
                    kind_filter=call.kind_filter,
                ),
                deadline=deadline,
            )
            if (page.project_id, page.canonical_root) != (
                project.project_id,
                project.root,
            ) or not (
                page.within(after=call.after, limit=call.limit)
                and page.matches(call.claim_filter, call.kind_filter)
            ):
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
        elif call.operation in {
            "show",
            "logs",
            "tests.results",
            "tests.latest",
            "tests.retry-failed",
            "jobs.logs",
        }:
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
                    latest_pytest=call.operation == "tests.latest",
                ),
                deadline=deadline,
            )
            record = observed.record
            result = observed.result
            pytest_result = None
            if call.operation.startswith("tests."):
                token = pytest_run_token(record.manifest.argv)
                if token is None or (call.job_id is not None and record.job_id != call.job_id):
                    raise ReviewFault("execution_target_mismatch")
                if result is not None:
                    pytest_result = result.pytest_result
                    if (
                        pytest_result is not None
                        and pytest_result.report is not None
                        and pytest_result.report.run_token != token
                    ):
                        raise DockerBackendFault("docker_output_failed")
            if call.operation == "tests.retry-failed":
                report = pytest_result.report if pytest_result is not None else None
                manifest = record.manifest
                if (
                    result is None
                    or result.disposition != "completed"
                    or not isinstance(manifest, GuardedExecutionManifest)
                    or report is None
                    or report.version != 2
                    or not report.failures
                ):
                    raise ReviewFault("execution_retry_unavailable")
                argv = (
                    *PYTEST_RUNNER_ARGV,
                    pytest_retry_token(call.request_id, record.job_id),
                    *(failure.node_id for failure in report.failures),
                )
                if len(argv) > ARGV_COUNT or sum(len(arg.encode()) for arg in argv) > ARGV_BYTES:
                    raise ReviewFault("execution_retry_unavailable")
                retry = proposal.propose(
                    self,
                    call.directory,
                    manifest.profile.name,
                    argv,
                    tuple(item.path for item in manifest.inputs.files),
                    deadline=deadline,
                    request_id=call.request_id,
                    retry_of=manifest,
                )
                return FreshExecutionReply(
                    ok=True,
                    request_id=call.request_id,
                    operation=call.operation,
                    identity=self.instance_id,
                    view=FreshExecutionView.from_record(retry),
                )
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
            log_page = None
            if call.operation == "jobs.logs":
                if record.job_id != call.job_id:
                    raise ReviewFault("execution_not_found")
                with self._control_mutex:
                    active = self._log_tail
                if (
                    active is not None
                    and active[0] == record.job_id
                    and (result is None or result.payload_state == "retained")
                    and (
                        observed.stop is None
                        or observed.stop.payload_state
                        not in {"retired", "expired", "clock_unconfirmed"}
                    )
                ):
                    log_page = active[1].read(call.log_after)
                    if log_page.closed and record.claim_state == "claimed":
                        log_page = log_page.model_copy(update={"closed": False})
                elif call.log_after is not None:
                    log_page = LiveLogPage(
                        offset=call.log_after,
                        next_offset=call.log_after,
                        dropped=True,
                        closed=True,
                        chunks=(),
                    )
                else:
                    archive = result.output if result is not None else observed.stop_output
                    fallback = LiveLogTail()
                    if archive is not None:
                        for tag, text in enumerate((archive.stdout, archive.stderr)):
                            data = text.encode("ascii")
                            fallback.publish(data, bytes((tag,)) * len(data))
                    fallback.close()
                    log_page = fallback.read()
                    if archive is None or archive.truncated or archive.aborted:
                        log_page = log_page.model_copy(update={"dropped": True})
            return FreshExecutionReply(
                ok=True,
                request_id=call.request_id,
                operation=call.operation,
                identity=self.instance_id,
                view=FreshExecutionView.from_record(record),
                completion=completion,
                log_page=log_page,
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

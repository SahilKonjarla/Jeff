"""Legacy review policy through project and storage ports; no executor."""

from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from jeff.application.projects import ErrorData, Failure, ProjectService
from jeff.domain.projects import Project

from .manifests import build_manifest
from .ports import ReviewStore
from .profiles import ExecutionProfile
from .review_models import (
    ExecutionCall,
    ExecutionReply,
    ExecutionResult,
    ExecutionSuccess,
    ReviewFault,
    ReviewOperation,
)


def execution_failure(code: str) -> Failure:
    messages = {
        "execution_profile_missing": "No declared execution profile binds this project.",
        "execution_invalid": "The public action or profile is invalid; no review was stored.",
        "execution_target_mismatch": "Execution declaration changed; no authority issued.",
        "execution_not_found": "No review record exists in this validated project scope.",
        "execution_conflict": "Review identity or decision conflicts; inspect existing facts.",
        "execution_expired": "The review expired or was invalidated; fresh review is required.",
        "execution_capacity": "The bounded review ledger is full; no additional review admitted.",
        "execution_clock": "Review clock is unconfirmed; no approval was granted.",
        "execution_outcome_unknown": "A prior launch is unconfirmed; new claims are quarantined.",
        "storage_busy": "Storage admission is busy; no automatic retry was made.",
        "storage_closing": "Storage is closing; inspect existing facts after restart.",
        "storage_outcome_unconfirmed": "Storage outcome is unknown; inspect rather than retry.",
        "storage_failed": "Storage failed; no automatic repair was attempted.",
    }
    safe = code if code in messages else "execution_invalid"
    return Failure(error=ErrorData(code=safe, message=messages[safe]))


class ExecutionService:
    def __init__(
        self,
        projects: ProjectService,
        store: ReviewStore,
        profiles: Callable[[], tuple[ExecutionProfile, ...]],
        instance_id: UUID,
    ) -> None:
        self.projects, self.store, self.profiles, self.instance_id = (
            projects,
            store,
            profiles,
            instance_id,
        )

    def execute(self, call: ExecutionCall, deadline: float) -> ExecutionReply:
        # ProjectService uses fixed internal discovery, never the proposed argv.
        observed = self.projects.execute("config", call.directory, deadline=deadline)
        if not observed.ok:
            return ExecutionReply(request_id=call.request_id, result=observed)
        project = Project(UUID(observed.data.project_id), Path(observed.data.root))
        if (call.expected_root is not None and call.expected_root != project.root) or (
            call.expected_project_id is not None and call.expected_project_id != project.project_id
        ):
            return ExecutionReply(
                request_id=call.request_id, result=execution_failure("execution_target_mismatch")
            )
        try:
            name = call.profile_name
            if call.operation == "approve":
                current = self.store.review(
                    ReviewOperation(
                        call=call.model_copy(update={"operation": "approval.show"}),
                        project_id=project.project_id,
                        canonical_root=project.root,
                        owner_instance_id=self.instance_id,
                    ),
                    deadline=deadline,
                )
                name = current.items[0].manifest.profile.name
            matches = [
                p
                for p in self.profiles()
                if p.name == name
                and p.project_id == project.project_id
                and p.canonical_root == project.root
            ]
            profile = matches[0] if len(matches) == 1 else None
            if call.operation == "propose" and profile is None:
                return ExecutionReply(
                    request_id=call.request_id,
                    result=execution_failure("execution_profile_missing"),
                )
            manifest = (
                build_manifest(project, profile, call.argv)
                if call.operation == "propose" and profile is not None
                else None
            )
            operation = ReviewOperation(
                call=call,
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=self.instance_id,
                manifest=manifest,
                configuration_fingerprint=profile.fingerprint if profile is not None else None,
            )
            data = self.store.review(operation, deadline=deadline)
            result: ExecutionResult = ExecutionSuccess(data=data)
        except ReviewFault as error:
            result = execution_failure(error.code)
        except ValueError:
            result = execution_failure("execution_invalid")
        return ExecutionReply(request_id=call.request_id, identity=self.instance_id, result=result)

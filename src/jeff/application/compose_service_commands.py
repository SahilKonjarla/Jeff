"""Project-scoped public service review; only Apply reaches the existing worker."""

import time
from typing import Literal, Protocol, Self
from uuid import UUID

from pydantic import model_validator

from jeff.application.compose_lifecycle import (
    Digest,
    ServiceAction,
    ServiceAuthorityOperation,
    ServiceAuthorityRecord,
    ServiceStart,
    ExistingServiceAction,
    _Fact,
)
from jeff.application.compose_service_worker import (
    ServiceBackend,
    ServiceJournal,
    ServiceWork,
    ServiceWorker,
    ServiceWorkResult,
)
from jeff.application.execution import DockerBackendFault, ReviewFault
from jeff.application.projects import ErrorData, Failure
from jeff.domain.execution import AuthorityState
from jeff.domain.projects import Project

from .compose_status import ComposeName


class ServiceCommand(_Fact):
    operation: Literal["propose-start", "propose-stop", "propose-restart", "approve", "apply"]
    service: ComposeName | None = None
    start_job_id: UUID | None = None
    approval_id: UUID | None = None
    digest: Digest | None = None

    @model_validator(mode="after")
    def exact_arguments(self) -> Self:
        targeted = self.operation in ("approve", "apply")
        if (
            (self.service is not None) != (self.operation == "propose-start")
            or (self.start_job_id is not None) != (self.operation in ("propose-stop", "propose-restart"))
            or (self.approval_id is not None) != targeted
            or (self.digest is not None) != targeted
        ):
            raise ValueError("Service operation has an invalid target.")
        return self


class ServiceReview(_Fact):
    approval_id: UUID
    job_id: UUID
    manifest: ServiceAction
    digest: Digest
    state: AuthorityState
    expires_at_ms: int


class ServiceCommandSuccess(_Fact):
    ok: Literal[True] = True
    data: ServiceReview | ServiceWorkResult
    error: None = None


class ServiceReviewJournal(ServiceJournal, Protocol):
    def service_stop_manifest(
        self, project: Project, job_id: UUID, *, deadline: float, restart: bool = False
    ) -> ExistingServiceAction: ...


def service_failure(code: str = "service_unavailable") -> Failure:
    messages = {
        "service_unavailable": "Service lifecycle is not available for this project.",
        "service_target_unconfirmed": "The reviewed service target could not be confirmed.",
        "service_not_found": "No matching service job or approval was found.",
        "service_conflict": "The service approval or resource state does not allow this action.",
        "service_review_unknown": "The review outcome could not be confirmed; do not replay it.",
    }
    if code not in messages:
        code = "service_review_unknown"
    return Failure(error=ErrorData(code=code, message=messages[code]))


class ServiceCommands:
    def __init__(
        self, journal: ServiceReviewJournal, backend: ServiceBackend,
        worker: ServiceWorker, start: ServiceStart, owner: UUID,
    ) -> None:
        self._journal = journal
        self._backend = backend
        self._worker = worker
        self._start = start
        self._owner = owner

    def execute(
        self, project: Project, request_id: UUID, command: ServiceCommand, deadline: float
    ) -> ServiceCommandSuccess | Failure:
        target = self._start.target
        if (target.project_id, target.root) != (project.project_id, project.root):
            return service_failure()
        if time.monotonic() >= deadline:
            return service_failure("service_conflict")
        if command.operation == "apply":
            assert command.approval_id is not None and command.digest is not None
            result = self._worker.execute(
                ServiceWork(
                    request_id=request_id, project_id=project.project_id, canonical_root=project.root,
                    approval_id=command.approval_id, reviewed_digest=command.digest,
                ),
                deadline,
            )
            if result.outcome == "refused":
                return service_failure("service_conflict")
            return ServiceCommandSuccess(data=result)

        def authority(
            operation: Literal["show", "propose", "approve"],
            *, manifest: ServiceAction | None = None,
            digest: str | None = None,
        ) -> ServiceAuthorityRecord:
            return self._journal.service_authority(
                ServiceAuthorityOperation(
                    operation=operation, request_id=request_id, project_id=project.project_id,
                    canonical_root=project.root, owner_instance_id=self._owner,
                    approval_id=command.approval_id, manifest=manifest, current_manifest_digest=digest,
                ),
                deadline=deadline,
            ).record

        try:
            action: ServiceAction
            if command.operation == "propose-start":
                if command.service != target.service:
                    return service_failure()
                action = self._start
            elif command.operation in ("propose-stop", "propose-restart"):
                assert command.start_job_id is not None
                action = self._journal.service_stop_manifest(
                    project, command.start_job_id, deadline=deadline,
                    restart=command.operation == "propose-restart",
                )
            else:
                existing = authority("show")
                if existing.manifest_digest != command.digest or existing.state != AuthorityState.PENDING:
                    return service_failure("service_conflict")
                action = existing.manifest
            if action.target != target or (
                self._backend.verify(action, deadline=deadline) != action.digest
            ):
                return service_failure("service_target_unconfirmed")
            if time.monotonic() >= deadline:
                return service_failure("service_conflict")
            record = (
                authority("approve", digest=action.digest) if command.operation == "approve"
                else authority("propose", manifest=action, digest=action.digest)
            )
            return ServiceCommandSuccess(data=ServiceReview(
                approval_id=record.approval_id, job_id=record.job_id, manifest=record.manifest,
                digest=record.manifest_digest, state=record.state, expires_at_ms=record.expires_at_ms,
            ))
        except DockerBackendFault:
            return service_failure("service_target_unconfirmed")
        except ReviewFault as error:
            return service_failure({
                "storage_unrecognized": "service_unavailable", "execution_not_found": "service_not_found",
                "execution_conflict": "service_conflict", "execution_expired": "service_conflict",
                "execution_target_mismatch": "service_target_unconfirmed",
            }.get(error.code, "service_review_unknown"))

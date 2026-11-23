"""PostgreSQL ports for the existing guarded controller, serialized on one writer."""

from collections.abc import Callable
from concurrent.futures import Future
from typing import Literal, TypeVar

from jeff.application.execution import (
    AuthorityOperation,
    AuthorityRecord,
    AuthorityResult,
    DockerBackendFault,
    DockerCompletion,
    DockerExecutionResult,
    DockerLaunchEvent,
    DockerOutputSnapshot,
    ExecutionCancelCall,
    ExecutionInspectionScope,
    ExecutionListScope,
    ExecutionProfile,
    ExecutionStopObservation,
    ExecutionStopRecord,
    ExecutionStopRequest,
    FreshExecutionPage,
    GuardedDockerBackendConfiguration,
    ProposalRequestScope,
    ReviewFault,
)
from jeff.application.storage import GuardedRuntimeSettings, StorageFault
from jeff.infrastructure.runtime_state import Ownership

from . import postgres_inspection
from .postgres_execution import find_authority
from .postgres_project_actor import PostgresProjectActor
from .postgres_storage import PostgresStorage

T = TypeVar("T")


class PostgresExecutionActor(PostgresProjectActor):
    """Storage only: no image verification, process control, or Docker fallback."""

    def __init__(
        self,
        owner: Ownership,
        adapter: PostgresStorage,
        settings: GuardedRuntimeSettings,
        *,
        on_commit: Callable[[], None],
        settings_source: Literal["default_file", "existing_file"] = "existing_file",
    ) -> None:
        super().__init__(
            adapter,
            on_commit=on_commit,
            retention_days=settings.history.retention_days,
            settings_source=settings_source,
        )
        self.owner = owner
        self.instance_id = adapter.instance_id
        self.execution_profiles: tuple[ExecutionProfile, ...] = settings.execution.profiles
        self.execution_backend: GuardedDockerBackendConfiguration | None = settings.backend

    def _review(
        self, operation: Callable[[float], T], deadline: float, *, write: bool = False
    ) -> T:
        try:
            return self._call(operation, deadline, write=write)
        except StorageFault as error:
            raise ReviewFault(error.code) from None

    def authority(self, operation: AuthorityOperation, *, deadline: float) -> AuthorityResult:
        return self._review(
            lambda end: self._adapter.authority(operation, deadline=end),
            deadline,
            write=operation.operation != "show",
        )

    def lookup_proposal(
        self, scope: ProposalRequestScope, *, deadline: float
    ) -> AuthorityRecord | None:
        if scope.owner_instance_id != self.instance_id:
            raise ReviewFault("execution_target_mismatch")

        def read(end: float) -> AuthorityRecord | None:
            with self._adapter._transaction(end, write=False) as connection:
                return postgres_inspection.lookup_proposal(connection, scope)

        return self._review(read, deadline)

    def list_executions(self, scope: ExecutionListScope, *, deadline: float) -> FreshExecutionPage:
        if scope.owner_instance_id != self.instance_id:
            raise ReviewFault("execution_target_mismatch")

        def read(end: float) -> FreshExecutionPage:
            with self._adapter._transaction(end, write=False) as connection:
                return postgres_inspection.list_executions(connection, scope)

        return self._review(read, deadline)

    def record(self, event: DockerLaunchEvent, *, deadline: float) -> None:
        try:
            self._call(lambda end: self._adapter.record(event, deadline=end), deadline, write=True)
        except StorageFault as error:
            raise DockerBackendFault("docker_journal_unconfirmed") from error

    def complete_execution(
        self, completion: DockerCompletion, *, deadline: float
    ) -> DockerExecutionResult:
        try:
            return self._call(
                lambda end: self._adapter.results.complete_execution(completion, deadline=end),
                deadline,
                write=True,
            )
        except StorageFault as error:
            raise DockerBackendFault("docker_journal_unconfirmed") from error

    def submit_cancel(
        self, call: ExecutionCancelCall, *, deadline: float
    ) -> Future[ExecutionStopRecord]:
        def stop(end: float) -> ExecutionStopRecord:
            with self._adapter._transaction(end, write=False) as connection:
                facts = find_authority(connection, call.approval_id)
            request = ExecutionStopRequest(
                request_id=call.request_id,
                approval_id=call.approval_id,
                project_id=facts.project_id,
                canonical_root=facts.canonical_root,
                owner_instance_id=self.instance_id,
                manifest_digest=call.reviewed_manifest_digest,
            )
            return self._adapter.results.request_stop(request, deadline=end)

        try:
            return self._submit(stop, deadline, write=True, control=True)
        except StorageFault as error:
            raise ReviewFault(error.code) from None

    def request_stop(
        self, request: ExecutionStopRequest, *, deadline: float
    ) -> ExecutionStopRecord:
        return self._review(
            lambda end: self._adapter.results.request_stop(request, deadline=end),
            deadline,
            write=True,
        )

    def inspect_stop(
        self, scope: ExecutionInspectionScope, *, deadline: float
    ) -> ExecutionStopRecord | None:
        return self._review(
            lambda end: self._adapter.results.inspect_stop(scope, deadline=end), deadline
        )

    def finish_stop(
        self, observation: ExecutionStopObservation, *, deadline: float
    ) -> ExecutionStopRecord:
        return self._review(
            lambda end: self._adapter.results.finish_stop(observation, deadline=end),
            deadline,
            write=True,
        )

    def finish_stop_with_capture(
        self,
        observation: ExecutionStopObservation,
        capture: "DockerOutputSnapshot",
        *,
        deadline: float,
    ) -> ExecutionStopRecord:
        return self._review(
            lambda end: self._adapter.results.finish_stop(
                observation, deadline=end, capture=capture
            ),
            deadline,
            write=True,
        )

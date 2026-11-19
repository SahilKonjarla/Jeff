"""Application ports for reviews, verification and exact-owned execution control."""

from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from jeff.domain.projects import Project

from .admission import ExecutionAdmission
from .authority import AuthorityOperation, AuthorityRecord, AuthorityResult, ProposalRequestScope
from .completion import DockerCompletion, DockerExecutionResult
from .docker_target import DockerCLIIdentity, DockerTargetDeclaration, VerifiedDockerTarget
from .guard_execution import GuardedExecutionManifest
from .guard_protocol_v2 import GuardV2Terminal
from .inspection import ExecutionInspection
from .launch import DockerLaunchEvent, DockerLaunchIntent, DockerOwnedContainer
from .manifests import ExecutionManifest
from .output import DockerJobOutcome, DockerOutputSnapshot
from .prepared_inputs import PreparedInputBinding, PreparedInputs
from .replies import ExecutionCancelReply, FreshExecutionReply
from .requests import (
    ExecutionCancelCall,
    ExecutionInspectionScope,
    ExecutionListScope,
    ExecutionLivePhase,
    FreshExecutionCall,
)
from .review_models import ExecutionCall, ExecutionData, ExecutionReply, ReviewOperation
from .stop_records import ExecutionStopObservation, ExecutionStopRecord, ExecutionStopRequest
from .stop_signal import ExecutionStopSignal
from .views import FreshExecutionPage


class ReviewStore(Protocol):
    def review(self, operation: ReviewOperation, *, deadline: float) -> ExecutionData: ...


class ExecutionClient(Protocol):
    def review(self, call: ExecutionCall) -> ExecutionReply: ...


class AuthorityStore(Protocol):
    def authority(self, operation: AuthorityOperation, *, deadline: float) -> AuthorityResult: ...


class ProposalLookupStore(Protocol):
    def lookup_proposal(
        self, scope: ProposalRequestScope, *, deadline: float
    ) -> AuthorityRecord | None: ...


class DockerTargetVerifier(Protocol):
    def verify_target(
        self, declaration: DockerTargetDeclaration, *, deadline: float
    ) -> VerifiedDockerTarget: ...


class PreparedInputPreparer(Protocol):
    def prepare(
        self, project: Project, paths: tuple[str, ...], *, deadline: float
    ) -> PreparedInputs: ...


class PreparedInputVerifier(Protocol):
    def verify(self, manifest: ExecutionManifest, *, deadline: float) -> PreparedInputBinding: ...
    def check(
        self, binding: PreparedInputBinding, manifest: ExecutionManifest, *, deadline: float
    ) -> None: ...


class DockerLaunchJournal(Protocol):
    """Must durably validate transition/claim uniqueness before returning; ACK loss raises.

    Enforce current owner/scope/manifest binding, original expiry at create/start,
    and permanent one-attempt exclusion. Only the SQL owner implements durability;
    no default/fallback journal or public route is granted by this port. Cleanup facts
    alone do not release admission before completion/output integration exists.
    """

    def record(self, event: DockerLaunchEvent, *, deadline: float) -> None: ...


class DockerOutputSink(Protocol):
    """Trusted bounded, nonblocking memory-only filter. Never persist raw input chunks."""

    @property
    def retention_limit(self) -> int: ...

    def accept(self, stream: Literal["stdout", "stderr"], data: bytes) -> None: ...
    def finish(self, stream: Literal["stdout", "stderr"]) -> None: ...


class DockerOutputCapture(DockerOutputSink, Protocol):
    def seal(self, *, abort: bool = False) -> DockerOutputSnapshot: ...


class DockerCompletionStore(Protocol):
    def complete_execution(
        self, completion: DockerCompletion, *, deadline: float
    ) -> DockerExecutionResult: ...


class DockerExecutionController(Protocol):
    """Owned lifecycle interface for internal orchestration, not public handle adoption."""

    def create(
        self,
        claim: AuthorityResult,
        declaration: DockerTargetDeclaration,
        *,
        deadline: float,
        cleanup_deadline: float,
    ) -> DockerOwnedContainer: ...
    def run(
        self,
        handle: DockerOwnedContainer,
        sink: DockerOutputSink,
        *,
        deadline: float,
        cleanup_deadline: float,
    ) -> DockerJobOutcome: ...
    def complete(
        self,
        handle: DockerOwnedContainer,
        capture: DockerOutputCapture,
        store: DockerCompletionStore,
        *,
        deadline: float,
    ) -> DockerExecutionResult: ...
    def terminate_remove_owned(self, handle: DockerOwnedContainer, *, deadline: float) -> bool: ...


class GuardExecutionVerifier(Protocol):
    def prepare(
        self, original: ExecutionManifest, *, deadline: float
    ) -> GuardedExecutionManifest: ...
    def check(self, action: GuardedExecutionManifest, *, deadline: float) -> None: ...


@runtime_checkable
class GuardedDockerBackend(Protocol):
    def attach_guarded(
        self,
        handle: DockerOwnedContainer,
        sink: DockerOutputSink,
        stop: ExecutionStopSignal | None,
        observe_running: Callable[[], None] | None,
        *,
        deadline: float,
    ) -> tuple[int, GuardV2Terminal]: ...
    def release_guarded(self, handle: DockerOwnedContainer, *, deadline: float) -> bool: ...


class DockerLifecycleBackend(Protocol):
    """Installation-internal Docker I/O seam; never caller-supplied CLI arguments."""

    identity: DockerCLIIdentity

    def engine_id(self, endpoint: str, *, deadline: float) -> str: ...
    def create(self, intent: DockerLaunchIntent, *, deadline: float) -> str: ...
    def inspect(
        self, endpoint: str, container_id: str, *, deadline: float
    ) -> dict[str, object]: ...
    def attach(
        self, endpoint: str, container_id: str, sink: DockerOutputSink, *, deadline: float
    ) -> int: ...
    def stop(self, endpoint: str, container_id: str, *, deadline: float) -> None: ...
    def kill(self, endpoint: str, container_id: str, *, deadline: float) -> None: ...
    def remove(self, endpoint: str, container_id: str, *, deadline: float) -> None: ...
    def absent(self, endpoint: str, container_id: str, *, deadline: float) -> bool: ...


class ExecutionStarter(Protocol):
    """Owned-worker seam, not a callable endpoint or a caller-lifetime executor."""

    def start(
        self,
        directory: Path,
        approval_id: UUID,
        expected_manifest_digest: str,
        *,
        request_id: UUID,
        admission_deadline: float,
        on_admitted: Callable[[ExecutionAdmission], None],
    ) -> "DockerExecutionResult | AuthorityRecord | ExecutionStopRecord": ...


class ExecutionCancelClient(Protocol):
    def cancel(self, call: ExecutionCancelCall) -> ExecutionCancelReply: ...


class FreshExecutionClient(Protocol):
    def fresh(self, call: FreshExecutionCall) -> FreshExecutionReply: ...


class FreshExecutionService(ExecutionStarter, Protocol):
    def execute(self, call: FreshExecutionCall, deadline: float) -> FreshExecutionReply: ...


class ExecutionListReader(Protocol):
    def list_executions(
        self, scope: ExecutionListScope, *, deadline: float
    ) -> FreshExecutionPage: ...


class ExecutionResultReader(Protocol):
    def inspect_execution(
        self, scope: ExecutionInspectionScope, *, deadline: float
    ) -> ExecutionInspection: ...


@runtime_checkable
class CancellableDockerVerifier(Protocol):
    def verify_target_cancellable(
        self, declaration: DockerTargetDeclaration, stop: ExecutionStopSignal, *, deadline: float
    ) -> VerifiedDockerTarget: ...


@runtime_checkable
class CancellableDockerBackend(Protocol):
    def attach_cancellable(
        self,
        endpoint: str,
        container_id: str,
        sink: DockerOutputSink,
        stop: ExecutionStopSignal,
        observe_running: Callable[[], None],
        *,
        deadline: float,
    ) -> int: ...
    def stop_gracefully(
        self, endpoint: str, container_id: str, *, grace_seconds: int, deadline: float
    ) -> None: ...


@runtime_checkable
class CancellableDockerController(Protocol):
    def create(
        self,
        claim: AuthorityResult,
        declaration: DockerTargetDeclaration,
        *,
        deadline: float,
        cleanup_deadline: float,
        stop: ExecutionStopSignal | None = None,
    ) -> DockerOwnedContainer: ...
    def run(
        self,
        handle: DockerOwnedContainer,
        sink: DockerOutputSink,
        *,
        deadline: float,
        cleanup_deadline: float,
        stop: ExecutionStopSignal | None = None,
    ) -> DockerJobOutcome: ...
    def finish_stop_owned(
        self,
        handle: DockerOwnedContainer,
        capture: DockerOutputCapture,
        stop: ExecutionStopSignal,
        store: "ExecutionStopStore",
    ) -> ExecutionStopRecord: ...


@runtime_checkable
class ExecutionCancellationStore(Protocol):
    def submit_cancel(
        self, call: ExecutionCancelCall, *, deadline: float
    ) -> Future[ExecutionStopRecord]: ...


@runtime_checkable
class FreshExecutionControl(Protocol):
    @property
    def shutdown_confirmed(self) -> bool: ...
    @property
    def cancellation_available(self) -> bool: ...
    def submit_cancel(
        self, call: ExecutionCancelCall, *, deadline: float
    ) -> Future[ExecutionStopRecord]: ...
    def begin_shutdown(self, deadline: float) -> None: ...
    def execution_phase(self, approval_id: UUID) -> ExecutionLivePhase | None: ...


class ExecutionStopStore(Protocol):
    def request_stop(
        self, request: ExecutionStopRequest, *, deadline: float
    ) -> ExecutionStopRecord: ...

    def inspect_stop(
        self, scope: ExecutionInspectionScope, *, deadline: float
    ) -> ExecutionStopRecord | None: ...

    def finish_stop(
        self, observation: ExecutionStopObservation, *, deadline: float
    ) -> ExecutionStopRecord: ...

    def finish_stop_with_capture(
        self,
        observation: ExecutionStopObservation,
        capture: DockerOutputSnapshot,
        *,
        deadline: float,
    ) -> ExecutionStopRecord: ...

"""Verified fresh execution use cases behind explicit routing; no implicit approval.

Calls are synchronous and serialized without queueing. Job deadline permits explicit
owned cleanup≤10s and completion≤5s afterward; filesystem cancellation is cooperative.
Only bootstrap supplies adapters; importing this module performs no external I/O."""

import threading
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from uuid import UUID

from jeff.application.execution import (
    AuthorityRecord,
    AuthorityStore,
    DockerCompletionStore,
    DockerExecutionController,
    DockerExecutionResult,
    DockerOutputCapture,
    DockerTargetDeclaration,
    DockerTargetVerifier,
    ExecutionAdmission,
    ExecutionCancelCall,
    ExecutionListReader,
    ExecutionLivePhase,
    ExecutionProfile,
    ExecutionResultReader,
    ExecutionStopRecord,
    ExecutionStopSignal,
    ExecutionStopStore,
    FreshExecutionCall,
    FreshExecutionReply,
    GuardExecutionVerifier,
    PreparedInputPreparer,
    PreparedInputVerifier,
    ProposalLookupStore,
)
from jeff.application.execution.log_tail import LiveLogTail
from jeff.application.execution.watch import WatchStore
from jeff.application.projects import ProjectService
from jeff.application.storage import ProjectRegistry

from . import admission, cancellation, proposal, requests


class ExecutionCoordinator:
    """Bind reviewed inputs to one locally confirmed claim and its owned execution.

    Persisted records and public acknowledgements are observations, not reusable
    launch permission. Storage, target verification and container I/O stay behind
    supplied ports; the coordinator never constructs an adapter or retries work.
    """

    def __init__(
        self,
        projects: ProjectService,
        registry: ProjectRegistry,
        authority: AuthorityStore,
        preparer: PreparedInputPreparer,
        inputs: PreparedInputVerifier,
        verifier: DockerTargetVerifier,
        controller: DockerExecutionController,
        results: DockerCompletionStore,
        capture: Callable[[int], DockerOutputCapture],
        *,
        profiles: Callable[[], tuple[ExecutionProfile, ...]],
        declarations: tuple[DockerTargetDeclaration, ...],
        instance_id: UUID,
        proposal_lookup: ProposalLookupStore | None = None,
        inspection: ExecutionResultReader | None = None,
        listing: ExecutionListReader | None = None,
        stop_store: ExecutionStopStore | None = None,
        guards: GuardExecutionVerifier | None = None,
        require_guard: bool = False,
        pytest_capture: Callable[[int, str, tuple[str, ...]], DockerOutputCapture] | None = None,
        attach_log_tail: Callable[[DockerOutputCapture, LiveLogTail], None] | None = None,
        watch_store: WatchStore | None = None,
    ) -> None:
        self.projects, self.registry, self.authority = (projects, registry, authority)
        self.preparer, self.inputs, self.verifier = (preparer, inputs, verifier)
        self.controller, self.results, self.capture = (controller, results, capture)
        self.pytest_capture = pytest_capture
        self.attach_log_tail = attach_log_tail
        self.watch_store = watch_store
        self._log_tail: tuple[UUID, LiveLogTail] | None = None
        self.profiles, self.declarations, self.instance_id = (profiles, declarations, instance_id)
        self.proposal_lookup = proposal_lookup
        self.inspection = inspection
        self.listing = listing
        self._mutex = threading.Lock()
        self.stop_store = stop_store
        self.guards, self.require_guard = guards, require_guard
        self._control_mutex = threading.Lock()
        self._live: ExecutionStopSignal | None = None
        self._shutdown_deadline: float | None = None
        self._execution_uncertain = False

    @property
    def shutdown_confirmed(self) -> bool:
        return cancellation.shutdown_confirmed(self)

    @property
    def cancellation_available(self) -> bool:
        return cancellation.cancellation_available(self)

    def submit_cancel(
        self, call: ExecutionCancelCall, *, deadline: float
    ) -> Future[ExecutionStopRecord]:
        return cancellation.submit_cancel(self, call, deadline=deadline)

    def begin_shutdown(self, deadline: float) -> None:
        return cancellation.begin_shutdown(self, deadline)

    def execution_phase(self, approval_id: UUID) -> ExecutionLivePhase | None:
        return cancellation.execution_phase(self, approval_id)

    def execute(self, call: FreshExecutionCall, deadline: float) -> FreshExecutionReply:
        """Public intent gateway. Starts must use the existing owned-worker observer seam."""
        return requests.execute(self, call, deadline)

    def propose(
        self,
        directory: Path,
        profile_name: str,
        argv: tuple[str, ...],
        paths: tuple[str, ...],
        *,
        deadline: float,
        request_id: UUID | None = None,
    ) -> AuthorityRecord:
        """Prepare literal inputs once; replay returns the original reviewed facts.

        Serialization is local to this coordinator, not a durable preparation
        reservation. Failed or cross-instance preparation can leave bounded residue.
        """
        return proposal.propose(
            self, directory, profile_name, argv, paths, deadline=deadline, request_id=request_id
        )

    def decide(
        self,
        directory: Path,
        approval_id: UUID,
        expected_manifest_digest: str,
        *,
        approve: bool,
        deadline: float,
        request_id: UUID | None = None,
    ) -> AuthorityRecord:
        return proposal.decide(
            self,
            directory,
            approval_id,
            expected_manifest_digest,
            approve=approve,
            deadline=deadline,
            request_id=request_id,
        )

    def start(
        self,
        directory: Path,
        approval_id: UUID,
        expected_manifest_digest: str,
        *,
        request_id: UUID,
        admission_deadline: float,
        on_admitted: Callable[[ExecutionAdmission], None],
    ) -> DockerExecutionResult | AuthorityRecord | ExecutionStopRecord:
        """Owned-worker entry: acknowledge a committed claim, then continue locally.

        The observer receives no permission. Its failure cannot cancel admitted
        work. This method remains synchronous for its owning worker; no thread,
        queue, RPC handler or automatic retry is created here. Replays return facts.
        """
        return admission.start(
            self,
            directory,
            approval_id,
            expected_manifest_digest,
            request_id=request_id,
            admission_deadline=admission_deadline,
            on_admitted=on_admitted,
        )

    def run(
        self, directory: Path, approval_id: UUID, expected_manifest_digest: str, *, deadline: float
    ) -> DockerExecutionResult:
        """Run synchronously for internal callers without publishing admission early.

        Public starts use ``start`` so the owning worker outlives its admission
        observer. Neither entry point can reconstruct a lost first-claim token.
        """
        return admission.run(
            self, directory, approval_id, expected_manifest_digest, deadline=deadline
        )

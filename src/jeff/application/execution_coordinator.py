"""Verified fresh execution use cases behind explicit routing; no implicit approval.

Calls are synchronous and serialized without queueing. Job deadline permits explicit
owned cleanup≤10s and completion≤5s afterward; filesystem cancellation is cooperative.
Only bootstrap supplies adapters; importing this module performs no external I/O.
"""

import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from jeff.application.execution import (
    AuthorityOperation,
    AuthorityRecord,
    AuthorityResult,
    AuthorityStore,
    DockerBackendFault,
    DockerCompletionStore,
    DockerExecutionController,
    DockerExecutionResult,
    DockerOutputCapture,
    DockerTargetDeclaration,
    DockerTargetVerifier,
    ExecutionAdmission,
    ExecutionInspectionScope,
    ExecutionManifest,
    ExecutionProfile,
    ExecutionResultReader,
    FreshExecutionCall,
    FreshExecutionCompletion,
    FreshExecutionReply,
    FreshExecutionView,
    PreparedInputFault,
    PreparedInputPreparer,
    PreparedInputs,
    PreparedInputVerifier,
    ProposalLookupStore,
    ProposalRequestScope,
    ReviewFault,
    absolute_path,
    canonical_prepared_paths,
    fresh_execution_failure,
)
from jeff.application.projects import ProjectService
from jeff.application.storage import ProjectRegistry, StorageFault
from jeff.domain.execution import AuthorityState
from jeff.domain.projects import Project


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
    ) -> None:
        self.projects, self.registry, self.authority = projects, registry, authority
        self.preparer, self.inputs, self.verifier = preparer, inputs, verifier
        self.controller, self.results, self.capture = controller, results, capture
        self.profiles, self.declarations, self.instance_id = profiles, declarations, instance_id
        self.proposal_lookup = proposal_lookup
        self.inspection = inspection
        self._mutex = threading.Lock()

    @staticmethod
    def _deadline(deadline: float) -> None:
        if not math.isfinite(deadline) or deadline <= time.monotonic():
            raise DockerBackendFault("docker_timeout")

    @contextmanager
    def _active(self, deadline: float) -> Iterator[None]:
        if not self._mutex.acquire(blocking=False):
            raise DockerBackendFault("docker_busy")
        try:
            self._deadline(deadline)
            yield
        except (DockerBackendFault, PreparedInputFault, ReviewFault, StorageFault):
            raise
        except Exception:
            raise DockerBackendFault("docker_policy_mismatch") from None
        finally:
            self._mutex.release()

    def _project(self, directory: Path, deadline: float) -> Project:
        absolute_path(directory)
        observed = self.projects.execute("config", directory, deadline=deadline)
        if not observed.ok:
            raise DockerBackendFault("docker_authority_required")
        project = Project(UUID(observed.data.project_id), Path(observed.data.root))
        if self.registry.lookup(project, deadline=deadline) != project:
            raise DockerBackendFault("docker_authority_required")
        return project

    def _profile(self, project: Project, name: str) -> ExecutionProfile:
        matches = [
            p
            for p in self.profiles()
            if p.name == name
            and p.project_id == project.project_id
            and p.canonical_root == project.root
        ]
        if len(matches) != 1:
            raise DockerBackendFault("docker_authority_required")
        return matches[0]

    def _declaration(self, profile: ExecutionProfile) -> DockerTargetDeclaration:
        matches = [d for d in self.declarations if d.profile == profile]
        if len(matches) != 1:
            raise DockerBackendFault("docker_target_mismatch")
        return matches[0]

    def _target(self, declaration: DockerTargetDeclaration, deadline: float) -> None:
        target = self.verifier.verify_target(declaration, deadline=deadline)
        p = declaration.profile
        if (
            target.declaration_digest != declaration.fingerprint
            or target.profile_digest != p.fingerprint
            or (target.endpoint, target.engine_id, target.image_id, target.platform)
            != (p.endpoint, p.engine_id, p.image_id, p.platform)
        ):
            raise DockerBackendFault("docker_target_mismatch")

    def _operation(self, project: Project, **values: object) -> AuthorityOperation:
        return AuthorityOperation.model_validate(
            {
                "project_id": project.project_id,
                "canonical_root": project.root,
                "owner_instance_id": self.instance_id,
                **values,
            }
        )

    def _load(
        self, project: Project, approval_id: UUID, expected: str, deadline: float
    ) -> AuthorityRecord:
        record = self.authority.authority(
            self._operation(project, operation="show", approval_id=approval_id), deadline=deadline
        ).record
        if (
            record.owner_instance_id != self.instance_id
            or record.manifest_digest != expected
            or record.project_id != project.project_id
            or record.canonical_root != project.root
        ):
            raise DockerBackendFault("docker_authority_required")
        return record

    def _checked(
        self, project: Project, record: AuthorityRecord, deadline: float
    ) -> DockerTargetDeclaration:
        profile = self._profile(project, record.manifest.profile.name)
        if profile != record.manifest.profile:
            raise DockerBackendFault("docker_target_mismatch")
        declaration = self._declaration(profile)
        self._target(declaration, deadline)
        binding = self.inputs.verify(record.manifest, deadline=deadline)
        if (
            binding.snapshot_id != record.manifest.inputs.snapshot_id
            or binding.inventory_digest != record.manifest.inputs.digest
        ):
            raise DockerBackendFault("docker_policy_mismatch")
        self.inputs.check(binding, record.manifest, deadline=deadline)
        self._deadline(deadline)
        return declaration

    def execute(self, call: FreshExecutionCall, deadline: float) -> FreshExecutionReply:
        """Public intent gateway. Starts must use the existing owned-worker observer seam."""
        try:
            self._deadline(deadline)
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
            elif call.operation in {"show", "logs"}:
                if self.inspection is None:
                    raise DockerBackendFault("docker_authority_required")
                assert call.approval_id is not None
                project = self._project(call.directory, deadline)
                observed = self.inspection.inspect_execution(
                    ExecutionInspectionScope(
                        project_id=project.project_id,
                        canonical_root=project.root,
                        owner_instance_id=self.instance_id,
                        approval_id=call.approval_id,
                    ),
                    deadline=deadline,
                )
                record = observed.record
                result = observed.result
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
                    output=result.output
                    if result is not None and call.operation == "logs"
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
        with self._active(deadline):
            project = self._project(directory, deadline)
            profile = self._profile(project, profile_name)
            # Validate public argv before allocating/copying a snapshot; dummy inventory
            # is never persisted or granted authority.
            ExecutionManifest(profile=profile, argv=argv, inputs=PreparedInputs())
            if request_id is not None:
                if self.proposal_lookup is None:
                    raise DockerBackendFault("docker_authority_required")
                try:
                    paths = canonical_prepared_paths(paths)
                except ValueError:
                    raise PreparedInputFault("input_invalid") from None
                prior = self.proposal_lookup.lookup_proposal(
                    ProposalRequestScope(
                        request_id=request_id,
                        project_id=project.project_id,
                        canonical_root=project.root,
                        owner_instance_id=self.instance_id,
                    ),
                    deadline=deadline,
                )
                if prior is not None:
                    if (
                        prior.manifest.profile.name != profile_name
                        or prior.manifest.argv != argv
                        or tuple(item.path for item in prior.manifest.inputs.files) != paths
                    ):
                        raise ReviewFault("execution_conflict")
                    # Replay original facts; refresh existing expiry without target
                    # queries, source reads, another snapshot or launch authority.
                    return self._load(project, prior.approval_id, prior.manifest_digest, deadline)
            declaration = self._declaration(profile)
            self._target(declaration, deadline)
            prepared = self.preparer.prepare(project, paths, deadline=deadline)
            manifest = ExecutionManifest(profile=profile, argv=argv, inputs=prepared)
            binding = self.inputs.verify(manifest, deadline=deadline)
            self.inputs.check(binding, manifest, deadline=deadline)
            self._deadline(deadline)
            return self.authority.authority(
                self._operation(
                    project,
                    operation="propose",
                    manifest=manifest,
                    current_manifest_digest=manifest.fingerprint,
                    **({"request_id": request_id} if request_id is not None else {}),
                ),
                deadline=deadline,
            ).record

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
        with self._active(deadline):
            if type(approve) is not bool:
                raise DockerBackendFault("docker_policy_mismatch")
            project = self._project(directory, deadline)
            record = self._load(project, approval_id, expected_manifest_digest, deadline)
            if (
                record.state not in (AuthorityState.PENDING, AuthorityState.APPROVED)
                and request_id is None
            ):
                raise DockerBackendFault("docker_authority_required")
            if approve and record.state in (AuthorityState.PENDING, AuthorityState.APPROVED):
                self._checked(project, record, deadline)
            return self.authority.authority(
                self._operation(
                    project,
                    operation="approve" if approve else "deny",
                    approval_id=approval_id,
                    current_manifest_digest=record.manifest.fingerprint if approve else None,
                    **({"request_id": request_id} if request_id is not None else {}),
                ),
                deadline=deadline,
            ).record

    def _execute_claim(
        self,
        claim: AuthorityResult,
        declaration: DockerTargetDeclaration,
        capture: DockerOutputCapture,
        job_deadline: float,
        cleanup_deadline: float,
    ) -> DockerExecutionResult:
        # Only the local first confirmed response reaches this mutation path.
        handle = self.controller.create(
            claim, declaration, deadline=job_deadline, cleanup_deadline=cleanup_deadline
        )
        try:
            self.controller.run(
                handle, capture, deadline=job_deadline, cleanup_deadline=cleanup_deadline
            )
        except Exception:
            if not self.controller.terminate_remove_owned(handle, deadline=cleanup_deadline):
                raise DockerBackendFault("docker_mutation_unconfirmed") from None
            raise
        return self.controller.complete(
            handle,
            capture,
            self.results,
            deadline=min(time.monotonic() + 5, job_deadline + 15),
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
    ) -> DockerExecutionResult | AuthorityRecord:
        """Owned-worker entry: acknowledge a committed claim, then continue locally.

        The observer receives no permission. Its failure cannot cancel admitted
        work. This method remains synchronous for its owning worker; no thread,
        queue, RPC handler or automatic retry is created here. Replays return facts.
        """
        with self._active(admission_deadline):
            if not callable(on_admitted):
                raise DockerBackendFault("docker_authority_required")
            project = self._project(directory, admission_deadline)
            record = self._load(project, approval_id, expected_manifest_digest, admission_deadline)
            if record.state not in (AuthorityState.APPROVED, AuthorityState.CONSUMED):
                raise DockerBackendFault("docker_authority_required")
            capture: DockerOutputCapture | None = None
            try:
                job_deadline = time.monotonic() + record.manifest.profile.timeout_seconds
                claim_deadline = min(admission_deadline, job_deadline)
                declaration: DockerTargetDeclaration | None = None
                if record.state == AuthorityState.APPROVED:
                    declaration = self._checked(project, record, claim_deadline)
                    capture = self.capture(record.manifest.profile.output_bytes)
                    if (
                        type(capture.retention_limit) is not int
                        or capture.retention_limit != record.manifest.profile.output_bytes
                    ):
                        raise DockerBackendFault("docker_unsafe_configuration")
                claim = self.authority.authority(
                    self._operation(
                        project,
                        operation="claim",
                        request_id=request_id,
                        approval_id=approval_id,
                        current_manifest_digest=record.manifest.fingerprint,
                    ),
                    deadline=claim_deadline,
                )
                if (
                    claim.record.approval_id != record.approval_id
                    or claim.record.manifest != record.manifest
                    or claim.record.owner_instance_id != self.instance_id
                    or claim.record.state != AuthorityState.CONSUMED
                ):
                    raise DockerBackendFault("docker_authority_required")
                replayed = claim.launch_claim is None
                if not replayed and (
                    declaration is None
                    or capture is None
                    or claim.launch_claim != claim.record.claim_id
                    or claim.record.claim_state != "claimed"
                ):
                    raise DockerBackendFault("docker_authority_required")
                observation = ExecutionAdmission.model_validate(
                    {
                        "request_id": request_id,
                        "project_id": project.project_id,
                        "canonical_root": project.root,
                        "approval_id": claim.record.approval_id,
                        "job_id": claim.record.job_id,
                        "manifest_digest": claim.record.manifest_digest,
                        "claim_state": claim.record.claim_state,
                        "replayed": replayed,
                    }
                )
                try:
                    on_admitted(observation)
                except Exception:
                    pass  # Observer delivery is not worker ownership or cancellation.
                if replayed:
                    if capture is not None:
                        capture.seal(abort=True)
                    return claim.record
                if capture is None or declaration is None:
                    raise DockerBackendFault("docker_authority_required")
                return self._execute_claim(
                    claim, declaration, capture, job_deadline, job_deadline + 10
                )
            except Exception:
                if capture is not None:
                    try:
                        capture.seal(abort=True)
                    except Exception:
                        pass
                raise

    def run(
        self, directory: Path, approval_id: UUID, expected_manifest_digest: str, *, deadline: float
    ) -> DockerExecutionResult:
        """Run synchronously for internal callers without publishing admission early.

        Public starts use ``start`` so the owning worker outlives its admission
        observer. Neither entry point can reconstruct a lost first-claim token.
        """
        with self._active(deadline):
            project = self._project(directory, deadline)
            record = self._load(project, approval_id, expected_manifest_digest, deadline)
            if record.state != AuthorityState.APPROVED:
                raise DockerBackendFault("docker_authority_required")
            declaration = self._checked(project, record, deadline)
            job_deadline = min(deadline, time.monotonic() + record.manifest.profile.timeout_seconds)
            cleanup_deadline = job_deadline + 10
            capture = self.capture(record.manifest.profile.output_bytes)
            try:
                if (
                    type(capture.retention_limit) is not int
                    or capture.retention_limit != record.manifest.profile.output_bytes
                ):
                    raise DockerBackendFault("docker_unsafe_configuration")
                self._deadline(job_deadline)
                claim = self.authority.authority(
                    self._operation(
                        project,
                        operation="claim",
                        approval_id=approval_id,
                        current_manifest_digest=record.manifest.fingerprint,
                    ),
                    deadline=job_deadline,
                )
                if (
                    claim.record.approval_id != record.approval_id
                    or claim.record.manifest != record.manifest
                    or claim.record.owner_instance_id != self.instance_id
                ):
                    raise DockerBackendFault("docker_authority_required")
                if claim.launch_claim is None:
                    raise DockerBackendFault("docker_authority_required")
                # Never cache/return the ephemeral first-response launch permission.
                return self._execute_claim(
                    claim, declaration, capture, job_deadline, cleanup_deadline
                )
            except Exception:
                try:
                    capture.seal(abort=True)
                except Exception:
                    pass
                raise

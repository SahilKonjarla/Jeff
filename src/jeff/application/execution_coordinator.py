"""Trusted internal fresh execution interface. No public routing or implicit approval.

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
    AuthorityStore,
    DockerBackendFault,
    DockerCompletionStore,
    DockerExecutionController,
    DockerExecutionResult,
    DockerOutputCapture,
    DockerTargetDeclaration,
    DockerTargetVerifier,
    ExecutionManifest,
    ExecutionProfile,
    PreparedInputFault,
    PreparedInputPreparer,
    PreparedInputs,
    PreparedInputVerifier,
    ReviewFault,
    absolute_path,
)
from jeff.application.projects import ProjectService
from jeff.application.storage import ProjectRegistry, StorageFault
from jeff.domain.execution import AuthorityState
from jeff.domain.projects import Project


class ExecutionCoordinator:
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
    ) -> None:
        self.projects, self.registry, self.authority = projects, registry, authority
        self.preparer, self.inputs, self.verifier = preparer, inputs, verifier
        self.controller, self.results, self.capture = controller, results, capture
        self.profiles, self.declarations, self.instance_id = profiles, declarations, instance_id
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

    def propose(
        self,
        directory: Path,
        profile_name: str,
        argv: tuple[str, ...],
        paths: tuple[str, ...],
        *,
        deadline: float,
    ) -> AuthorityRecord:
        with self._active(deadline):
            project = self._project(directory, deadline)
            profile = self._profile(project, profile_name)
            # Validate public argv before allocating/copying a snapshot; dummy inventory
            # is never persisted or granted authority.
            ExecutionManifest(profile=profile, argv=argv, inputs=PreparedInputs())
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
    ) -> AuthorityRecord:
        with self._active(deadline):
            if type(approve) is not bool:
                raise DockerBackendFault("docker_policy_mismatch")
            project = self._project(directory, deadline)
            record = self._load(project, approval_id, expected_manifest_digest, deadline)
            if record.state not in (AuthorityState.PENDING, AuthorityState.APPROVED):
                raise DockerBackendFault("docker_authority_required")
            if approve:
                self._checked(project, record, deadline)
            return self.authority.authority(
                self._operation(
                    project,
                    operation="approve" if approve else "deny",
                    approval_id=approval_id,
                    current_manifest_digest=record.manifest.fingerprint if approve else None,
                ),
                deadline=deadline,
            ).record

    def run(
        self, directory: Path, approval_id: UUID, expected_manifest_digest: str, *, deadline: float
    ) -> DockerExecutionResult:
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
                handle = self.controller.create(
                    claim, declaration, deadline=job_deadline, cleanup_deadline=cleanup_deadline
                )
                try:
                    self.controller.run(
                        handle, capture, deadline=job_deadline, cleanup_deadline=cleanup_deadline
                    )
                except Exception:
                    # Includes expiry in the create→run gap. Only this live handle;
                    # no guessed intent/resource adoption or second execution.
                    if not self.controller.terminate_remove_owned(
                        handle, deadline=cleanup_deadline
                    ):
                        raise DockerBackendFault("docker_mutation_unconfirmed") from None
                    raise
                return self.controller.complete(
                    handle,
                    capture,
                    self.results,
                    deadline=min(time.monotonic() + 5, job_deadline + 15),
                )
            except Exception:
                try:
                    capture.seal(abort=True)
                except Exception:
                    pass
                raise

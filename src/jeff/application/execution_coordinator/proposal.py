"""Exact proposal preparation, facts-only replay and explicit review decisions."""

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from jeff.application.execution import (
    AuthorityRecord,
    DockerBackendFault,
    ExecutionManifest,
    PreparedInputFault,
    PreparedInputs,
    ProposalRequestScope,
    ReviewFault,
    canonical_prepared_paths,
)
from jeff.domain.execution import AuthorityState

from .binding import (
    _active,
    _checked,
    _deadline,
    _declaration,
    _load,
    _operation,
    _profile,
    _project,
    _target,
)

if TYPE_CHECKING:
    from .coordinator import ExecutionCoordinator


def propose(
    self: "ExecutionCoordinator",
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
    with _active(self, deadline):
        project = _project(self, directory, deadline)
        profile = _profile(self, project, profile_name)
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
                return _load(self, project, prior.approval_id, prior.manifest_digest, deadline)
        if self.require_guard and self.guards is None:
            raise DockerBackendFault("docker_unsafe_configuration")
        declaration = _declaration(self, profile)
        _target(self, declaration, deadline)
        prepared = self.preparer.prepare(project, paths, deadline=deadline)
        manifest = ExecutionManifest(profile=profile, argv=argv, inputs=prepared)
        binding = self.inputs.verify(manifest, deadline=deadline)
        self.inputs.check(binding, manifest, deadline=deadline)
        action = self.guards.prepare(manifest, deadline=deadline) if self.guards else manifest
        _deadline(deadline)
        return self.authority.authority(
            _operation(
                self,
                project,
                operation="propose",
                manifest=action,
                current_manifest_digest=action.fingerprint,
                **({"request_id": request_id} if request_id is not None else {}),
            ),
            deadline=deadline,
        ).record


def decide(
    self: "ExecutionCoordinator",
    directory: Path,
    approval_id: UUID,
    expected_manifest_digest: str,
    *,
    approve: bool,
    deadline: float,
    request_id: UUID | None = None,
) -> AuthorityRecord:
    with _active(self, deadline):
        if type(approve) is not bool:
            raise DockerBackendFault("docker_policy_mismatch")
        project = _project(self, directory, deadline)
        record = _load(self, project, approval_id, expected_manifest_digest, deadline)
        if (
            record.state not in (AuthorityState.PENDING, AuthorityState.APPROVED)
            and request_id is None
        ):
            raise DockerBackendFault("docker_authority_required")
        if approve and record.state in (AuthorityState.PENDING, AuthorityState.APPROVED):
            _checked(self, project, record, deadline)
        return self.authority.authority(
            _operation(
                self,
                project,
                operation="approve" if approve else "deny",
                approval_id=approval_id,
                current_manifest_digest=record.manifest.fingerprint if approve else None,
                **({"request_id": request_id} if request_id is not None else {}),
            ),
            deadline=deadline,
        ).record

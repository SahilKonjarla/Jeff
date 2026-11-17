"""Scope, profile and target validation; one nonblocking action reservation."""

import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from jeff.application.execution import (
    AuthorityOperation,
    AuthorityRecord,
    CancellableDockerVerifier,
    DockerBackendFault,
    DockerTargetDeclaration,
    ExecutionProfile,
    ExecutionStopSignal,
    PreparedInputFault,
    ReviewFault,
    absolute_path,
)
from jeff.application.storage import StorageFault
from jeff.domain.projects import Project

if TYPE_CHECKING:
    from .coordinator import ExecutionCoordinator


def _deadline(deadline: float) -> None:
    if not math.isfinite(deadline) or deadline <= time.monotonic():
        raise DockerBackendFault("docker_timeout")


@contextmanager
def _active(self: "ExecutionCoordinator", deadline: float) -> Iterator[None]:
    if not self._mutex.acquire(blocking=False):
        raise DockerBackendFault("docker_busy")
    try:
        _deadline(deadline)
        yield
    except (DockerBackendFault, PreparedInputFault, ReviewFault, StorageFault):
        raise
    except Exception:
        raise DockerBackendFault("docker_policy_mismatch") from None
    finally:
        self._mutex.release()


def _project(self: "ExecutionCoordinator", directory: Path, deadline: float) -> Project:
    absolute_path(directory)
    observed = self.projects.execute("config", directory, deadline=deadline)
    if not observed.ok:
        raise DockerBackendFault("docker_authority_required")
    project = Project(UUID(observed.data.project_id), Path(observed.data.root))
    if self.registry.lookup(project, deadline=deadline) != project:
        raise DockerBackendFault("docker_authority_required")
    return project


def _profile(self: "ExecutionCoordinator", project: Project, name: str) -> ExecutionProfile:
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


def _declaration(
    self: "ExecutionCoordinator", profile: ExecutionProfile
) -> DockerTargetDeclaration:
    matches = [d for d in self.declarations if d.profile == profile]
    if len(matches) != 1:
        raise DockerBackendFault("docker_target_mismatch")
    return matches[0]


def _target(
    self: "ExecutionCoordinator",
    declaration: DockerTargetDeclaration,
    deadline: float,
    stop: ExecutionStopSignal | None = None,
) -> None:
    if stop is not None:
        if not isinstance(self.verifier, CancellableDockerVerifier):
            raise DockerBackendFault("docker_unsafe_configuration")
        target = self.verifier.verify_target_cancellable(declaration, stop, deadline=deadline)
    else:
        target = self.verifier.verify_target(declaration, deadline=deadline)
    p = declaration.profile
    if (
        target.declaration_digest != declaration.fingerprint
        or target.profile_digest != p.fingerprint
        or (target.endpoint, target.engine_id, target.image_id, target.platform)
        != (p.endpoint, p.engine_id, p.image_id, p.platform)
    ):
        raise DockerBackendFault("docker_target_mismatch")


def _operation(
    self: "ExecutionCoordinator", project: Project, **values: object
) -> AuthorityOperation:
    return AuthorityOperation.model_validate(
        {
            "project_id": project.project_id,
            "canonical_root": project.root,
            "owner_instance_id": self.instance_id,
            **values,
        }
    )


def _load(
    self: "ExecutionCoordinator",
    project: Project,
    approval_id: UUID,
    expected: str,
    deadline: float,
) -> AuthorityRecord:
    record = self.authority.authority(
        _operation(self, project, operation="show", approval_id=approval_id), deadline=deadline
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
    self: "ExecutionCoordinator",
    project: Project,
    record: AuthorityRecord,
    deadline: float,
    stop: ExecutionStopSignal | None = None,
) -> DockerTargetDeclaration:
    profile = _profile(self, project, record.manifest.profile.name)
    if profile != record.manifest.profile:
        raise DockerBackendFault("docker_target_mismatch")
    declaration = _declaration(self, profile)
    _target(self, declaration, deadline, stop)
    if stop is not None:
        stop.check()
    binding = self.inputs.verify(record.manifest, deadline=deadline)
    if (
        binding.snapshot_id != record.manifest.inputs.snapshot_id
        or binding.inventory_digest != record.manifest.inputs.digest
    ):
        raise DockerBackendFault("docker_policy_mismatch")
    self.inputs.check(binding, record.manifest, deadline=deadline)
    _deadline(deadline)
    return declaration

"""Cold explicit execution composition and unchanged owned activation scope."""

from pathlib import Path
from uuid import UUID

from jeff.application.execution import DockerCLIIdentity, DockerTargetDeclaration
from jeff.application.execution_coordinator import ExecutionCoordinator
from jeff.application.projects import Failure, ProjectService
from jeff.infrastructure.runtime_state import Ownership
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .. import runtime as _runtime


def create_internal_execution(
    *,
    owner: Ownership,
    storage: SQLiteStorage,
    projects: ProjectService,
    instance_id: UUID,
    binary: Path,
    config: Path,
    metadata_home: Path,
    lifecycle_home: Path,
    declarations: tuple[DockerTargetDeclaration, ...],
    expected_binary: DockerCLIIdentity | None = None,
    deadline: float | None = None,
) -> ExecutionCoordinator:
    """Construct adapters without Docker queries, implicit discovery or new workers.

    Trusted startup or an internal caller supplies owned SQL and private fresh HOME paths.
    Construction validates those explicit resources but never queries/launches Docker,
    creates directories, loads default user state or starts another worker.
    """
    owner.check()
    if (
        storage.owner is not owner
        or storage.instance_id != instance_id
        or storage.snapshot().state != "ready"
    ):
        raise _runtime.DockerBackendFault("docker_authority_required")
    if (
        metadata_home == lifecycle_home
        or metadata_home in lifecycle_home.parents
        or lifecycle_home in metadata_home.parents
    ):
        raise _runtime.DockerBackendFault("docker_unsafe_configuration")
    inputs = _runtime.FilePreparedInputStore(owner)
    verifier = _runtime.DockerTargetInspector(binary, config, home=metadata_home, deadline=deadline)
    backend = _runtime.DockerLifecycleCLI(binary, config, lifecycle_home, deadline=deadline)
    if expected_binary is not None and (
        verifier.identity != expected_binary or backend.identity != expected_binary
    ):
        raise _runtime.DockerBackendFault("docker_unsafe_configuration")
    controller = _runtime.DockerJobController(
        verifier, backend, inputs, storage, owner_instance_id=instance_id
    )
    return _runtime.ExecutionCoordinator(
        projects,
        storage,
        storage,
        inputs,
        inputs,
        verifier,
        controller,
        storage,
        _runtime.GuardedOutputCollector,
        profiles=lambda: storage.execution_profiles,
        declarations=declarations,
        instance_id=instance_id,
        proposal_lookup=storage,
        inspection=storage,
        stop_store=storage,
    )


def create_configured_execution(
    *,
    owner: Ownership,
    storage: SQLiteStorage,
    projects: ProjectService,
    instance_id: UUID,
    deadline: float,
) -> ExecutionCoordinator | Failure:
    """Owned startup construction; never query Docker, discover defaults or repair state."""

    def active() -> None:
        owner.check()
        if storage.owner is not owner or storage.instance_id != instance_id:
            raise _runtime.ControlFault("runtime_ownership_unconfirmed")
        if storage.snapshot().state != "ready":
            raise _runtime.StorageFault("storage_closing")

    active()
    configuration = storage.execution_backend
    if configuration is None:
        return _runtime.fresh_execution_failure("execution_unavailable")
    try:
        with owner.execution_homes(instance_id, deadline=deadline) as (metadata, lifecycle):
            built = _runtime.create_internal_execution(
                owner=owner,
                storage=storage,
                projects=projects,
                instance_id=instance_id,
                binary=configuration.binary.path,
                config=configuration.config,
                metadata_home=metadata,
                lifecycle_home=lifecycle,
                declarations=configuration.declarations,
                expected_binary=configuration.binary,
                deadline=deadline,
            )
        active()
        return built
    except _runtime.DockerBackendFault as error:
        active()  # Never disguise lost owner/storage readiness as optional backend failure.
        return _runtime.fresh_execution_failure(
            "execution_backend_capacity"
            if error.code == "docker_home_capacity"
            else "execution_backend_unavailable"
        )

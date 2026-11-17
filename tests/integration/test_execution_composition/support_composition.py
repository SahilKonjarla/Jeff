"""Shared private SQLite/files/prepared-input composition and explicit approval helpers."""

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from jeff.application.execution import (
    AuthorityOperation,
    AuthorityRecord,
    DockerCLIIdentity,
    DockerTargetDeclaration,
    ExecutionProfile,
    ExecutionSettings,
)
from jeff.application.execution_coordinator import ExecutionCoordinator
from jeff.application.projects import ProjectService
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import DEFAULT_SETTINGS
from jeff.domain.projects import Project
from jeff.infrastructure.docker_cli import DockerTargetInspector
from jeff.infrastructure.prepared_inputs import FilePreparedInputStore
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_execution import DockerJobController
from jeff.infrastructure.runtime_output import GuardedOutputCollector
from jeff.infrastructure.runtime_settings import load_or_create_settings
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_docker import Docker


class Discovery:
    def find_root(self, directory: Path, *, deadline: float | None = None) -> Path:
        return directory  # Explicit disposable namespace, no Git invocation.


@contextmanager
def composed(
    root: Path, *, configured: bool = False, cancellable: bool = False
) -> Iterator[tuple[ExecutionCoordinator, SQLiteStorage, Project, Docker]]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / "p"
    path.mkdir(mode=0o700)
    project = Project(uuid4(), path)
    files = FileProjectStore()
    files.initialize(project)
    (path / "source.py").write_text("public original\n")
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        owner.store.credential(owner)
        load_or_create_settings(owner)
        profile = ExecutionProfile(
            name="python",
            project_id=project.project_id,
            canonical_root=path,
            engine_id="synthetic-engine",
            endpoint="unix:///synthetic/docker.sock",
            image_id="sha256:" + "1" * 64,
            platform="linux/arm64",
            toolchain="synthetic",
            executable="/usr/local/bin/python",
        )
        settings = DEFAULT_SETTINGS.model_copy(
            update={"execution": ExecutionSettings(profiles=(profile,))}
        )
        if configured:
            import hashlib

            from jeff.application.execution import DockerBackendConfiguration
            from jeff.application.storage import ConfiguredRuntimeSettings

            binary = root / "configured-docker"
            binary.write_bytes(b"synthetic-cli")
            binary.chmod(0o500)
            config = root / "configured-cfg"
            config.mkdir(mode=0o700)
            (config / "config.json").write_text("{}")
            (config / "config.json").chmod(0o600)
            declared = DockerTargetDeclaration(
                profile=profile,
                image_environment=(("LANG", "C.UTF-8"), ("PATH", "/usr/local/bin:/usr/bin:/bin")),
                image_command=("python3",),
            )
            configured_settings = ConfiguredRuntimeSettings(
                schema_version=3,
                history=settings.history,
                execution=settings.execution,
                backend=DockerBackendConfiguration(
                    binary=DockerCLIIdentity(
                        path=binary,
                        sha256=hashlib.sha256(b"synthetic-cli").hexdigest(),
                        size=len(b"synthetic-cli"),
                    ),
                    config=config,
                    declarations=(declared,),
                ),
            )
            layout.settings.write_text(configured_settings.model_dump_json())
        else:
            layout.settings.write_text(settings.model_dump_json())  # JSON is valid fixture YAML.
        generation = uuid4()
        store = SQLiteStorage(owner, instance_id=generation)
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            store.index(project)
            docker = Docker()
            docker.cancel_mode = cancellable
            verifier = DockerTargetInspector(_reader=docker)
            inputs = FilePreparedInputStore(owner)
            controller = DockerJobController(
                verifier, docker, inputs, store, owner_instance_id=generation
            )
            declaration = DockerTargetDeclaration(
                profile=profile,
                image_environment=(("LANG", "C.UTF-8"), ("PATH", "/usr/local/bin:/usr/bin:/bin")),
                image_command=("python3",),
            )
            coordinator = ExecutionCoordinator(
                ProjectService(Discovery(), files),
                store,
                store,
                inputs,
                inputs,
                verifier,
                controller,
                store,
                GuardedOutputCollector,
                profiles=lambda: store.execution_profiles,
                declarations=(declaration,),
                instance_id=generation,
                proposal_lookup=store,
                inspection=store,
                stop_store=store if cancellable else None,
            )
            yield coordinator, store, project, docker
        finally:
            store.close(timeout=5)


def pending(coordinator: ExecutionCoordinator, project: Project) -> AuthorityRecord:
    return coordinator.propose(
        project.root,
        "python",
        ("/usr/local/bin/python", "-V"),
        ("source.py",),
        deadline=time.monotonic() + 5,
    )


def approved(coordinator: ExecutionCoordinator, project: Project) -> AuthorityRecord:
    record = pending(coordinator, project)
    return coordinator.decide(
        project.root,
        record.approval_id,
        record.manifest_digest,
        approve=True,
        deadline=time.monotonic() + 5,
    )


def show(store: SQLiteStorage, project: Project, approval_id: UUID) -> AuthorityRecord:
    assert store.instance_id is not None
    return store.authority(
        AuthorityOperation(
            operation="show",
            project_id=project.project_id,
            canonical_root=project.root,
            owner_instance_id=store.instance_id,
            approval_id=approval_id,
        ),
        deadline=time.monotonic() + 5,
    ).record

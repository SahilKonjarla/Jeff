"""Private SQLite/owner fixture and explicit scoped authority-operation builder."""

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from jeff.application.execution import (
    AuthorityOperation,
    ExecutionManifest,
    ExecutionProfile,
    PreparedInputs,
)
from jeff.application.runtime import RuntimeLayout
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


@contextmanager
def authority_store(
    root: Path,
    clock: dict[str, int] | None = None,
    *,
    retention_clock: Callable[[], int] | None = None,
    retention_monotonic: Callable[[], float] | None = None,
) -> Iterator[tuple[SQLiteStorage, Project, ExecutionManifest, UUID]]:
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    project, generation = Project(uuid4(), root / "project"), uuid4()
    profile = ExecutionProfile(
        name="python",
        project_id=project.project_id,
        canonical_root=project.root,
        engine_id="synthetic-no-engine-query",
        endpoint="unix:///synthetic/nonexistent.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="synthetic",
        executable="/usr/local/bin/python",
    )
    manifest = ExecutionManifest(
        profile=profile, argv=(profile.executable, "-V"), inputs=PreparedInputs()
    )
    with owner:
        owner.store.credential(owner)
        storage = SQLiteStorage(
            owner,
            instance_id=generation,
            retention_clock=retention_clock,
            retention_monotonic=retention_monotonic,
            review_clock=(lambda: clock["wall"]) if clock is not None else None,
            review_monotonic=(lambda: float(clock["mono"])) if clock is not None else None,
        )
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
            storage.index(project)
            yield storage, project, manifest, generation
        finally:
            storage.close(timeout=5)


def command(project: Project, generation: UUID, **values: object) -> AuthorityOperation:
    return AuthorityOperation.model_validate(
        {
            "project_id": project.project_id,
            "canonical_root": project.root,
            "owner_instance_id": generation,
            **values,
        }
    )

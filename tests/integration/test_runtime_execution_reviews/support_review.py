"""Canonical fresh owner/storage context and fully bound review operation builder."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from jeff.application.execution import (
    ExecutionCall,
    ExecutionProfile,
    ReviewOperation,
    build_manifest,
)
from jeff.application.runtime import RuntimeLayout
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


@contextmanager
def review_store(
    root: Path, clock: dict[str, float] | None = None
) -> Iterator[tuple[SQLiteStorage, Project, ExecutionProfile, UUID]]:
    owner = RuntimeStateStore(
        RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    ).acquire(create=True)
    assert owner is not None
    generation = uuid4()
    project = Project(uuid4(), root / "project")
    profile = ExecutionProfile(
        name="default",
        project_id=project.project_id,
        canonical_root=project.root,
        engine_id="synthetic-engine",
        endpoint="unix:///private/tmp/synthetic.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="Python",
        executable="/usr/local/bin/python",
    )
    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(
            owner,
            instance_id=generation,
            review_clock=(lambda: int(clock["wall"])) if clock is not None else None,
            review_monotonic=(lambda: clock["mono"]) if clock is not None else None,
        )
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            store.index(project)
            yield store, project, profile, generation
        finally:
            store.close()


def operation(
    project: Project, generation: UUID, call: ExecutionCall, profile: ExecutionProfile | None = None
) -> ReviewOperation:
    return ReviewOperation(
        call=call,
        project_id=project.project_id,
        canonical_root=project.root,
        owner_instance_id=generation,
        manifest=build_manifest(project, profile, call.argv)
        if profile is not None and call.operation == "propose"
        else None,
        configuration_fingerprint=profile.fingerprint if profile is not None else None,
    )

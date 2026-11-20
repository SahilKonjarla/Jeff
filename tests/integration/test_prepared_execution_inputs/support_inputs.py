"""Canonical private input-store owner context and bound execution-manifest builder."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from jeff.application.execution import ExecutionManifest, ExecutionProfile
from jeff.application.runtime import RuntimeLayout
from jeff.domain.projects import Project
from jeff.infrastructure.prepared_inputs import FilePreparedInputStore
from jeff.infrastructure.runtime_state import RuntimeStateStore


@contextmanager
def inputs_store(root: Path) -> Iterator[tuple[FilePreparedInputStore, Project, Path]]:
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        with owner.store.directory(layout.data_dir, create=True):
            pass
        project_root = root / "p"
        project_root.mkdir(mode=0o700)
        project = Project(uuid4(), project_root)
        yield FilePreparedInputStore(owner), project, layout.data_dir


def manifest(project: Project, prepared: object) -> ExecutionManifest:
    profile = ExecutionProfile(
        name="python",
        project_id=project.project_id,
        canonical_root=project.root,
        engine_id="synthetic",
        endpoint="unix:///synthetic/docker.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="synthetic",
        executable="/usr/local/bin/python",
    )
    return ExecutionManifest.model_validate(
        {"profile": profile, "argv": (profile.executable, "-V"), "inputs": prepared}
    )

"""Validated Git/YAML identity remains authoritative over durable registry rows."""

import os
from pathlib import Path
from uuid import uuid4

from jeff.application.projects import Failure, ProjectService, Success
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import RegistryService, RegistrySuccess
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage
from jeff.integrations.git import GitProjectDiscovery


def test_index_and_lookup_revalidate_yaml_without_copying_private_settings(
    git_repo: Path, tmp_path: Path
) -> None:
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    assert isinstance(projects.execute("init", git_repo), Success)
    config = git_repo / "jeff.local.yaml"
    config.write_bytes(config.read_bytes() + b"private: SYNTHETIC_PRIVATE_REGISTRY\n")
    original = config.read_bytes()
    ignore = (git_repo / ".gitignore").read_bytes()
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
            registry = RegistryService(projects, storage)
            absent = registry.execute("lookup", git_repo)
            assert isinstance(absent, RegistrySuccess) and not absent.data.indexed
            indexed = registry.execute("index", git_repo)
            assert isinstance(indexed, RegistrySuccess) and indexed.data.created
            repeated = registry.execute("index", git_repo)
            assert isinstance(repeated, RegistrySuccess) and not repeated.data.created
            config.unlink()
            missing = registry.execute("lookup", git_repo)
            assert isinstance(missing, Failure) and missing.error.code == "project_not_initialized"
            config.write_bytes(original)
            config.chmod(0o600)
            assert isinstance(registry.execute("lookup", git_repo), RegistrySuccess)
            assert config.read_bytes() == original
            assert (git_repo / ".gitignore").read_bytes() == ignore
            config.write_bytes(
                original.replace(str(indexed.data.project_id).encode(), str(uuid4()).encode())
            )
            changed = registry.execute("lookup", git_repo)
            assert isinstance(changed, Failure) and changed.error.code == "registry_conflict"
            config.write_bytes(original)
            moved = git_repo.with_name("moved fixture")
            git_repo.rename(moved)
            invalid = registry.execute("lookup", moved)
            assert isinstance(invalid, Failure) and invalid.error.code == "project_moved"
            moved.rename(git_repo)
        finally:
            storage.close()
    # Explicitly agreed durable-sink privacy check; CRUD assertions above use the seam.
    assert b"SYNTHETIC_PRIVATE_REGISTRY" not in layout.database.read_bytes()

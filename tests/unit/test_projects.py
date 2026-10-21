"""Pure project/application rules through their declared public interface."""

from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.projects import Failure, ProjectService, Success
from jeff.domain.projects import Project, ProjectError


class Discovery:
    def __init__(self, root: Path) -> None:
        self.root = root

    def find_root(self, directory: Path) -> Path:
        return self.root


class Store:
    def __init__(self) -> None:
        self.project: Project | None = None

    def initialize(self, project: Project) -> tuple[Project, bool]:
        created = self.project is None
        if created:
            self.project = project
        assert self.project is not None
        return self.project, created

    def read(self, root: Path) -> Project:
        if self.project is None:
            raise ProjectError("project_not_initialized", "Run jeff init.")
        return self.project


def test_initialization_is_stable(tmp_path: Path) -> None:
    service = ProjectService(Discovery(tmp_path), Store())
    first = service.execute("init", tmp_path)
    repeated = service.execute("init", tmp_path)

    assert isinstance(first, Success) and isinstance(repeated, Success)
    assert first.data.created and not repeated.data.created
    assert first.data.project_id == repeated.data.project_id
    assert first.data.root == str(tmp_path)


@pytest.mark.parametrize("operation", ["config", "status"])
def test_inspection_never_creates_registration(tmp_path: Path, operation: str) -> None:
    store = Store()
    service = ProjectService(Discovery(tmp_path), store)
    # Explicit cases keep the public operation type closed.
    result = service.execute("config" if operation == "config" else "status", tmp_path)

    assert isinstance(result, Failure)
    assert result.error.code == "project_not_initialized"
    assert store.project is None


def test_service_rejects_a_port_returning_a_stale_root(tmp_path: Path) -> None:
    store = Store()
    store.project = Project(uuid4(), tmp_path / "previous")
    result = ProjectService(Discovery(tmp_path), store).execute("status", tmp_path)

    assert isinstance(result, Failure)
    assert result.error.code == "project_moved"
    assert "previous" not in result.model_dump_json()


def test_domain_rejects_relative_roots() -> None:
    with pytest.raises(ProjectError, match="absolute"):
        Project(uuid4(), Path("relative"))

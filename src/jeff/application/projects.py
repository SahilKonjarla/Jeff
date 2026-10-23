"""Project use cases, typed results, and narrow adapter ports."""

from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from jeff.domain.projects import Project, ProjectError

Operation = Literal["init", "config", "status"]


class ProjectDiscovery(Protocol):
    def find_root(self, directory: Path, *, deadline: float | None = None) -> Path:
        """Find a canonical non-bare Git root without user-command execution."""
        ...


class ProjectStore(Protocol):
    def initialize(self, project: Project) -> tuple[Project, bool]:
        """Preserve existing identity/settings; return identity and whether created."""
        ...

    def read(self, root: Path) -> Project:
        """Read only declared configuration; reject missing, unsafe, or stale state."""
        ...


class ProjectData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    root: str
    config_path: str
    registered: Literal[True] = True
    created: bool = False


class ErrorData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str


class Success(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: Literal[True] = True
    data: ProjectData
    error: None = None


class Failure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: Literal[False] = False
    data: None = None
    error: ErrorData


class ProjectService:
    def __init__(self, discovery: ProjectDiscovery, store: ProjectStore) -> None:
        self._discovery = discovery
        self._store = store

    def resolve_root(self, directory: Path, *, deadline: float | None = None) -> Path | Failure:
        """Read-only canonical binding for an init that has no registration yet."""
        try:
            return self._discovery.find_root(
                directory, **({"deadline": deadline} if deadline is not None else {})
            )
        except ProjectError as error:
            return Failure(error=ErrorData(code=error.code, message=error.message))

    def execute(
        self, operation: Operation, directory: Path, *, deadline: float | None = None
    ) -> Success | Failure:
        """Bind a project and return observed identity only, not arbitrary settings."""
        try:
            root = self._discovery.find_root(
                directory, **({"deadline": deadline} if deadline is not None else {})
            )
            created = False
            if operation == "init":
                project, created = self._store.initialize(Project(uuid4(), root))
            else:
                project = self._store.read(root)
            if project.root != root:
                raise ProjectError(
                    "project_moved",
                    "The configured root differs from this repository. "
                    "Project rebind is not implemented; no identity was changed.",
                )
            return Success(
                data=ProjectData(
                    project_id=str(project.project_id),
                    root=str(project.root),
                    config_path=str(root / "jeff.local.yaml"),
                    created=created,
                )
            )
        except ProjectError as error:
            return Failure(error=ErrorData(code=error.code, message=error.message))

"""Local read-only project status with lazily selected runtime diagnostics."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from jeff.application.projects import Failure, ProjectData, ProjectService
from jeff.application.runtime import (
    ControlFault,
    ControlObservation,
    RuntimeControl,
    control_failure,
)

from .scope import _DispatchModel


class ProjectStatusData(ProjectData):
    runtime: ControlObservation


class ProjectStatusSuccess(_DispatchModel):
    ok: Literal[True] = True
    data: ProjectStatusData
    error: None = None


class ProjectStatusService:
    """Local read-only registration, then lazily selected runtime diagnostics; no action path."""

    def __init__(self, projects: ProjectService, control: Callable[[], RuntimeControl]) -> None:
        self.projects, self.control = projects, control

    def execute(self, directory: Path) -> ProjectStatusSuccess | Failure:
        project = self.projects.execute("status", directory)
        if isinstance(project, Failure):
            return project
        try:
            runtime = self.control().status()
        except ControlFault as error:
            return control_failure(error.code)
        if isinstance(runtime, Failure):
            return runtime
        return ProjectStatusSuccess(
            data=ProjectStatusData(**project.data.model_dump(), runtime=runtime.data)
        )

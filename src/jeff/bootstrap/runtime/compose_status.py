"""Compose one opt-in, pinned inspection target without touching Docker at startup."""

import time

from jeff.application.compose_status import (
    ComposeStatusData,
    ComposeStatusReader,
    ComposeStatusSuccess,
    ComposeStatusTarget,
    compose_status_failure,
)
from jeff.application.execution import DockerBackendFault
from jeff.application.projects import Failure
from jeff.domain.projects import Project
from jeff.infrastructure.docker_cli.compose import ComposeInspectionCLI


def compose_status_reader(target: ComposeStatusTarget) -> ComposeStatusReader:
    def inspect(project: Project, deadline: float) -> ComposeStatusSuccess | Failure:
        if (project.project_id, project.root) != (target.project_id, target.root):
            return compose_status_failure("compose_status_not_configured")
        deadline = min(deadline, time.monotonic() + 5)
        try:
            cli = ComposeInspectionCLI(
                target.binary.path, target.config, target.home, deadline=deadline
            )
            if cli.identity != target.binary:
                return compose_status_failure("compose_status_unconfirmed")
            report = cli.compose_status(
                endpoint=target.endpoint,
                project=target.project,
                service=target.service,
                root=target.root,
                deadline=deadline,
            )
        except DockerBackendFault as error:
            return compose_status_failure(
                "docker_unavailable"
                if error.code == "docker_unavailable"
                else "compose_status_unconfirmed"
            )
        return ComposeStatusSuccess(
            data=ComposeStatusData.model_validate(
                {
                    "compose_project": target.project,
                    "service": target.service,
                    "states": report.states,
                    "ambiguous": report.ambiguous,
                }
            )
        )

    return inspect

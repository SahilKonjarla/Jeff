"""Compose one opt-in, pinned inspection target without touching Docker at startup."""

import time

from jeff.application.compose_status import (
    ComposeLogsReader,
    ComposeLogsSuccess,
    ComposeStatusData,
    ComposeStatusReader,
    ComposeStatusSuccess,
    ComposeStatusTarget,
    compose_logs_failure,
    compose_status_failure,
)
from jeff.application.execution import DockerBackendFault
from jeff.application.projects import Failure
from jeff.domain.projects import Project
from jeff.infrastructure.docker_cli.compose import ComposeInspectionCLI


def _pinned_cli(target: ComposeStatusTarget, deadline: float) -> ComposeInspectionCLI:
    cli = ComposeInspectionCLI(target.binary.path, target.config, target.home, deadline=deadline)
    if cli.identity != target.binary:
        raise DockerBackendFault("docker_unsafe_configuration")
    return cli


def compose_logs_reader(target: ComposeStatusTarget) -> ComposeLogsReader:
    def logs(project: Project, service: str, deadline: float) -> ComposeLogsSuccess | Failure:
        if not target.allow_logs or (project.project_id, project.root, service) != (
            target.project_id, target.root, target.service
        ):
            return compose_logs_failure("compose_logs_not_configured")
        deadline = min(deadline, time.monotonic() + 5)
        try:
            report = _pinned_cli(target, deadline).compose_logs(
                endpoint=target.endpoint,
                project=target.project,
                service=target.service,
                root=target.root,
                deadline=deadline,
            )
        except DockerBackendFault as error:
            if error.code == "docker_unavailable":
                return compose_logs_failure("docker_unavailable")
            if error.code == "docker_target_mismatch":
                return compose_logs_failure("compose_logs_target_unconfirmed")
            return compose_logs_failure("compose_logs_unconfirmed")
        return ComposeLogsSuccess(data=report)

    return logs


def compose_status_reader(target: ComposeStatusTarget) -> ComposeStatusReader:
    def inspect(project: Project, deadline: float) -> ComposeStatusSuccess | Failure:
        if (project.project_id, project.root) != (target.project_id, target.root):
            return compose_status_failure("compose_status_not_configured")
        deadline = min(deadline, time.monotonic() + 5)
        try:
            report = _pinned_cli(target, deadline).compose_status(
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
                    "health": report.health,
                    "published_ports": report.published_ports,
                    "ambiguous": report.ambiguous,
                }
            )
        )

    return inspect

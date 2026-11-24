"""Explicit propose, approve, and apply CLI for project service lifecycle."""

from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import typer

from jeff.application.compose_service_commands import ServiceCommand, ServiceCommandSuccess
from jeff.application.dispatch import ProjectCall, ProjectClient
from jeff.application.projects import Failure
from jeff.application.runtime import ControlFault

from .invocation import invoke


def register(docker_app: typer.Typer, client: Callable[[], ProjectClient]) -> None:
    app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)
    docker_app.add_typer(app, name="service", help="Review and apply declared service actions.")

    def execute(operation: str, json_output: bool, **fields: str | UUID) -> None:
        def dispatch(directory: Path) -> ServiceCommandSuccess | Failure:
            call = ProjectCall(
                operation="service.lifecycle", directory=directory,
                service_command=ServiceCommand.model_validate({"operation": operation, **fields}),
            )
            reply = client().execute(call)
            if reply.accepted is None:
                typer.echo(f"Request: {call.request_id}", err=True)
            if not isinstance(reply.result, (ServiceCommandSuccess, Failure)):
                raise ControlFault("runtime_protocol_error")
            return reply.result

        invoke(dispatch, json_output)

    @app.command("propose-start")
    def propose_start(service: str, json_output: bool = typer.Option(False, "--json")) -> None:
        """Review the exact declared start without starting anything."""
        execute("propose-start", json_output, service=service)

    @app.command("propose-stop")
    def propose_stop(start_job_id: UUID, json_output: bool = typer.Option(False, "--json")) -> None:
        """Review a stop for the original start job's exact resources."""
        execute("propose-stop", json_output, start_job_id=start_job_id)

    @app.command("propose-restart")
    def propose_restart(start_job_id: UUID, json_output: bool = typer.Option(False, "--json")) -> None:
        """Review an in-place restart of the original running container."""
        execute("propose-restart", json_output, start_job_id=start_job_id)

    @app.command("approve")
    def approve(
        approval_id: UUID, digest: str = typer.Option(..., "--digest"),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Approve exactly the reviewed digest without running the action."""
        execute("approve", json_output, approval_id=approval_id, digest=digest)

    @app.command("apply")
    def apply(
        approval_id: UUID, digest: str = typer.Option(..., "--digest"),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Apply a fresh approval once. Never replay an uncertain result."""
        execute("apply", json_output, approval_id=approval_id, digest=digest)

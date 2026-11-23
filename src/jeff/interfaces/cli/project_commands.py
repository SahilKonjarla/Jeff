"""Register project identity and deterministic question commands."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal

import typer

from jeff.application.compose_status import ComposeLogsSuccess, ComposeStatusSuccess
from jeff.application.dispatch import ProjectCall, ProjectClient
from jeff.application.process_ports import ProcessListSuccess, ProcessPortsSuccess
from jeff.application.projects import Failure, Success
from jeff.application.questions import QuestionSuccess

from .invocation import invoke


def register(
    app: typer.Typer,
    client: Callable[[], ProjectClient],
    latest_pytest: Callable[[bool], None] | None = None,
) -> None:
    """Register callbacks without constructing the project client."""

    def execute(
        operation: Literal[
            "init", "config", "ask", "docker.status", "docker.logs", "process.ports", "process.list"
        ],
        json_output: bool,
        question: str | None = None,
        *,
        compose_service: str | None = None,
    ) -> None:
        def dispatch(
            directory: Path,
        ) -> (
            Success | QuestionSuccess | ComposeStatusSuccess | ComposeLogsSuccess
            | ProcessPortsSuccess | ProcessListSuccess | Failure
        ):
            call = ProjectCall(
                operation=operation, directory=directory, question=question,
                compose_service=compose_service,
            )
            reply = client().execute(call)
            # Correlation belongs on stderr, preserving the established JSON body.
            # Unknown or duplicate admission is not permission to retry the action.
            if (
                reply.accepted is None
                or (reply.receipt is not None and reply.receipt.state == "unknown")
                or (
                    isinstance(reply.result, Failure)
                    and reply.result.error.code
                    in ("duplicate_request", "request_conflict", "request_outcome_unknown")
                )
            ):
                typer.echo(f"Request: {call.request_id}", err=True)
            return reply.result

        invoke(dispatch, json_output)

    docker_app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)
    app.add_typer(docker_app, name="docker", help="Inspect the project's declared Docker target.")

    @docker_app.command("status")
    def docker_status(
        json_output: bool = typer.Option(
            False, "--json", help="Print a typed JSON result or error."
        ),
    ) -> None:
        """Read Compose service state without granting ownership or changing Docker."""
        execute("docker.status", json_output)

    @docker_app.command("logs")
    def docker_logs(
        service: str = typer.Argument(..., help="The exact configured Compose service."),
        json_output: bool = typer.Option(
            False, "--json", help="Print a typed JSON result or error."
        ),
    ) -> None:
        """Read one opt-in sanitized Compose log tail without persisting it."""
        execute("docker.logs", json_output, compose_service=service)

    process_app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)
    app.add_typer(process_app, name="process", help="Inspect native project processes and ports.")

    @process_app.command("list")
    def process_list(
        json_output: bool = typer.Option(
            False, "--json", help="Print a typed JSON result or error."
        ),
    ) -> None:
        """Observe same-user native processes with a working directory in this project."""
        execute("process.list", json_output)

    @process_app.command("ports")
    def process_ports(
        json_output: bool = typer.Option(
            False, "--json", help="Print a typed JSON result or error."
        ),
    ) -> None:
        """Observe same-user native IPv4 loopback TCP listeners for this project."""
        execute("process.ports", json_output)

    @app.command("init")
    def initialize(
        json_output: bool = typer.Option(
            False, "--json", help="Print a typed JSON result or error."
        ),
    ) -> None:
        """Register this Git repository, preserving existing identity and settings."""
        execute("init", json_output)

    @app.command("config")
    def configuration(
        json_output: bool = typer.Option(
            False, "--json", help="Print a typed JSON result or error."
        ),
    ) -> None:
        """Inspect public identity fields only; never dump arbitrary settings."""
        execute("config", json_output)

    @app.command("status")
    def status(
        json_output: bool = typer.Option(
            False, "--json", help="Print a typed JSON result or error."
        ),
    ) -> None:
        """Read-only project identity and explicit runtime facts; works while stopped."""
        invoke(lambda directory: client().status(directory), json_output)

    @app.command("ask")
    def ask(
        question: str = typer.Argument(..., help="A supported branch or change question."),
        json_output: bool = typer.Option(
            False, "--json", help="Print a typed JSON result or error."
        ),
    ) -> None:
        """Answer branch/change questions deterministically, without models or Git writes."""
        if (
            latest_pytest is not None
            and " ".join(question.strip().lower().removesuffix("?").split()) == "what failed"
        ):
            latest_pytest(json_output)
            return
        execute("ask", json_output, question)

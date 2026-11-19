"""Register fresh execution commands and identity-targeted cancellation."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

import typer
from pydantic import ValidationError

from jeff.application.execution import (
    ExecutionCancelCall,
    ExecutionCancelClient,
    ExecutionCancelReply,
    FreshExecutionCall,
    FreshExecutionClient,
    FreshExecutionOperation,
    FreshExecutionReply,
    fresh_execution_failure,
)
from jeff.application.runtime import ControlFault, control_failure

from .rendering import render, render_cancellation, render_execution


def register(
    app: typer.Typer,
    fresh_execution: Callable[[], FreshExecutionClient] | None,
    cancellation: Callable[[], ExecutionCancelClient] | None,
) -> None:
    """Register fresh actions without invoking factories or discovering cwd."""
    fresh_app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)
    app.add_typer(
        fresh_app,
        name="execution",
        help="Fresh reviewed actions; explicit approval and start, never legacy promotion.",
    )

    @fresh_app.command("cancel")
    def cancel_execution(
        approval_id: UUID,
        digest: str = typer.Option(..., "--digest", help="Exact reviewed manifest SHA256."),
        request_id: Annotated[UUID | None, typer.Option("--request-id")] = None,
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Request durable stop intent; acceptance does not confirm termination."""
        try:
            call = ExecutionCancelCall(
                request_id=request_id or uuid4(),
                approval_id=approval_id,
                reviewed_manifest_digest=digest,
            )
            typer.echo(f"Request: {call.request_id}", err=True)
            reply = (
                cancellation().cancel(call)
                if cancellation is not None
                else ExecutionCancelReply(
                    request_id=call.request_id,
                    approval_id=approval_id,
                    reviewed_manifest_digest=digest,
                    ok=False,
                    error=fresh_execution_failure("execution_unavailable"),
                )
            )
        except ControlFault as error:
            render(control_failure(error.code), json_output)
            return
        except (ValidationError, OSError):
            render(fresh_execution_failure("execution_unconfirmed"), json_output)
            return
        render_cancellation(reply, json_output)

    def fresh_action(
        operation: FreshExecutionOperation, json_output: bool, **values: object
    ) -> None:
        try:
            call = FreshExecutionCall.model_validate(
                {"directory": Path.cwd(), "request_id": uuid4(), "operation": operation, **values}
            )
            if operation in {"propose", "approve", "deny", "start"}:
                typer.echo(f"Request: {call.request_id}", err=True)
            reply = (
                fresh_execution().fresh(call)
                if fresh_execution is not None
                else FreshExecutionReply(
                    ok=False,
                    request_id=call.request_id,
                    operation=operation,
                    error=fresh_execution_failure("execution_unavailable"),
                )
            )
        except ControlFault as error:
            render(control_failure(error.code), json_output)
            return
        except (ValidationError, OSError):
            render(fresh_execution_failure("execution_unconfirmed"), json_output)
            return
        render_execution(reply, operation, json_output)

    @fresh_app.command("propose", context_settings={"ignore_unknown_options": True})
    def fresh_propose(
        argv: Annotated[
            list[str], typer.Argument(help="Literal public argv after --; never shell text.")
        ],
        profile: str = typer.Option("default", "--profile"),
        inputs: Annotated[
            list[str] | None,
            typer.Option(
                "--input",
                help="Repeated literal project-relative file; no glob/directory expansion.",
            ),
        ] = None,
        request_id: Annotated[UUID | None, typer.Option("--request-id")] = None,
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Prepare a fresh exact action for review; does not approve or start it."""
        fresh_action(
            "propose",
            json_output,
            profile_name=profile,
            argv=tuple(argv),
            paths=tuple(inputs or ()),
            request_id=request_id or uuid4(),
        )

    def fresh_target_command(operation: Literal["approve", "deny", "start"]) -> None:
        @fresh_app.command(operation)
        def target(
            approval_id: UUID,
            digest: str = typer.Option(
                ...,
                "--digest",
                help="Exact reviewed manifest SHA256; never current-verifier proof.",
            ),
            request_id: Annotated[UUID | None, typer.Option("--request-id")] = None,
            json_output: bool = typer.Option(False, "--json"),
        ) -> None:
            """Explicitly decide a pending review or request its single confirmed claim."""
            fresh_action(
                operation,
                json_output,
                approval_id=approval_id,
                reviewed_manifest_digest=digest,
                request_id=request_id or uuid4(),
            )

    for operation in ("approve", "deny", "start"):
        fresh_target_command(operation)

    def fresh_inspection_command(operation: Literal["show", "logs"]) -> None:
        @fresh_app.command(operation)
        def inspect(approval_id: UUID, json_output: bool = typer.Option(False, "--json")) -> None:
            """Inspect this fresh approval's durable job facts or bounded guarded logs."""
            fresh_action(operation, json_output, approval_id=approval_id)

    fresh_inspection_command("show")
    fresh_inspection_command("logs")

    def fresh_list_command(name: str, operation: Literal["approvals.list", "jobs.list"]) -> None:
        group = typer.Typer(no_args_is_help=True, rich_markup_mode=None)
        fresh_app.add_typer(
            group, name=name, help="Discover fresh executable records, not legacy reviews."
        )

        @group.command("list")
        def listing(
            after: Annotated[
                str | None, typer.Option("--after", help="next_after from this project page.")
            ] = None,
            limit: int = typer.Option(16, "--limit", min=1, max=32),
            json_output: bool = typer.Option(False, "--json"),
        ) -> None:
            """Read newest-first public facts; pagination grants no execution authority."""
            fresh_action(operation, json_output, after=after, limit=limit)

    fresh_list_command("approvals", "approvals.list")
    fresh_list_command("jobs", "jobs.list")

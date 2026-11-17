"""Register legacy review-only commands; they never grant execution authority."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

import typer

from jeff.application.execution import (
    ExecutionCall,
    ExecutionClient,
    ExecutionOperation,
    ExecutionSuccess,
    execution_failure,
)
from jeff.application.projects import Failure

from .invocation import invoke


def register(app: typer.Typer, execution: Callable[[], ExecutionClient] | None) -> None:
    """Keep each group's operation bound in its own callback scope."""

    def review_action(
        operation: ExecutionOperation,
        json_output: bool,
        *,
        argv: tuple[str, ...] = (),
        profile: str = "default",
        object_id: UUID | None = None,
        request_id: UUID | None = None,
        after: int = 0,
        limit: int = 16,
    ) -> None:
        def dispatch(directory: Path) -> ExecutionSuccess | Failure:
            call = ExecutionCall(
                directory=directory,
                operation=operation,
                argv=argv,
                profile_name=profile,
                object_id=object_id,
                request_id=request_id or uuid4(),
                after=after,
                limit=limit,
            )
            if execution is None:
                return execution_failure("execution_invalid")
            reply = execution().review(call)
            if not reply.result.ok:
                typer.echo(f"Request: {call.request_id}", err=True)
            return reply.result

        invoke(dispatch, json_output)

    @app.command("run", context_settings={"ignore_unknown_options": True})
    def propose_run(
        argv: Annotated[list[str], typer.Argument(help="Literal argv after --; never shell text.")],
        profile: str = typer.Option("default", "--profile", help="Declared project-bound profile."),
        request_id: Annotated[
            UUID | None, typer.Option("--request-id", help="Explicit mutation identity.")
        ] = None,
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Propose durable review-only work. This phase cannot execute commands."""
        review_action(
            "propose", json_output, argv=tuple(argv), profile=profile, request_id=request_id
        )

    def review_commands(name: str, stem: Literal["approval", "job"]) -> None:
        group = typer.Typer(no_args_is_help=True, rich_markup_mode=None)
        app.add_typer(group, name=name, help="Inspect review-only durable facts; no execution.")

        @group.command("list")
        def list_items(
            after: int = typer.Option(0, "--after", min=0),
            limit: int = typer.Option(16, "--limit", min=1, max=32),
            json_output: bool = typer.Option(False, "--json"),
        ) -> None:
            review_action(
                cast(ExecutionOperation, stem + ".list"), json_output, after=after, limit=limit
            )

        @group.command("show")
        def show_item(object_id: UUID, json_output: bool = typer.Option(False, "--json")) -> None:
            review_action(
                cast(ExecutionOperation, stem + ".show"), json_output, object_id=object_id
            )

        if stem == "approval":

            @group.command("approve")
            def approve(
                object_id: UUID,
                request_id: Annotated[UUID | None, typer.Option("--request-id")] = None,
                json_output: bool = typer.Option(False, "--json"),
            ) -> None:
                """Record one review-only decision, not launch authority."""
                review_action("approve", json_output, object_id=object_id, request_id=request_id)

            @group.command("deny")
            def deny(
                object_id: UUID,
                request_id: Annotated[UUID | None, typer.Option("--request-id")] = None,
                json_output: bool = typer.Option(False, "--json"),
            ) -> None:
                """Deny this exact proposal without execution."""
                review_action("deny", json_output, object_id=object_id, request_id=request_id)

    review_commands("approvals", "approval")
    review_commands("jobs", "job")

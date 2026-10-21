"""CLI presentation; filesystem/Git behavior stays behind application ports."""

import json
from collections.abc import Callable
from pathlib import Path

import typer

from jeff.application.projects import ErrorData, Failure, Operation, ProjectService, Success
from jeff.application.questions import BranchAnswer, ChangesAnswer, QuestionService, QuestionSuccess
from jeff.domain.repository import escape_metadata


def create_app(service: ProjectService, questions: QuestionService) -> typer.Typer:
    """Build a CLI around injected application use cases, not concrete adapters."""
    app = typer.Typer(
        name="jeff",
        help=(
            "Local developer assistant. Project registration and read-only Git questions. "
            "No daemon."
        ),
        add_completion=False,
        no_args_is_help=True,
        rich_markup_mode=None,
    )

    @app.callback()
    def cli() -> None:
        """Register a project and ask the two supported Git metadata questions."""

    def render(result: Success | QuestionSuccess | Failure, json_output: bool) -> None:
        if json_output:
            typer.echo(json.dumps(result.model_dump(mode="json"), ensure_ascii=True))
        elif result.ok:
            data = result.data
            typer.echo(f"Project: {data.project_id}\nRoot: {escape_metadata(data.root)}")
            if isinstance(data, BranchAnswer):
                typer.echo(f"Observed: {data.observed_at.isoformat()}")
                if data.branch.detached:
                    typer.echo(f"Detached HEAD at {data.branch.commit}")
                else:
                    suffix = " (no commits yet)" if data.branch.unborn else ""
                    typer.echo(f"Branch: {data.branch.name}{suffix}")
            elif isinstance(data, ChangesAnswer):
                typer.echo(f"Observed: {data.observed_at.isoformat()}")
                for change in data.changes:
                    origin = (
                        f"{change.original_path} -> " if change.original_path is not None else ""
                    )
                    conflict = " [conflict]" if change.conflicted else ""
                    typer.echo(
                        f"{change.index_status}{change.worktree_status} "
                        f"{origin}{change.path}{conflict}"
                    )
                if data.visible_clean:
                    typer.echo("No visible changes.")
                typer.echo(
                    "Scope: protected paths and submodule contents excluded; not full status."
                )
                if data.filtered_records:
                    typer.echo(f"Additional protected records withheld: {data.filtered_records}")
            else:
                typer.echo("Created registration." if data.created else "Registration preserved.")
        else:
            typer.echo(f"{result.error.code}: {result.error.message}", err=True)
        if not result.ok:
            raise typer.Exit(code=1)

    def invoke(
        call: Callable[[Path], Success | QuestionSuccess | Failure], json_output: bool
    ) -> None:
        try:
            result = call(Path.cwd())
        except OSError:
            result = Failure(
                error=ErrorData(
                    code="directory_unavailable",
                    message="Working directory unavailable. Run from an accessible repository.",
                )
            )
        render(result, json_output)

    def execute(operation: Operation, json_output: bool) -> None:
        invoke(lambda directory: service.execute(operation, directory), json_output)

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
        """Report project registration, not daemon, branch, or process status."""
        execute("status", json_output)

    @app.command("ask")
    def ask(
        question: str = typer.Argument(..., help="A supported branch or change question."),
        json_output: bool = typer.Option(
            False, "--json", help="Print a typed JSON result or error."
        ),
    ) -> None:
        """Answer branch/change questions deterministically, without models or Git writes."""
        invoke(lambda directory: questions.execute(question, directory), json_output)

    return app

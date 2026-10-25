"""CLI presentation; filesystem/Git behavior stays behind application ports."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import typer
from pydantic import ValidationError

from jeff.application.diagnostics import DoctorResult, RuntimeDiagnostics, unavailable_doctor
from jeff.application.dispatch import (
    DebugData,
    DebugQuery,
    DiagnosticSuccess,
    HistoryQuery,
    ProjectCall,
    ProjectClient,
    ProjectStatusSuccess,
    dispatch_failure,
)
from jeff.application.history import HistoryPage
from jeff.application.projects import ErrorData, Failure, Success
from jeff.application.questions import BranchAnswer, ChangesAnswer, QuestionSuccess
from jeff.application.runtime import ControlFault, ControlSuccess, RuntimeControl, control_failure
from jeff.domain.repository import escape_metadata


def create_app(
    client: Callable[[], ProjectClient],
    control: Callable[[], RuntimeControl],
    diagnostics: Callable[[], RuntimeDiagnostics] | None = None,
) -> typer.Typer:
    """Build a CLI around injected application use cases, not concrete adapters."""
    app = typer.Typer(
        name="jeff",
        help=(
            "Local developer assistant. Project registration and read-only Git questions. "
            "Explicit authenticated daemon; no autostart or local execution fallback."
        ),
        add_completion=False,
        no_args_is_help=True,
        rich_markup_mode=None,
    )

    @app.callback()
    def cli() -> None:
        """Register a project and ask the two supported Git metadata questions."""

    debug_app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)
    app.add_typer(debug_app, name="debug", help="Inspect bounded durable request metadata.")

    def render(
        result: Success
        | QuestionSuccess
        | ProjectStatusSuccess
        | ControlSuccess
        | DiagnosticSuccess
        | DoctorResult
        | Failure,
        json_output: bool,
    ) -> None:
        if json_output:
            typer.echo(json.dumps(result.model_dump(mode="json"), ensure_ascii=True))
        elif isinstance(result, DoctorResult):
            typer.echo(f"Doctor: {result.data.summary}")
            for check in result.data.checks:
                suffix = f" ({check.code})" if check.code is not None else ""
                typer.echo(f"{check.name}: {check.status}{suffix}")
            if result.error is not None:
                typer.echo(f"{result.error.code}: {result.error.message}", err=True)
        elif isinstance(result, DiagnosticSuccess):
            diagnostic_data = result.data
            if isinstance(diagnostic_data, HistoryPage):
                typer.echo(f"History coverage starts: {diagnostic_data.coverage_started_at_ms}ms")
                for item in diagnostic_data.items:
                    typer.echo(f"{item.request_id} {item.operation} {item.state}")
                if not diagnostic_data.items:
                    typer.echo("No instrumented request history.")
                if diagnostic_data.next_cursor is not None:
                    typer.echo(f"Next cursor: {diagnostic_data.next_cursor}")
                return
            typer.echo(f"Request: {diagnostic_data.receipt.request_id}")
            typer.echo(f"Root: {escape_metadata(str(diagnostic_data.receipt.canonical_root))}")
            typer.echo(
                f"Operation: {diagnostic_data.receipt.operation}\n"
                f"State: {diagnostic_data.receipt.state}"
            )
            if diagnostic_data.trace.legacy_metadata_only:
                typer.echo("Legacy metadata only; no recorded steps.")
            for event in diagnostic_data.trace.events:
                typer.echo(f"{event.sequence}: {event.kind} ({event.code or 'pending'})")
                if event.git is not None:
                    typer.echo(
                        f"  {event.git.category}: {event.git.elapsed_ms}ms, "
                        f"{event.git.output_bytes} bytes, started={event.git.process_started}"
                    )
        elif isinstance(result, ControlSuccess):
            typer.echo(f"Runtime: {result.data.state.value}")
            typer.echo(f"Application ready: {'yes' if result.data.application_ready else 'no'}")
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
                if isinstance(result, ProjectStatusSuccess):
                    typer.echo(f"Runtime: {result.data.runtime.state.value}")
                    typer.echo(
                        "Application ready: "
                        f"{'yes' if result.data.runtime.application_ready else 'no'}"
                    )
        else:
            typer.echo(f"{result.error.code}: {result.error.message}", err=True)
        if not result.ok:
            raise typer.Exit(code=1)

    def invoke(
        call: Callable[
            [Path], Success | QuestionSuccess | ProjectStatusSuccess | DiagnosticSuccess | Failure
        ],
        json_output: bool,
    ) -> None:
        try:
            result = call(Path.cwd())
        except ControlFault as error:
            result = control_failure(error.code)
        except ValidationError:
            result = Failure(
                error=ErrorData(
                    code="invalid_project_request",
                    message="Project request exceeds its path or input bounds.",
                )
            )
        except OSError:
            result = Failure(
                error=ErrorData(
                    code="directory_unavailable",
                    message="Working directory unavailable. Run from an accessible repository.",
                )
            )
        render(result, json_output)

    def execute(
        operation: Literal["init", "config", "ask"], json_output: bool, question: str | None = None
    ) -> None:
        def dispatch(directory: Path) -> Success | QuestionSuccess | Failure:
            call = ProjectCall(operation=operation, directory=directory, question=question)
            reply = client().execute(call)
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

    def lifecycle(operation: str, json_output: bool) -> None:
        try:
            runtime = control()
            result = runtime.start() if operation == "start" else runtime.stop()
        except ControlFault as error:
            result = control_failure(error.code)
        render(result, json_output)

    @app.command("doctor")
    def doctor(
        json_output: bool = typer.Option(
            False, "--json", help="Print bounded read-only diagnostics."
        ),
    ) -> None:
        """Inspect the selected installation without starting, writing or repairing it."""
        result = diagnostics().inspect() if diagnostics is not None else unavailable_doctor()
        render(result, json_output)

    @app.command("start")
    def start(
        json_output: bool = typer.Option(False, "--json", help="Print typed runtime readiness."),
    ) -> None:
        """Explicitly start the daemon and wait for authenticated full dispatcher readiness."""
        lifecycle("start", json_output)

    @app.command("stop")
    def stop(
        json_output: bool = typer.Option(False, "--json", help="Print confirmed runtime shutdown."),
    ) -> None:
        """Drain and stop the authenticated instance; confirm native process exit."""
        lifecycle("stop", json_output)

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
        execute("ask", json_output, question)

    @app.command("history")
    def history(
        limit: int = typer.Option(50, "--limit", min=1, max=100, clamp=False),
        cursor: str | None = typer.Option(None, "--cursor"),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """List scoped instrumented requests in stable acceptance order; never writes."""

        def diagnostic(directory: Path) -> DiagnosticSuccess | Failure:
            reply = client().inspect_evidence(
                HistoryQuery(directory=directory, limit=limit, cursor=cursor)
            )
            if reply.error is not None:
                return reply.error
            if reply.history is None:
                return dispatch_failure("receipt_unavailable")
            return DiagnosticSuccess(data=reply.history)

        invoke(diagnostic, json_output)

    @debug_app.command("last")
    def debug_last(
        request_id: Annotated[UUID | None, typer.Option("--request-id")] = None,
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Inspect the last accepted project request or one exact scoped request ID."""

        def diagnostic(directory: Path) -> DiagnosticSuccess | Failure:
            reply = client().inspect_evidence(
                DebugQuery(directory=directory, request_id=request_id)
            )
            if reply.error is not None:
                return reply.error
            if reply.receipt is None or reply.trace is None:
                return dispatch_failure("receipt_unavailable")
            return DiagnosticSuccess(data=DebugData(receipt=reply.receipt, trace=reply.trace))

        invoke(diagnostic, json_output)

    return app

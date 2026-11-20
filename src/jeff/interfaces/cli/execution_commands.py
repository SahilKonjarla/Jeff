"""Register fresh execution commands and identity-targeted cancellation."""

import time
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
from jeff.application.execution.pytest_report import PYTEST_RUNNER_ARGV
from jeff.application.execution.requests import JobClaimFilter, JobKindFilter
from jeff.application.runtime import ControlFault, control_failure

from .rendering import render, render_cancellation, render_execution


def register(
    app: typer.Typer,
    fresh_execution: Callable[[], FreshExecutionClient] | None,
    cancellation: Callable[[], ExecutionCancelClient] | None,
    *,
    jobs: typer.Typer | None = None,
) -> Callable[[bool], None]:
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
            if operation in {"propose", "tests.retry-failed"}:
                typer.echo(
                    "Development-only: declare only arguments and files you reviewed as "
                    "non-secret public data; argv and copied inputs may persist. "
                    "Filename filtering, hashing and output guards are not secret certification.",
                    err=True,
                )
            if operation in {"propose", "approve", "deny", "start", "tests.retry-failed"}:
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

    if jobs is not None:

        @jobs.command("cancel")
        def cancel_job(
            job_id: UUID,
            digest: str = typer.Option(..., "--digest", help="Exact reviewed manifest SHA256."),
            request_id: Annotated[UUID | None, typer.Option("--request-id")] = None,
            json_output: bool = typer.Option(False, "--json"),
        ) -> None:
            """Resolve a fresh job in this project and submit the existing stop intent."""
            try:
                if fresh_execution is None:
                    render(fresh_execution_failure("execution_unavailable"), json_output)
                    return
                reply = fresh_execution().fresh(
                    FreshExecutionCall(
                        directory=Path.cwd(),
                        request_id=uuid4(),
                        operation="jobs.logs",
                        job_id=job_id,
                    )
                )
                if not reply.ok:
                    render_execution(reply, "jobs.logs", json_output)
                    return
                if (
                    reply.view is None
                    or reply.view.job_id != job_id
                    or reply.view.manifest_digest != digest
                ):
                    render(fresh_execution_failure("execution_conflict"), json_output)
                    return
                cancel_execution(reply.view.approval_id, digest, request_id, json_output)
            except ControlFault as error:
                render(control_failure(error.code), json_output)
            except (ValidationError, OSError):
                render(fresh_execution_failure("execution_unconfirmed"), json_output)

        @jobs.command("logs")
        def job_logs(job_id: UUID, follow: bool = typer.Option(False, "--follow")) -> None:
            """Read sanitized job logs; closing the viewer never cancels the job."""
            if fresh_execution is None:
                render(fresh_execution_failure("execution_unavailable"), False)
                return
            cursor = None
            identity = None
            deadline = time.monotonic() + 3660
            try:
                client = fresh_execution()
                directory = Path.cwd()
                while time.monotonic() < deadline:
                    reply = client.fresh(
                        FreshExecutionCall(
                            directory=directory,
                            request_id=uuid4(),
                            operation="jobs.logs",
                            job_id=job_id,
                            log_after=cursor,
                        )
                    )
                    if not reply.ok:
                        render_execution(reply, "jobs.logs", False)
                        return
                    if identity is not None and reply.identity != identity:
                        typer.echo("Jeff restarted; reconnect to read retained logs.", err=True)
                        raise typer.Exit(code=1)
                    identity = reply.identity
                    page = reply.log_page
                    assert page is not None
                    if page.dropped:
                        typer.echo("[Earlier log output unavailable]", err=True)
                    for chunk in page.chunks:
                        typer.echo(chunk.text, nl=False, err=chunk.stream == "stderr")
                    cursor = page.next_offset
                    if page.closed or not follow:
                        return
                    if not page.chunks:
                        time.sleep(0.25)
            except KeyboardInterrupt:
                return
            except ControlFault as error:
                render(control_failure(error.code), False)
                return
            typer.echo("Log follow timed out; no stop was requested.", err=True)
            raise typer.Exit(code=1)

    test_app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)
    app.add_typer(test_app, name="tests", help="Propose and inspect approved pytest jobs.")

    @test_app.command("run")
    def run_tests(
        inputs: Annotated[list[str], typer.Option("--input", help="Reviewed project file.")],
        profile: str = typer.Option("pytest", "--profile"),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Propose a pytest job; approve and start separately through execution commands."""
        request = uuid4()
        fresh_action(
            "propose",
            json_output,
            profile_name=profile,
            argv=(*PYTEST_RUNNER_ARGV, request.hex),
            paths=tuple(inputs),
            request_id=request,
        )

    @test_app.command("results")
    def test_results(job_id: UUID, json_output: bool = typer.Option(False, "--json")) -> None:
        """Read the saved test report for a job in this project."""
        fresh_action("tests.results", json_output, job_id=job_id)

    @test_app.command("retry-failed")
    def retry_failed(job_id: UUID, json_output: bool = typer.Option(False, "--json")) -> None:
        """Propose a new approval for only the recorded failed tests."""
        fresh_action("tests.retry-failed", json_output, job_id=job_id)

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
                help="Operator-reviewed non-secret public file; no glob/directory expansion.",
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
            state: Annotated[JobClaimFilter | None, typer.Option("--state")] = None,
            kind: Annotated[JobKindFilter | None, typer.Option("--kind")] = None,
        ) -> None:
            """Read newest-first public facts; pagination grants no execution authority."""
            if operation != "jobs.list" and (state is not None or kind is not None):
                raise typer.BadParameter("Job filters are available only for execution jobs list.")
            fresh_action(
                operation,
                json_output,
                after=after,
                limit=limit,
                claim_filter=state,
                kind_filter=kind,
            )

    fresh_list_command("approvals", "approvals.list")
    fresh_list_command("jobs", "jobs.list")

    def latest_pytest(json_output: bool) -> None:
        fresh_action("tests.latest", json_output)

    return latest_pytest

"""Render typed CLI results without selecting clients or runtime state."""

import json
from uuid import UUID

import typer

from jeff.application.diagnostics import DoctorResult
from jeff.application.dispatch import DiagnosticSuccess, ProjectStatusSuccess
from jeff.application.execution import (
    ExecutionCancelReply,
    ExecutionSuccess,
    FreshExecutionOperation,
    FreshExecutionReply,
)
from jeff.application.history import HistoryPage
from jeff.application.projects import Failure, Success
from jeff.application.questions import BranchAnswer, ChangesAnswer, QuestionSuccess
from jeff.application.runtime import ControlSuccess
from jeff.domain.repository import escape_metadata


def render(
    result: Success
    | QuestionSuccess
    | ProjectStatusSuccess
    | ControlSuccess
    | DiagnosticSuccess
    | DoctorResult
    | ExecutionSuccess
    | Failure,
    json_output: bool,
) -> None:
    """Render typed public results only; raw exceptions are never output payloads."""
    if json_output:
        typer.echo(
            json.dumps(
                result.model_dump(mode="json"),
                ensure_ascii=True,
                separators=(",", ":") if isinstance(result, DoctorResult) else None,
            )
        )
    elif isinstance(result, DoctorResult):
        typer.echo(f"Doctor: {result.data.summary}")
        if result.data.runtime is not None:
            runtime_data = result.data.runtime
            typer.echo(f"Runtime: {runtime_data.state.value}")
            if runtime_data.active_retention_days is not None:
                typer.echo(f"Active retention days: {runtime_data.active_retention_days}")
        if result.data.configured_retention_days is not None:
            typer.echo(
                f"Configured retention days (inactive): {result.data.configured_retention_days}"
            )
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
    elif isinstance(result, ExecutionSuccess):
        typer.echo(result.data.message)
        for review in result.data.items:
            typer.echo(f"Proposal: {review.proposal_id}\nApproval: {review.approval_id}")
            typer.echo(f"Job: {review.job_id}")
            typer.echo(f"Review: {review.state.value}; job: {review.job_state} ({review.reason})")
            typer.echo(f"Manifest SHA256: {review.manifest_digest}")
            typer.echo(
                json.dumps(review.manifest.model_dump(mode="json"), ensure_ascii=True, indent=2)
            )
        if result.data.next_after is not None:
            typer.echo(f"Next after: {result.data.next_after}")
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
                origin = f"{change.original_path} -> " if change.original_path is not None else ""
                conflict = " [conflict]" if change.conflicted else ""
                typer.echo(
                    f"{change.index_status}{change.worktree_status} {origin}{change.path}{conflict}"
                )
            if data.visible_clean:
                typer.echo("No visible changes.")
            typer.echo("Scope: protected paths and submodule contents excluded; not full status.")
            if data.filtered_records:
                typer.echo(f"Additional protected records withheld: {data.filtered_records}")
        else:
            typer.echo("Created registration." if data.created else "Registration preserved.")
            if isinstance(result, ProjectStatusSuccess):
                typer.echo(f"Runtime: {result.data.runtime.state.value}")
                typer.echo(
                    f"Application ready: {'yes' if result.data.runtime.application_ready else 'no'}"
                )
    else:
        typer.echo(f"{result.error.code}: {result.error.message}", err=True)
    if not result.ok:
        raise typer.Exit(code=1)


def _unknown_execution_guidance(approval_id: UUID | None = None) -> None:
    identifier = str(approval_id) if approval_id is not None else "APPROVAL_ID"
    typer.echo("Recovery: inspect original facts in the same installation and registered project:")
    typer.echo(f"  jeff execution show {identifier} --json")
    typer.echo(f"  jeff execution logs {identifier} --json")
    typer.echo(
        "Missing logs do not prove nonexecution or cleanup. Do not retry, reset or adopt "
        "unknown work; preserve original IDs and seek separately authorized review. "
        "Restart/log expiry does not clear unknown-claim quarantine."
    )


def render_cancellation(reply: ExecutionCancelReply, json_output: bool) -> None:
    """Keep stop-intent acceptance distinct from confirmed termination."""
    if json_output:
        typer.echo(json.dumps(reply.model_dump(mode="json"), ensure_ascii=True))
    elif reply.error is not None:
        typer.echo(f"{reply.error.error.code}: {reply.error.error.message}", err=True)
    elif reply.stop is not None:
        typer.echo(f"Stop intent: durable; reason: {reply.stop.reason}")
        typer.echo(
            f"Termination: {reply.stop.outcome or 'unconfirmed'}; "
            f"cleanup: {reply.stop.cleanup_verified}; logs: {reply.stop.payload_state}"
        )
        typer.echo("Intent acceptance is not termination or complete-output confirmation.")
        if reply.stop.outcome == "unknown":
            _unknown_execution_guidance(reply.approval_id)
    if not reply.ok:
        raise typer.Exit(code=1)


def render_execution(
    reply: FreshExecutionReply, operation: FreshExecutionOperation, json_output: bool
) -> None:
    """Present fresh execution facts without implying permission to retry."""
    if json_output:
        typer.echo(json.dumps(reply.model_dump(mode="json"), ensure_ascii=True))
    elif reply.error is not None:
        typer.echo(f"{reply.error.error.code}: {reply.error.error.message}", err=True)
    elif reply.page is not None:
        typer.echo(f"Project: {reply.page.project_id}; root: {reply.page.canonical_root}")
        for item in reply.page.items:
            typer.echo(
                f"Approval: {item.approval_id}; job: {item.job_id}; "
                f"state: {item.state.value}; claim: {item.claim_state or 'none'}"
            )
        if reply.page.next_after is not None:
            typer.echo(f"Next --after: {reply.page.next_after}")
        typer.echo("Discovery only; inspect the exact manifest before any decision.")
        if any(item.claim_state == "unknown" for item in reply.page.items):
            _unknown_execution_guidance()
    elif reply.admission is not None:
        typer.echo(
            f"Job: {reply.admission.job_id}\nClaim: {reply.admission.claim_state}; "
            f"replayed={reply.admission.replayed}"
        )
        typer.echo("Claim acknowledgement only; not guest-start or completion confirmation.")
    elif reply.view is not None:
        view = reply.view
        typer.echo(
            f"Approval: {view.approval_id}\nJob: {view.job_id}\nState: {view.state.value}; "
            f"claim: {view.claim_state or 'none'}"
        )
        typer.echo(f"Manifest SHA256: {view.manifest_digest}")
        typer.echo(json.dumps(view.manifest.model_dump(mode="json"), ensure_ascii=True, indent=2))
        if operation in {"show", "logs"}:
            typer.echo(
                f"Completion: {reply.completion_state}; absence is not proof a guest never ran."
            )
        if reply.execution_phase is not None:
            typer.echo(f"Observation phase: {reply.execution_phase}")
        if reply.stop is not None:
            typer.echo(
                f"Stop: {reply.stop.reason}; outcome: {reply.stop.outcome or 'unconfirmed'}; "
                f"cleanup: {reply.stop.cleanup_verified}; text: {reply.stop.payload_state}"
            )
            if reply.stop.output_facts is not None:
                facts = reply.stop.output_facts
                typer.echo(
                    f"Output: aborted={facts.aborted}; stdout EOF={facts.stdout_eof}; "
                    f"stderr EOF={facts.stderr_eof}; truncated={facts.truncated}"
                )
        if reply.completion is not None:
            typer.echo(
                f"Disposition: {reply.completion.disposition}; "
                f"exit: {reply.completion.outcome.exit_code}; "
                f"text: {reply.completion.payload_state}"
            )
        if view.claim_state == "unknown":
            _unknown_execution_guidance(view.approval_id)
        if reply.output is not None:
            typer.echo(reply.output.stdout, nl=False)
            typer.echo(reply.output.stderr, nl=False, err=True)
    if not reply.ok:
        raise typer.Exit(code=1)

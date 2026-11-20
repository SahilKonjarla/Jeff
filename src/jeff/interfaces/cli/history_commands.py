"""Register scoped history and request-evidence commands."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from jeff.application.dispatch import (
    DebugData,
    DebugQuery,
    DiagnosticSuccess,
    HistoryQuery,
    ProjectClient,
    dispatch_failure,
)
from jeff.application.projects import Failure

from .invocation import invoke


def register(app: typer.Typer, debug_app: typer.Typer, client: Callable[[], ProjectClient]) -> None:
    """Register evidence callbacks without querying the client."""

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

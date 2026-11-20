"""Lazy CLI composition; concrete adapters remain bootstrap's responsibility."""

from collections.abc import Callable

import typer

from jeff.application.diagnostics import RuntimeDiagnostics
from jeff.application.dispatch import ProjectClient
from jeff.application.execution import ExecutionCancelClient, ExecutionClient, FreshExecutionClient
from jeff.application.runtime import RuntimeControl

from . import (
    execution_commands,
    history_commands,
    project_commands,
    review_commands,
    runtime_commands,
)


def create_app(
    client: Callable[[], ProjectClient],
    control: Callable[[], RuntimeControl],
    diagnostics: Callable[[], RuntimeDiagnostics] | None = None,
    execution: Callable[[], ExecutionClient] | None = None,
    fresh_execution: Callable[[], FreshExecutionClient] | None = None,
    cancellation: Callable[[], ExecutionCancelClient] | None = None,
) -> typer.Typer:
    """Build a CLI without invoking factories or selecting an installation.

    Registered callback docstrings are user-visible help; rendering and factory
    selection remain separate so help/syntax cannot start runtime work.
    """
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

    # Preserve registration order, including group order in public help.
    review_commands.register(app, execution)
    latest_pytest = execution_commands.register(app, fresh_execution, cancellation)
    runtime_commands.register(app, control, diagnostics)
    project_commands.register(app, client, latest_pytest)
    history_commands.register(app, debug_app, client)
    return app

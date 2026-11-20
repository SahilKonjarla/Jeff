"""Register explicit runtime lifecycle and read-only diagnosis commands."""

from collections.abc import Callable

import typer

from jeff.application.diagnostics import RuntimeDiagnostics, unavailable_doctor
from jeff.application.runtime import ControlFault, RuntimeControl, control_failure

from .rendering import render


def register(
    app: typer.Typer,
    control: Callable[[], RuntimeControl],
    diagnostics: Callable[[], RuntimeDiagnostics] | None,
) -> None:
    """Retain lazy factories: help must not select or start an installation."""

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

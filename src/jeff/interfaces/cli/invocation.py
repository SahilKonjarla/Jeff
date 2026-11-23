"""Working-directory capture and bounded CLI error presentation."""

from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from jeff.application.compose_status import ComposeLogsSuccess, ComposeStatusSuccess
from jeff.application.dispatch import DiagnosticSuccess, ProjectStatusSuccess
from jeff.application.execution import ExecutionSuccess
from jeff.application.process_ports import ProcessListSuccess, ProcessPortsSuccess
from jeff.application.projects import ErrorData, Failure, Success
from jeff.application.questions import QuestionSuccess
from jeff.application.runtime import ControlFault, control_failure

from .rendering import render


def invoke(
    call: Callable[
        [Path],
        Success
        | QuestionSuccess
        | ProjectStatusSuccess
        | DiagnosticSuccess
        | ComposeStatusSuccess
        | ComposeLogsSuccess
        | ProcessPortsSuccess
        | ProcessListSuccess
        | ExecutionSuccess
        | Failure,
    ],
    json_output: bool,
) -> None:
    """Capture cwd only for commands that target the current project."""
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

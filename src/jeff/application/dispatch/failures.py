"""Fixed public dispatch refusals without raw input or transport diagnostics."""

from typing import Literal

from jeff.application.projects import ErrorData, Failure


def dispatch_failure(
    code: Literal[
        "unsupported_operation",
        "unsupported_intent",
        "project_target_mismatch",
        "duplicate_request",
        "request_conflict",
        "request_timeout",
        "request_outcome_unknown",
        "runtime_busy",
        "runtime_stopping",
        "receipt_unavailable",
    ],
) -> Failure:
    messages = {
        "unsupported_operation": (
            "This dispatcher supports only project init/config and the two Git questions."
        ),
        "unsupported_intent": "Supported questions: 'what branch am I on?' and 'what changed?'.",
        "project_target_mismatch": "The validated project does not match the requested target.",
        "duplicate_request": "This request ID already has a receipt; no action was replayed.",
        "request_conflict": "The request ID conflicts with its recorded scope or state.",
        "request_timeout": "The request budget expired; no automatic retry was performed.",
        "request_outcome_unknown": (
            "Request outcome unconfirmed; inspect its receipt. Do not replay automatically."
        ),
        "runtime_busy": "Project admission is full or storage is busy; no work was replayed.",
        "runtime_stopping": "The dispatcher is closing admission.",
        "receipt_unavailable": "No receipt is available in the validated scope.",
    }
    return Failure(error=ErrorData(code=code, message=messages[code]))

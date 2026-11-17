"""Finite public-safe runtime/control failures with the original storage fallthrough."""

from typing import Literal

from jeff.application.projects import Failure
from jeff.application.storage import StorageIssueCode

from .. import runtime as _runtime

RuntimeIssueCode = Literal[
    "invalid_runtime_path",
    "unsafe_runtime_path",
    "runtime_path_too_long",
    "runtime_inspection_failed",
    "unsupported_runtime_platform",
]


_MESSAGES: dict[RuntimeIssueCode, str] = {
    "invalid_runtime_path": "Runtime locations must be valid absolute paths with a valid owner.",
    "unsafe_runtime_path": "Runtime paths have unsafe links, types, ownership or permissions.",
    "runtime_path_too_long": "Runtime paths exceed platform byte limits; choose shorter locations.",
    "runtime_inspection_failed": "Runtime metadata is unconfirmed; no state was changed.",
    "unsupported_runtime_platform": "Runtime locations are not validated for this platform.",
}


def runtime_failure(code: RuntimeIssueCode) -> Failure:
    """Map only fixed public-safe descriptions, never raw validation/OS exceptions."""
    return _runtime.Failure(error=_runtime.ErrorData(code=code, message=_runtime._MESSAGES[code]))


ControlIssueCode = (
    RuntimeIssueCode
    | StorageIssueCode
    | Literal[
        "runtime_absent",
        "daemon_not_running",
        "runtime_credentials_invalid",
        "runtime_ownership_unconfirmed",
        "runtime_endpoint_collision",
        "runtime_protocol_error",
        "runtime_protocol_incompatible",
        "runtime_authentication_failed",
        "runtime_handshake_unconfirmed",
        "runtime_peer_unverified",
        "runtime_unreachable",
        "runtime_busy",
        "runtime_start_failed",
        "runtime_start_timeout",
        "runtime_stop_unconfirmed",
        "runtime_capability_unavailable",
        "activity_stream_closed",
        "activity_slow_reader",
    ]
)


_CONTROL_MESSAGES: dict[ControlIssueCode, str] = {
    code: message for code, message in _MESSAGES.items()
}


_CONTROL_MESSAGES.update(
    {
        "runtime_capability_unavailable": (
            "This runtime does not provide the required live capability."
        ),
        "activity_stream_closed": "The activity stream ended; resume or resynchronize explicitly.",
        "activity_slow_reader": "Activity delivery exceeded its bound; resynchronize explicitly.",
        "runtime_absent": "No control runtime ownership or endpoint was found.",
        "daemon_not_running": (
            "The daemon is stopped. Run jeff start explicitly; no action was executed."
        ),
        "runtime_credentials_invalid": "The private installation credential is missing or unsafe.",
        "runtime_ownership_unconfirmed": "Runtime ownership could not be established safely.",
        "runtime_endpoint_collision": "An existing endpoint cannot be adopted or removed safely.",
        "runtime_protocol_error": "The control message is invalid or exceeds its bounds.",
        "runtime_protocol_incompatible": "The control protocol revision is incompatible.",
        "runtime_authentication_failed": "Control authentication was not established.",
        "runtime_handshake_unconfirmed": (
            "Control handshake unconfirmed; compatibility/authentication was not established."
        ),
        "runtime_peer_unverified": "Native control peer identity could not be verified.",
        "runtime_unreachable": "The control runtime is not reachable.",
        "runtime_busy": "Control connection capacity is exhausted.",
        "runtime_start_failed": "The owned control child could not start safely.",
        "runtime_start_timeout": "Control readiness was not established within its deadline.",
        "runtime_stop_unconfirmed": "Runtime shutdown could not be confirmed within its bounds.",
    }
)


class ControlFault(Exception):
    """Fixed descriptions only, never credential/frame/OS exception content."""

    def __init__(self, code: ControlIssueCode) -> None:
        self.code = code
        super().__init__(_runtime.control_failure(code).error.message)


def control_failure(code: ControlIssueCode) -> Failure:
    if code not in _runtime._CONTROL_MESSAGES:
        return _runtime.storage_failure(_runtime.cast(_runtime.StorageIssueCode, code))
    return _runtime.Failure(
        error=_runtime.ErrorData(code=code, message=_runtime._CONTROL_MESSAGES[code])
    )

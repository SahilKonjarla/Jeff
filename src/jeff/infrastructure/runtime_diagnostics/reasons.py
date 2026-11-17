"""Finite declared diagnostic reason projection; raw collaborator errors never escape."""

from jeff.application.diagnostics import DoctorReason

from .. import runtime_diagnostics as _native

_PATH_REASONS = {
    "invalid_runtime_path",
    "unsafe_runtime_path",
    "runtime_path_too_long",
    "runtime_inspection_failed",
    "unsupported_runtime_platform",
}


def _control_reason(code: str) -> DoctorReason:
    # Projection maps only declared public reasons; never raw exceptions/frames.
    allowed = {
        "unsafe_runtime_path",
        "runtime_inspection_failed",
        "runtime_ownership_unconfirmed",
        "runtime_endpoint_collision",
        "runtime_credentials_invalid",
        "runtime_authentication_failed",
        "runtime_protocol_incompatible",
        "runtime_protocol_error",
        "runtime_peer_unverified",
        "runtime_handshake_unconfirmed",
        "runtime_unreachable",
        "runtime_busy",
        "runtime_capability_unavailable",
        "storage_unsafe",
        "storage_unrecognized",
        "storage_schema_unsupported",
        "storage_recovery_required",
        "storage_busy",
        "storage_failed",
        "settings_invalid",
        "storage_closing",
        "storage_outcome_unconfirmed",
    }
    if code == "runtime_absent":
        return "installation_incomplete"
    return (
        _native.cast(_native.DoctorReason, code) if code in allowed else "diagnostic_not_available"
    )


def _path_reason(code: str) -> DoctorReason:
    return (
        _native.cast(_native.DoctorReason, code)
        if code in _native._PATH_REASONS
        else "diagnostic_not_available"
    )

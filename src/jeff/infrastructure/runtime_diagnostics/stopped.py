"""Continuously leased non-creating settings/storage inspection; discard unconfirmed facts."""

from jeff.application.diagnostics import DoctorCheck
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import LoadedRuntimeSettings

from .. import runtime_diagnostics as _native


def _stopped(
    layout: RuntimeLayout, deadline: float
) -> tuple[list[DoctorCheck], LoadedRuntimeSettings | None, int | None]:
    """Hold a non-creating lease through all stopped reads and final revalidation."""
    settings: _native.LoadedRuntimeSettings | None = None
    version: int | None = None
    checks = [
        _native.DoctorCheck(
            name=name, status="unavailable", code="diagnostic_not_available", required=True
        )
        for name in _native.DOCTOR_CHECKS[4:]
    ]
    try:
        with _native.RuntimeStateStore(layout).diagnostic_lease(deadline) as lease:
            checks[0] = _native.DoctorCheck(name="runtime", status="ok", required=True)
            try:
                settings = _native.read_existing_settings(lease)
                checks[1] = _native.DoctorCheck(name="settings", status="ok", required=True)
            except _native.StorageFault:
                checks[1] = _native.DoctorCheck(
                    name="settings", status="failed", code="settings_invalid", required=True
                )
            try:
                version = _native.inspect_stopped_storage(lease, deadline)
                checks[2] = _native.DoctorCheck(
                    name="storage",
                    status="ok" if version == _native.DATABASE_SCHEMA else "failed",
                    code=None if version == _native.DATABASE_SCHEMA else "storage_upgrade_required",
                    required=True,
                )
            except _native.StorageFault as error:
                allowed = {
                    "storage_unsafe",
                    "storage_unrecognized",
                    "storage_schema_unsupported",
                    "storage_recovery_required",
                    "storage_busy",
                    "storage_failed",
                }
                code: _native.DoctorReason = (
                    _native.cast(_native.DoctorReason, error.code)
                    if error.code in allowed
                    else "storage_failed"
                )
                checks[2] = _native.DoctorCheck(
                    name="storage",
                    status="unavailable" if code == "storage_busy" else "failed",
                    code=code,
                    required=True,
                )
            lease.check()
    except _native.DiagnosticDeadlineExceeded:
        checks = [
            _native.DoctorCheck(
                name=name,
                status="unavailable",
                code="diagnostic_deadline_exceeded",
                required=True,
            )
            for name in _native.DOCTOR_CHECKS[4:]
        ]
        settings, version = None, None
    except _native.ControlFault as error:
        allowed = {
            "unsafe_runtime_path",
            "runtime_inspection_failed",
            "runtime_ownership_unconfirmed",
            "runtime_endpoint_collision",
            "runtime_credentials_invalid",
        }
        reason: _native.DoctorReason = (
            _native.cast(_native.DoctorReason, error.code)
            if error.code in allowed
            else "installation_incomplete"
            if error.code == "runtime_absent"
            else "runtime_inspection_failed"
        )
        checks = [
            _native.DoctorCheck(name=name, status="unavailable", code=reason, required=True)
            for name in _native.DOCTOR_CHECKS[4:]
        ]
        if reason in ("runtime_credentials_invalid", "installation_incomplete"):
            checks[0] = _native.DoctorCheck(
                name="runtime", status="failed", code=reason, required=True
            )
        settings, version = None, None
    return checks, settings, version

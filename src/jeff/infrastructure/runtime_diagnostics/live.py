"""Authenticated live caches or continuously observed stopped transition; no live SQL reads."""

from jeff.application.diagnostics import DoctorCheck, DoctorRuntimeData
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import LoadedRuntimeSettings

from .. import runtime_diagnostics as _native


def _runtime(
    layout: RuntimeLayout, deadline: float
) -> tuple[list[DoctorCheck], LoadedRuntimeSettings | None, int | None, DoctorRuntimeData | None]:
    """Use authenticated cached live facts, never client-side live settings/SQL reads."""
    try:
        with _native.RuntimeStateStore(layout).diagnostic_observation(deadline) as ownership:
            if not ownership.held:
                checks, settings, version = _native.RuntimeDiagnosticsAdapter._stopped(
                    layout, deadline
                )
                return checks, settings, version, None
            if not ownership.has_endpoint:
                raise _native.ControlFault("runtime_ownership_unconfirmed")
            observed = _native.inspect_control(layout, deadline)
            ownership.check()
            if (
                observed.application_ready
                and "activity.subscribe" not in observed.available_operations
            ):
                raise _native.ControlFault("runtime_capability_unavailable")
            if observed.identity is None or observed.state not in (
                _native.RuntimeState.RUNNING,
                _native.RuntimeState.STOPPING,
            ):
                raise _native.ControlFault("runtime_ownership_unconfirmed")
            storage = observed.storage
            retention = storage.retention_days
            runtime = _native.DoctorRuntimeData(
                state=observed.state,
                identity=observed.identity,
                control_ready=observed.control_ready,
                application_ready=observed.application_ready,
                available_operations=observed.available_operations,
                storage_state=storage.state,
                storage_error=storage.error,
                active_retention_days=(
                    None
                    if retention is None
                    else retention
                    if retention <= 9223372036854775807
                    else "above_report_limit"
                ),
                settings_source=storage.settings_source,
            )
            checks = [_native.DoctorCheck(name="runtime", status="ok", required=True)]
            if observed.state == _native.RuntimeState.STOPPING:
                checks[0] = _native.DoctorCheck(
                    name="runtime", status="unavailable", code="runtime_stopping", required=True
                )
            elif not observed.application_ready:
                checks[0] = _native.DoctorCheck(
                    name="runtime",
                    status="unavailable",
                    code="runtime_not_ready",
                    required=True,
                )
            if storage.state == "ready":
                checks += [
                    _native.DoctorCheck(name=name, status="ok", required=True)
                    for name in _native.DOCTOR_CHECKS[5:]
                ]
            else:
                checks.append(
                    _native.DoctorCheck(
                        name="settings",
                        status="unavailable",
                        code="storage_not_ready",
                        required=True,
                    )
                )
                code: _native.DoctorReason = (
                    "storage_closing" if storage.state == "closing" else "storage_not_ready"
                )
                if storage.error is not None:
                    code = _native.RuntimeDiagnosticsAdapter._control_reason(storage.error)
                checks.append(
                    _native.DoctorCheck(
                        name="storage",
                        status="failed" if storage.error else "unavailable",
                        code=code,
                        required=True,
                    )
                )
            return checks, None, storage.schema_version, runtime
    except _native.DiagnosticDeadlineExceeded:
        reason: _native.DoctorReason = "diagnostic_deadline_exceeded"
    except _native.ControlFault as error:
        reason = (
            "diagnostic_deadline_exceeded"
            if _native.time.monotonic() >= deadline
            else _native.RuntimeDiagnosticsAdapter._control_reason(error.code)
        )
    except (_native.ValidationError, OSError, ValueError):
        reason = "diagnostic_not_available"
    failed = reason in {
        "unsafe_runtime_path",
        "runtime_credentials_invalid",
        "runtime_authentication_failed",
        "runtime_peer_unverified",
        "runtime_protocol_error",
        "runtime_protocol_incompatible",
        "runtime_capability_unavailable",
        "installation_incomplete",
    }
    return (
        [
            _native.DoctorCheck(
                name=name,
                status="failed" if failed else "unavailable",
                code=reason,
                required=True,
            )
            for name in _native.DOCTOR_CHECKS[4:]
        ],
        None,
        None,
        None,
    )

"""Read-only metadata/tool diagnostics; no lifecycle, credential or SQL-owner access."""

import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Literal, cast

from pydantic import ValidationError

from jeff.application.diagnostics import (
    DOCTOR_CHECKS,
    DiagnosticDeadlineExceeded,
    DoctorCheck,
    DoctorReason,
    DoctorReport,
    DoctorResult,
    DoctorRuntimeData,
    doctor_result,
)
from jeff.application.projects import Failure
from jeff.application.runtime import ControlFault, RuntimeLayout, RuntimePathInspection
from jeff.application.storage import DATABASE_SCHEMA, RuntimeSettings, StorageFault
from jeff.domain.runtime import RuntimeState
from jeff.infrastructure.runtime_ipc import inspect_control
from jeff.infrastructure.runtime_paths import inspect_layout
from jeff.infrastructure.runtime_settings import read_existing_settings
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import inspect_stopped_storage

_PATH_REASONS = {
    "invalid_runtime_path",
    "unsafe_runtime_path",
    "runtime_path_too_long",
    "runtime_inspection_failed",
    "unsupported_runtime_platform",
}


class RuntimeDiagnosticsAdapter:
    def __init__(self, layout: Callable[[], RuntimeLayout]) -> None:
        self._layout = layout

    def inspect(self) -> DoctorResult:
        deadline = time.monotonic() + 5.0
        checks = [
            DoctorCheck(
                name="platform",
                status="ok" if sys.platform == "darwin" else "failed",
                code=None if sys.platform == "darwin" else "unsupported_runtime_platform",
                required=True,
            ),
            DoctorCheck(
                name="python",
                status="ok" if sys.version_info >= (3, 12) else "failed",
                code=None if sys.version_info >= (3, 12) else "python_unsupported",
                required=True,
            ),
        ]
        python_version = ".".join(map(str, sys.version_info[:3]))
        git_version = None
        git_reason: DoctorReason | None = None
        git_status: Literal["ok", "failed", "missing", "unavailable"] = "ok"
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("git", 0)
            probe = subprocess.run(
                ["/usr/bin/git", "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=remaining,
                env={
                    "PATH": os.defpath,
                    "LC_ALL": "C",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
            )
            version = probe.stdout.rstrip(b"\n")
            if (
                probe.returncode != 0
                or not 0 < len(version) <= 64
                or re.fullmatch(rb"git version [0-9][0-9A-Za-z .()+_-]*", version) is None
            ):
                git_status, git_reason = "failed", "git_probe_failed"
            else:
                git_version = version.decode("ascii")
        except FileNotFoundError:
            git_status, git_reason = "missing", "git_unavailable"
        except subprocess.TimeoutExpired:
            git_status, git_reason = "unavailable", "diagnostic_deadline_exceeded"
        except OSError:
            git_status, git_reason = "failed", "git_probe_failed"
        checks.append(DoctorCheck(name="git", status=git_status, code=git_reason, required=True))

        paths: RuntimePathInspection | None = None
        layout: RuntimeLayout | None = None
        reason: DoctorReason = "diagnostic_not_available"
        if time.monotonic() >= deadline:
            reason = "diagnostic_deadline_exceeded"
        elif sys.platform != "darwin":
            reason = "unsupported_runtime_platform"
        else:
            try:
                layout = self._layout()
                observed = inspect_layout(layout)
                if isinstance(observed, Failure):
                    reason = self._path_reason(observed.error.code)
                else:
                    paths = observed
            except ControlFault as error:
                reason = self._path_reason(error.code)
            except (OSError, ValueError):
                reason = "runtime_inspection_failed"

        expired = time.monotonic() >= deadline
        absent = paths is not None and all(entry.state == "missing" for entry in paths.entries)
        unsafe = paths is not None and any(entry.state == "unsafe" for entry in paths.entries)
        unknown = paths is not None and any(entry.state == "unknown" for entry in paths.entries)
        incomplete = paths is not None and any(
            entry.state == "missing" for entry in paths.entries if entry.role != "socket"
        )
        if expired:
            reason = "diagnostic_deadline_exceeded"
        for name in DOCTOR_CHECKS[3:]:
            if expired:
                check = DoctorCheck(name=name, status="unavailable", code=reason, required=True)
            elif unsafe and name == "paths":
                check = DoctorCheck(
                    name=name, status="unsafe", code="unsafe_runtime_path", required=True
                )
            elif absent:
                check = DoctorCheck(
                    name=name, status="missing", code="installation_absent", required=False
                )
            elif incomplete and name == "paths":
                check = DoctorCheck(
                    name=name, status="missing", code="installation_incomplete", required=True
                )
            elif (
                name == "paths"
                and paths is None
                and reason
                in (
                    "invalid_runtime_path",
                    "runtime_path_too_long",
                )
            ):
                check = DoctorCheck(name=name, status="failed", code=reason, required=True)
            elif name == "paths" and paths is not None and not unknown:
                check = DoctorCheck(name=name, status="ok", required=True)
            else:
                check = DoctorCheck(
                    name=name,
                    status="unavailable",
                    code="runtime_inspection_failed" if unknown else reason,
                    required=True,
                )
            checks.append(check)
        settings: RuntimeSettings | None = None
        database_version: int | None = None
        runtime: DoctorRuntimeData | None = None
        if (
            layout is not None
            and paths is not None
            and not expired
            and not absent
            and not unsafe
            and not unknown
            and not incomplete
        ):
            stopped_checks, settings, database_version, runtime = self._runtime(layout, deadline)
            if stopped_checks[0].code == "unsafe_runtime_path":
                checks[3] = DoctorCheck(
                    name="paths", status="unsafe", code="unsafe_runtime_path", required=True
                )
            checks[4:] = stopped_checks
        summary: Literal[
            "unsupported", "unhealthy", "unavailable", "uninitialized", "stopped", "healthy"
        ]
        if sys.platform != "darwin" or sys.version_info < (3, 12):
            summary = "unsupported"
        elif any(
            check.code in ("runtime_protocol_incompatible", "runtime_capability_unavailable")
            for check in checks
        ):
            summary = "unsupported"
        elif any(
            check.required
            and check.status in ("unsafe", "failed", "missing")
            and check.code != "storage_upgrade_required"
            for check in checks
        ):
            summary = "unhealthy"
        elif any(check.code == "storage_upgrade_required" for check in checks):
            summary = "unsupported"
        elif any(check.required and check.status != "ok" for check in checks):
            summary = "unavailable"
        else:
            summary = "uninitialized" if absent else "healthy" if runtime is not None else "stopped"
        return doctor_result(
            DoctorReport(
                summary=summary,
                checks=tuple(checks),
                paths=paths,
                runtime=runtime,
                python_version=python_version,
                git_version=git_version,
                settings_schema_version=settings.schema_version if settings is not None else None,
                configured_retention_days=(
                    None
                    if settings is None
                    else settings.history.retention_days
                    if settings.history.retention_days <= 9223372036854775807
                    else "above_report_limit"
                ),
                database_schema_version=database_version,
            )
        )

    @staticmethod
    def _runtime(
        layout: RuntimeLayout, deadline: float
    ) -> tuple[list[DoctorCheck], RuntimeSettings | None, int | None, DoctorRuntimeData | None]:
        try:
            with RuntimeStateStore(layout).diagnostic_observation(deadline) as ownership:
                if not ownership.held:
                    checks, settings, version = RuntimeDiagnosticsAdapter._stopped(layout, deadline)
                    return checks, settings, version, None
                if not ownership.has_endpoint:
                    raise ControlFault("runtime_ownership_unconfirmed")
                observed = inspect_control(layout, deadline)
                ownership.check()
                if (
                    observed.application_ready
                    and "activity.subscribe" not in observed.available_operations
                ):
                    raise ControlFault("runtime_capability_unavailable")
                if observed.identity is None or observed.state not in (
                    RuntimeState.RUNNING,
                    RuntimeState.STOPPING,
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
                storage = observed.storage
                retention = storage.retention_days
                runtime = DoctorRuntimeData(
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
                checks = [DoctorCheck(name="runtime", status="ok", required=True)]
                if observed.state == RuntimeState.STOPPING:
                    checks[0] = DoctorCheck(
                        name="runtime", status="unavailable", code="runtime_stopping", required=True
                    )
                elif not observed.application_ready:
                    checks[0] = DoctorCheck(
                        name="runtime",
                        status="unavailable",
                        code="runtime_not_ready",
                        required=True,
                    )
                if storage.state == "ready":
                    checks += [
                        DoctorCheck(name=name, status="ok", required=True)
                        for name in DOCTOR_CHECKS[5:]
                    ]
                else:
                    checks.append(
                        DoctorCheck(
                            name="settings",
                            status="unavailable",
                            code="storage_not_ready",
                            required=True,
                        )
                    )
                    code: DoctorReason = (
                        "storage_closing" if storage.state == "closing" else "storage_not_ready"
                    )
                    if storage.error is not None:
                        code = RuntimeDiagnosticsAdapter._control_reason(storage.error)
                    checks.append(
                        DoctorCheck(
                            name="storage",
                            status="failed" if storage.error else "unavailable",
                            code=code,
                            required=True,
                        )
                    )
                return checks, None, storage.schema_version, runtime
        except DiagnosticDeadlineExceeded:
            reason: DoctorReason = "diagnostic_deadline_exceeded"
        except ControlFault as error:
            reason = (
                "diagnostic_deadline_exceeded"
                if time.monotonic() >= deadline
                else RuntimeDiagnosticsAdapter._control_reason(error.code)
            )
        except (ValidationError, OSError, ValueError):
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
                DoctorCheck(
                    name=name,
                    status="failed" if failed else "unavailable",
                    code=reason,
                    required=True,
                )
                for name in DOCTOR_CHECKS[4:]
            ],
            None,
            None,
            None,
        )

    @staticmethod
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
        return cast(DoctorReason, code) if code in allowed else "diagnostic_not_available"

    @staticmethod
    def _stopped(
        layout: RuntimeLayout, deadline: float
    ) -> tuple[list[DoctorCheck], RuntimeSettings | None, int | None]:
        settings: RuntimeSettings | None = None
        version: int | None = None
        checks = [
            DoctorCheck(
                name=name, status="unavailable", code="diagnostic_not_available", required=True
            )
            for name in DOCTOR_CHECKS[4:]
        ]
        try:
            with RuntimeStateStore(layout).diagnostic_lease(deadline) as lease:
                checks[0] = DoctorCheck(name="runtime", status="ok", required=True)
                try:
                    settings = read_existing_settings(lease)
                    checks[1] = DoctorCheck(name="settings", status="ok", required=True)
                except StorageFault:
                    checks[1] = DoctorCheck(
                        name="settings", status="failed", code="settings_invalid", required=True
                    )
                try:
                    version = inspect_stopped_storage(lease, deadline)
                    checks[2] = DoctorCheck(
                        name="storage",
                        status="ok" if version == DATABASE_SCHEMA else "failed",
                        code=None if version == DATABASE_SCHEMA else "storage_upgrade_required",
                        required=True,
                    )
                except StorageFault as error:
                    allowed = {
                        "storage_unsafe",
                        "storage_unrecognized",
                        "storage_schema_unsupported",
                        "storage_recovery_required",
                        "storage_busy",
                        "storage_failed",
                    }
                    code: DoctorReason = (
                        cast(DoctorReason, error.code)
                        if error.code in allowed
                        else "storage_failed"
                    )
                    checks[2] = DoctorCheck(
                        name="storage",
                        status="unavailable" if code == "storage_busy" else "failed",
                        code=code,
                        required=True,
                    )
                lease.check()
        except DiagnosticDeadlineExceeded:
            checks = [
                DoctorCheck(
                    name=name,
                    status="unavailable",
                    code="diagnostic_deadline_exceeded",
                    required=True,
                )
                for name in DOCTOR_CHECKS[4:]
            ]
            settings, version = None, None
        except ControlFault as error:
            allowed = {
                "unsafe_runtime_path",
                "runtime_inspection_failed",
                "runtime_ownership_unconfirmed",
                "runtime_endpoint_collision",
                "runtime_credentials_invalid",
            }
            reason: DoctorReason = (
                cast(DoctorReason, error.code)
                if error.code in allowed
                else "installation_incomplete"
                if error.code == "runtime_absent"
                else "runtime_inspection_failed"
            )
            checks = [
                DoctorCheck(name=name, status="unavailable", code=reason, required=True)
                for name in DOCTOR_CHECKS[4:]
            ]
            if reason in ("runtime_credentials_invalid", "installation_incomplete"):
                checks[0] = DoctorCheck(name="runtime", status="failed", code=reason, required=True)
            settings, version = None, None
        return checks, settings, version

    @staticmethod
    def _path_reason(code: str) -> DoctorReason:
        return cast(DoctorReason, code) if code in _PATH_REASONS else "diagnostic_not_available"

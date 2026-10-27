"""Bounded read-only installation diagnostics; no concrete I/O or repair authority."""

from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from jeff.application.runtime import ProjectCapability, RuntimeIdentityData, RuntimePathInspection
from jeff.application.storage import StorageIssueCode
from jeff.domain.runtime import RuntimeState

DoctorName = Literal["platform", "python", "git", "paths", "runtime", "settings", "storage"]
DOCTOR_CHECKS: tuple[DoctorName, ...] = (
    "platform",
    "python",
    "git",
    "paths",
    "runtime",
    "settings",
    "storage",
)
DoctorReason = Literal[
    "installation_absent",
    "unsupported_runtime_platform",
    "python_unsupported",
    "git_unavailable",
    "git_probe_failed",
    "diagnostic_not_available",
    "diagnostic_deadline_exceeded",
    "invalid_runtime_path",
    "unsafe_runtime_path",
    "runtime_path_too_long",
    "runtime_inspection_failed",
    "installation_incomplete",
    "runtime_ownership_unconfirmed",
    "runtime_endpoint_collision",
    "runtime_credentials_invalid",
    "settings_invalid",
    "storage_unsafe",
    "storage_unrecognized",
    "storage_schema_unsupported",
    "storage_upgrade_required",
    "storage_recovery_required",
    "storage_busy",
    "storage_failed",
    "runtime_authentication_failed",
    "runtime_protocol_incompatible",
    "runtime_protocol_error",
    "runtime_peer_unverified",
    "runtime_handshake_unconfirmed",
    "runtime_unreachable",
    "runtime_busy",
    "runtime_capability_unavailable",
    "runtime_not_ready",
    "runtime_stopping",
    "storage_not_ready",
    "storage_closing",
    "storage_outcome_unconfirmed",
]
ConfiguredRetentionDays = (
    Annotated[StrictInt, Field(gt=0, le=9223372036854775807)] | Literal["above_report_limit"]
)


class _DoctorModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class DoctorCheck(_DoctorModel):
    name: DoctorName
    status: Literal["ok", "missing", "unavailable", "unsafe", "failed"]
    code: DoctorReason | None = None
    required: StrictBool

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.status == "ok") != (self.code is None):
            raise ValueError("Only successful checks omit a reason.")
        if self.status == "unsafe" and not self.required:
            raise ValueError("Unsafe checks are always required.")
        return self


class DoctorRuntimeData(_DoctorModel):
    """Authenticated cached facts only; bounded independently of wire readiness values."""

    state: Literal[RuntimeState.RUNNING, RuntimeState.STOPPING]
    identity: RuntimeIdentityData
    control_ready: StrictBool
    application_ready: StrictBool
    available_operations: tuple[ProjectCapability, ...] = Field(max_length=10)
    storage_state: Literal["not_checked", "initializing", "ready", "failed", "closing"]
    storage_error: StorageIssueCode | None = None
    active_retention_days: ConfiguredRetentionDays | None = None
    settings_source: Literal["default_file", "existing_file"] | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.control_ready != (self.state == RuntimeState.RUNNING):
            raise ValueError("Only running control is ready.")
        if len(set(self.available_operations)) != len(self.available_operations):
            raise ValueError("Capabilities cannot be duplicated.")
        has_facts = self.active_retention_days is not None and self.settings_source is not None
        any_facts = self.active_retention_days is not None or self.settings_source is not None
        if (self.storage_state == "ready" and not has_facts) or (
            self.storage_state != "ready" and any_facts
        ):
            raise ValueError("Only ready cached storage has complete active facts.")
        if (self.storage_state == "failed") != (self.storage_error is not None):
            raise ValueError("Only failed storage carries an error.")
        if self.available_operations and not (self.control_ready and self.storage_state == "ready"):
            raise ValueError("Capabilities require ready control and storage.")
        if self.application_ready and not (
            self.control_ready
            and self.storage_state == "ready"
            and set(self.available_operations)
            == {
                "ask.branch",
                "ask.changes",
                "init",
                "config",
                "receipt.inspect",
                "activity.trace",
                "activity.replay",
                "debug.last",
                "history.list",
                "activity.subscribe",
            }
        ):
            raise ValueError("Full doctor readiness requires all supported operations.")
        return self


class DoctorReport(_DoctorModel):
    summary: Literal[
        "healthy", "stopped", "uninitialized", "unhealthy", "unavailable", "unsupported"
    ]
    checks: tuple[DoctorCheck, ...] = Field(min_length=7, max_length=7)
    runtime: DoctorRuntimeData | None = None
    paths: RuntimePathInspection | None = None
    settings_schema_version: StrictInt | None = Field(default=None, ge=1, le=1)
    configured_retention_days: ConfiguredRetentionDays | None = None
    database_schema_version: StrictInt | None = Field(default=None, ge=0, le=4)
    python_version: str | None = Field(
        default=None, min_length=1, max_length=32, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$"
    )
    git_version: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^git version [0-9][0-9A-Za-z .()+_-]*$"
    )

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if tuple(check.name for check in self.checks) != DOCTOR_CHECKS:
            raise ValueError("Each diagnostic check is required exactly once in declared order.")
        failed = any(check.required and check.status != "ok" for check in self.checks)
        if (self.summary in ("healthy", "stopped", "uninitialized")) == failed:
            raise ValueError("Summary must reflect all required checks.")
        if (self.settings_schema_version is None) != (self.configured_retention_days is None):
            raise ValueError("Configured settings facts must be complete.")
        if self.settings_schema_version is not None and self.checks[5].status != "ok":
            raise ValueError("Only validated settings expose configuration facts.")
        if self.summary == "stopped" and (
            self.runtime is not None
            or self.settings_schema_version != 1
            or self.configured_retention_days is None
            or self.database_schema_version != 4
        ):
            raise ValueError(
                "Stopped health requires current validated facts without live identity."
            )
        if self.runtime is not None and (
            self.settings_schema_version is not None or self.configured_retention_days is not None
        ):
            raise ValueError("Live cached facts cannot claim on-disk configured settings.")
        if self.summary == "healthy" and (
            self.runtime is None
            or not self.runtime.application_ready
            or self.database_schema_version != 4
        ):
            raise ValueError("Healthy requires authenticated full cached readiness.")
        return self


class DoctorError(_DoctorModel):
    code: Literal["doctor_unhealthy", "doctor_unavailable"]
    message: Literal["Required diagnostics are unhealthy.", "Required diagnostics are unavailable."]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        expected = (
            "Required diagnostics are unavailable."
            if self.code == "doctor_unavailable"
            else "Required diagnostics are unhealthy."
        )
        if self.message != expected:
            raise ValueError("Diagnostic errors have fixed descriptions.")
        return self


class DoctorResult(_DoctorModel):
    ok: StrictBool
    data: DoctorReport
    error: DoctorError | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        healthy = self.data.summary in ("healthy", "stopped", "uninitialized")
        if self.ok != healthy or self.ok != (self.error is None):
            raise ValueError("Diagnostic result must preserve report health and error status.")
        if self.error is not None:
            unavailable = self.data.summary == "unavailable"
            if unavailable != (self.error.code == "doctor_unavailable"):
                raise ValueError("Diagnostic error must match the report summary.")
        if len(self.model_dump_json().encode("utf-8")) > 8192:
            raise ValueError("Diagnostic report exceeds its public output budget.")
        return self


def doctor_result(report: DoctorReport) -> DoctorResult:
    if report.summary in ("healthy", "stopped", "uninitialized"):
        return DoctorResult(ok=True, data=report)
    unavailable = report.summary == "unavailable"
    return DoctorResult(
        ok=False,
        data=report,
        error=DoctorError(
            code="doctor_unavailable" if unavailable else "doctor_unhealthy",
            message=(
                "Required diagnostics are unavailable."
                if unavailable
                else "Required diagnostics are unhealthy."
            ),
        ),
    )


def unavailable_doctor() -> DoctorResult:
    """Backward-compatible CLI composition without a diagnostics implementation."""
    return doctor_result(
        DoctorReport(
            summary="unavailable",
            checks=tuple(
                DoctorCheck(
                    name=name, status="unavailable", code="diagnostic_not_available", required=True
                )
                for name in DOCTOR_CHECKS
            ),
        )
    )


class DiagnosticDeadlineExceeded(Exception):
    """The shared diagnostic budget expired; carries no raw I/O details."""


class RuntimeDiagnostics(Protocol):
    def inspect(self) -> DoctorResult: ...

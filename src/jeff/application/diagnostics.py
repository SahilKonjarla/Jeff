"""Bounded read-only installation diagnostics; no concrete I/O or repair authority."""

from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from jeff.application.runtime import ControlObservation, RuntimePathInspection

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
]


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


class DoctorReport(_DoctorModel):
    summary: Literal[
        "healthy", "stopped", "uninitialized", "unhealthy", "unavailable", "unsupported"
    ]
    checks: tuple[DoctorCheck, ...] = Field(min_length=7, max_length=7)
    runtime: ControlObservation | None = None
    paths: RuntimePathInspection | None = None
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


class RuntimeDiagnostics(Protocol):
    def inspect(self) -> DoctorResult: ...

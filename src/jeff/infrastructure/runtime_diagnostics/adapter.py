"""Read-only report assembly under one deadline, with the original diagnostic dispatch seams."""

from collections.abc import Callable
from typing import Literal

from jeff.application.diagnostics import DoctorCheck, DoctorReason, DoctorResult, DoctorRuntimeData
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import LoadedRuntimeSettings

from .. import runtime_diagnostics as _native
from . import live, reasons, stopped


class RuntimeDiagnosticsAdapter:
    """Project finite facts under one budget without becoming another runtime owner."""

    def __init__(self, layout: Callable[[], RuntimeLayout]) -> None:
        self._layout = layout

    def inspect(self) -> DoctorResult:
        """Share one deadline across checks; uncertainty is a report, not repair authority."""
        deadline = _native.time.monotonic() + 5.0
        checks = [
            _native.DoctorCheck(
                name="platform",
                status="ok" if _native.sys.platform == "darwin" else "failed",
                code=None if _native.sys.platform == "darwin" else "unsupported_runtime_platform",
                required=True,
            ),
            _native.DoctorCheck(
                name="python",
                status="ok" if _native.sys.version_info >= (3, 12) else "failed",
                code=None if _native.sys.version_info >= (3, 12) else "python_unsupported",
                required=True,
            ),
        ]
        python_version = ".".join(map(str, _native.sys.version_info[:3]))
        git_version = None
        git_reason: _native.DoctorReason | None = None
        git_status: Literal["ok", "failed", "missing", "unavailable"] = "ok"
        try:
            remaining = deadline - _native.time.monotonic()
            if remaining <= 0:
                raise _native.subprocess.TimeoutExpired("git", 0)
            probe = _native.subprocess.run(
                ["/usr/bin/git", "--version"],
                stdin=_native.subprocess.DEVNULL,
                capture_output=True,
                timeout=remaining,
                env={
                    "PATH": _native.os.defpath,
                    "LC_ALL": "C",
                    "GIT_CONFIG_GLOBAL": _native.os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
            )
            version = probe.stdout.rstrip(b"\n")
            if (
                probe.returncode != 0
                or not 0 < len(version) <= 64
                or _native.re.fullmatch(rb"git version [0-9][0-9A-Za-z .()+_-]*", version) is None
            ):
                git_status, git_reason = "failed", "git_probe_failed"
            else:
                git_version = version.decode("ascii")
        except FileNotFoundError:
            git_status, git_reason = "missing", "git_unavailable"
        except _native.subprocess.TimeoutExpired:
            git_status, git_reason = "unavailable", "diagnostic_deadline_exceeded"
        except OSError:
            git_status, git_reason = "failed", "git_probe_failed"
        checks.append(
            _native.DoctorCheck(name="git", status=git_status, code=git_reason, required=True)
        )

        paths: _native.RuntimePathInspection | None = None
        layout: _native.RuntimeLayout | None = None
        reason: _native.DoctorReason = "diagnostic_not_available"
        if _native.time.monotonic() >= deadline:
            reason = "diagnostic_deadline_exceeded"
        elif _native.sys.platform != "darwin":
            reason = "unsupported_runtime_platform"
        else:
            try:
                layout = self._layout()
                observed = _native.inspect_layout(layout)
                if isinstance(observed, _native.Failure):
                    reason = self._path_reason(observed.error.code)
                else:
                    paths = observed
            except _native.ControlFault as error:
                reason = self._path_reason(error.code)
            except (OSError, ValueError):
                reason = "runtime_inspection_failed"

        expired = _native.time.monotonic() >= deadline
        absent = paths is not None and all(entry.state == "missing" for entry in paths.entries)
        unsafe = paths is not None and any(entry.state == "unsafe" for entry in paths.entries)
        unknown = paths is not None and any(entry.state == "unknown" for entry in paths.entries)
        ignored_missing = {"socket"}
        if layout is not None and stopped.postgres_expected(layout):
            ignored_missing.add("database")  # PostgreSQL deliberately has no local SQLite file.
        incomplete = paths is not None and any(
            entry.state == "missing" for entry in paths.entries if entry.role not in ignored_missing
        )
        if expired:
            reason = "diagnostic_deadline_exceeded"
        for name in _native.DOCTOR_CHECKS[3:]:
            if expired:
                check = _native.DoctorCheck(
                    name=name, status="unavailable", code=reason, required=True
                )
            elif unsafe and name == "paths":
                check = _native.DoctorCheck(
                    name=name, status="unsafe", code="unsafe_runtime_path", required=True
                )
            elif absent:
                check = _native.DoctorCheck(
                    name=name, status="missing", code="installation_absent", required=False
                )
            elif incomplete and name == "paths":
                check = _native.DoctorCheck(
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
                check = _native.DoctorCheck(name=name, status="failed", code=reason, required=True)
            elif name == "paths" and paths is not None and not unknown:
                check = _native.DoctorCheck(name=name, status="ok", required=True)
            else:
                check = _native.DoctorCheck(
                    name=name,
                    status="unavailable",
                    code="runtime_inspection_failed" if unknown else reason,
                    required=True,
                )
            checks.append(check)
        settings: _native.LoadedRuntimeSettings | None = None
        database_version: int | None = None
        runtime: _native.DoctorRuntimeData | None = None
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
                checks[3] = _native.DoctorCheck(
                    name="paths", status="unsafe", code="unsafe_runtime_path", required=True
                )
            checks[4:] = stopped_checks
        summary: Literal[
            "unsupported", "unhealthy", "unavailable", "uninitialized", "stopped", "healthy"
        ]
        if _native.sys.platform != "darwin" or _native.sys.version_info < (3, 12):
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
        return _native.doctor_result(
            _native.DoctorReport(
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
    ) -> tuple[
        list[DoctorCheck], LoadedRuntimeSettings | None, int | None, DoctorRuntimeData | None
    ]:
        """Use authenticated cached live facts, never client-side live settings/SQL reads."""
        return live._runtime(layout, deadline)

    @staticmethod
    def _control_reason(code: str) -> DoctorReason:
        return reasons._control_reason(code)

    @staticmethod
    def _stopped(
        layout: RuntimeLayout, deadline: float
    ) -> tuple[list[DoctorCheck], LoadedRuntimeSettings | None, int | None]:
        """Hold a non-creating lease through all stopped reads and final revalidation."""
        return stopped._stopped(layout, deadline)

    @staticmethod
    def _path_reason(code: str) -> DoctorReason:
        return reasons._path_reason(code)

"""Read-only metadata/tool diagnostics; no lifecycle, credential or SQL-owner access."""

import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Literal, cast

from jeff.application.diagnostics import (
    DOCTOR_CHECKS,
    DoctorCheck,
    DoctorReason,
    DoctorReport,
    DoctorResult,
    doctor_result,
)
from jeff.application.projects import Failure
from jeff.application.runtime import ControlFault, RuntimeLayout, RuntimePathInspection
from jeff.infrastructure.runtime_paths import inspect_layout

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
        reason: DoctorReason = "diagnostic_not_available"
        if time.monotonic() >= deadline:
            reason = "diagnostic_deadline_exceeded"
        elif sys.platform != "darwin":
            reason = "unsupported_runtime_platform"
        else:
            try:
                observed = inspect_layout(self._layout())
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
        summary: Literal["unsupported", "unhealthy", "unavailable", "uninitialized"]
        if sys.platform != "darwin" or sys.version_info < (3, 12):
            summary = "unsupported"
        elif any(
            check.required and check.status in ("unsafe", "failed", "missing") for check in checks
        ):
            summary = "unhealthy"
        elif any(check.required and check.status != "ok" for check in checks):
            summary = "unavailable"
        else:
            summary = "uninitialized"
        return doctor_result(
            DoctorReport(
                summary=summary,
                checks=tuple(checks),
                paths=paths,
                python_version=python_version,
                git_version=git_version,
            )
        )

    @staticmethod
    def _path_reason(code: str) -> DoctorReason:
        return cast(DoctorReason, code) if code in _PATH_REASONS else "diagnostic_not_available"

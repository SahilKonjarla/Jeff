"""Validate one untrusted pytest report against facts from the approved job.

A validated report is still workload data. Text must pass the output privacy
policy before persistence or display. This module does not read the filesystem.
"""

import json
import re
from collections.abc import Collection
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import Field, StrictInt, ValidationError, field_validator, model_validator

from ._model import _Model

PYTEST_REPORT_BYTES = 64 * 1024
PYTEST_RUNNER_ARGV = ("/usr/local/bin/python", "-I", "-B", "/opt/jeff/pytest_job_runner.py")


def pytest_run_token(argv: tuple[str, ...]) -> str | None:
    if argv[:-1] == PYTEST_RUNNER_ARGV and re.fullmatch(r"[a-f0-9]{32}", argv[-1]):
        return argv[-1]
    return None


PytestReportStatus = Literal["available", "missing", "oversized", "invalid", "withheld"]


class PytestFailure(_Model):
    node_id: str = Field(strict=True, min_length=1, max_length=1024)
    path: str = Field(strict=True, min_length=1, max_length=512)
    line: StrictInt = Field(ge=1, le=2147483647)
    excerpt: str = Field(strict=True, max_length=2048)

    @field_validator("node_id", "path", "excerpt")
    @classmethod
    def valid_text(cls, value: str) -> str:
        if any(not char.isprintable() and char != "\n" for char in value):
            raise ValueError("Unsupported report text.")
        return value


class PytestReport(_Model):
    version: Literal[1]
    run_token: str = Field(strict=True, pattern=r"^[a-f0-9]{32}$")
    passed: StrictInt = Field(ge=0, le=1000000)
    failed: StrictInt = Field(ge=0, le=128)
    exit_status: StrictInt = Field(ge=0, le=1)
    failures: tuple[PytestFailure, ...] = Field(max_length=128)


class PytestResult(_Model):
    status: PytestReportStatus
    report: PytestReport | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.status == "available") != (self.report is not None):
            raise ValueError("Inconsistent pytest report status.")
        return self


class PytestReportBuffer:
    """Memory-only stdout buffer; overflow discards the entire candidate report."""

    def __init__(self) -> None:
        self._data = bytearray()
        self._oversized = False

    def accept(self, chunk: bytes) -> None:
        if self._oversized:
            return
        if len(chunk) > PYTEST_REPORT_BYTES - len(self._data):
            self._data.clear()
            self._oversized = True
            return
        self._data.extend(chunk)

    def discard(self) -> None:
        self._data.clear()

    def finish(
        self,
        *,
        complete: bool,
        run_token: str,
        exit_status: int,
        approved_paths: Collection[str],
    ) -> tuple[PytestReportStatus, PytestReport | None]:
        try:
            if self._oversized:
                return "oversized", None
            if not complete:
                return "invalid", None
            return read_pytest_report(
                bytes(self._data),
                run_token=run_token,
                exit_status=exit_status,
                approved_paths=approved_paths,
            )
        finally:
            self._data.clear()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate report key.")
        result[key] = value
    return result


def _project_source(failure: PytestFailure, approved_paths: Collection[str]) -> bool:
    path = PurePosixPath(failure.path)
    return (
        not path.is_absolute()
        and str(path) == failure.path
        and all(part not in (".", "..") for part in path.parts)
        and not any(char in failure.path for char in ("\n", "\\", ":"))
        and "\n" not in failure.node_id
        and failure.path in approved_paths
        and failure.node_id.startswith(failure.path + "::")
        and len(failure.node_id) > len(failure.path) + 2
    )


def read_pytest_report(
    data: bytes,
    *,
    run_token: str,
    exit_status: int,
    approved_paths: Collection[str],
) -> tuple[PytestReportStatus, PytestReport | None]:
    """Accept a complete payload only; the caller must establish verified job completion.

    The token binds a report to a run but does not authenticate statements from
    project code. Approved paths come from the prepared project view, not the
    report or the current host checkout.
    """
    if not data:
        return "missing", None
    if len(data) > PYTEST_REPORT_BYTES:
        return "oversized", None
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(document, dict) or type(document.get("version")) is not int:
            return "invalid", None
        report = PytestReport.model_validate(document)
    except (ValidationError, ValueError, RecursionError):
        return "invalid", None
    if (
        report.run_token != run_token
        or report.exit_status != exit_status
        or report.failed != len(report.failures)
        or bool(report.failed) != (report.exit_status == 1)
        or report.passed + report.failed == 0
        or len({failure.node_id for failure in report.failures}) != report.failed
        or any(not _project_source(failure, approved_paths) for failure in report.failures)
    ):
        return "invalid", None
    return "available", report

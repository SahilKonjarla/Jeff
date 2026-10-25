"""Public doctor value and CLI seams; no concrete runtime/SQL collaborators."""

import json
from typing import NoReturn

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from jeff.application.diagnostics import (
    DoctorCheck,
    DoctorReport,
    DoctorResult,
    doctor_result,
    unavailable_doctor,
)
from jeff.interfaces.cli import create_app


def report() -> DoctorReport:
    names = ("platform", "python", "git", "paths", "runtime", "settings", "storage")
    return DoctorReport.model_validate(
        {
            "summary": "uninitialized",
            "checks": [
                {
                    "name": name,
                    "status": "ok" if index < 3 else "missing",
                    "code": None if index < 3 else "installation_absent",
                    "required": index < 3,
                }
                for index, name in enumerate(names)
            ],
            "python_version": "3.12.11",
            "git_version": "git version 2.39.5",
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "ok", "code": "installation_absent"},
        {"status": "failed", "code": None},
        {"status": "unsafe", "code": "unsafe_runtime_path", "required": False},
        {"code": "/synthetic/private/reason"},
        {"detail": "synthetic private payload"},
        {"required": "true"},
    ],
)
def test_check_rejects_contradictory_or_unbounded_values(changes: dict[str, object]) -> None:
    value: dict[str, object] = {
        "name": "paths",
        "status": "missing",
        "code": "installation_absent",
        "required": True,
    }
    with pytest.raises(ValidationError):
        DoctorCheck.model_validate({**value, **changes})


@pytest.mark.parametrize("alteration", ["short", "duplicate", "order", "python", "git", "summary"])
def test_report_rejects_invalid_shape_and_false_health(alteration: str) -> None:
    value = report().model_dump(mode="json")
    if alteration == "short":
        value["checks"].pop()
    elif alteration == "duplicate":
        value["checks"][1] = value["checks"][0]
    elif alteration == "order":
        value["checks"].reverse()
    elif alteration == "python":
        value["python_version"] = "3" * 100
    elif alteration == "git":
        value["git_version"] = "git version 2\nSYNTHETIC_PRIVATE"
    else:
        value["checks"][0] = {
            "name": "platform",
            "status": "failed",
            "code": "unsupported_runtime_platform",
            "required": True,
        }
    with pytest.raises(ValidationError):
        DoctorReport.model_validate(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"ok": False},
        {
            "ok": True,
            "error": {
                "code": "doctor_unavailable",
                "message": "Required diagnostics are unavailable.",
            },
        },
        {"ok": "true"},
    ],
)
def test_result_preserves_report_health(changes: dict[str, object]) -> None:
    value = doctor_result(report()).model_dump(mode="json")
    with pytest.raises(ValidationError):
        DoctorResult.model_validate({**value, **changes})


def test_reports_are_frozen_and_failure_keeps_bounded_data() -> None:
    result = unavailable_doctor()
    assert not result.ok and result.error is not None
    assert result.error.code == "doctor_unavailable"
    assert result.data.summary == "unavailable" and len(result.data.checks) == 7
    with pytest.raises(ValidationError):
        result.data.summary = "healthy"


def forbidden() -> NoReturn:
    raise AssertionError("Help/syntax must not select any installation or construct clients")


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["doctor", "--help"],
        ["doctor", "--repair"],
        ["doctor", "unexpected"],
    ],
)
def test_doctor_help_and_syntax_invoke_no_factories(arguments: list[str]) -> None:
    result = CliRunner().invoke(create_app(forbidden, forbidden, forbidden), arguments)
    assert result.exit_code == (0 if "--help" in arguments else 2), result.exception


@pytest.mark.parametrize("json_output", [False, True])
def test_two_factory_callers_get_honest_unavailable_doctor(json_output: bool) -> None:
    result = CliRunner().invoke(
        create_app(forbidden, forbidden), ["doctor", *(["--json"] if json_output else [])]
    )
    assert result.exit_code == 1, result.exception
    if json_output:
        payload = json.loads(result.stdout)
        assert payload["data"]["summary"] == "unavailable"
        assert payload["error"]["code"] == "doctor_unavailable" and payload["ok"] is False
    else:
        assert "Doctor: unavailable" in result.stdout
        assert "doctor_unavailable" in result.stderr


def test_doctor_uses_only_diagnostics_port_and_preserves_failure_report() -> None:
    class Diagnostics:
        def inspect(self) -> DoctorResult:
            return unavailable_doctor()

    result = CliRunner().invoke(create_app(forbidden, forbidden, Diagnostics), ["doctor", "--json"])
    assert result.exit_code == 1, result.exception
    assert json.loads(result.stdout) == unavailable_doctor().model_dump(mode="json")

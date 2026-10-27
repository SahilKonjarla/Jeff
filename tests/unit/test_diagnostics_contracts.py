"""Public doctor value and CLI seams; no concrete runtime/SQL collaborators."""

import json
from typing import Any, NoReturn

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from jeff.application.diagnostics import (
    DoctorCheck,
    DoctorReport,
    DoctorResult,
    DoctorRuntimeData,
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


def stopped_payload() -> dict[str, Any]:
    value = report().model_dump(mode="json")
    value.update(
        summary="stopped",
        settings_schema_version=1,
        configured_retention_days=17,
        database_schema_version=4,
    )
    value["checks"] = [
        {"name": name, "status": "ok", "code": None, "required": True}
        for name in ("platform", "python", "git", "paths", "runtime", "settings", "storage")
    ]
    return value


@pytest.mark.parametrize("value", [0, -1, 2**63, "17", 17.0, "unbounded private text"])
def test_configured_retention_summary_is_bounded_and_strict(value: object) -> None:
    payload = stopped_payload()
    payload["configured_retention_days"] = value
    with pytest.raises(ValidationError):
        DoctorReport.model_validate(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"configured_retention_days": None},
        {"settings_schema_version": None},
        {"database_schema_version": 3},
        {"runtime": {"state": "stopped"}},
    ],
)
def test_stopped_health_requires_complete_current_nonlive_facts(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DoctorReport.model_validate({**stopped_payload(), **changes})


def test_valid_oversized_configuration_uses_finite_marker_not_active_retention() -> None:
    payload = stopped_payload()
    payload["configured_retention_days"] = "above_report_limit"
    result = doctor_result(DoctorReport.model_validate(payload))
    assert result.ok and result.data.runtime is None
    assert result.data.configured_retention_days == "above_report_limit"


def live_payload() -> dict[str, Any]:
    return {
        "state": "running",
        "identity": {"instance_id": "00000000-0000-4000-8000-000000000001", "pid": 1},
        "control_ready": True,
        "application_ready": True,
        "available_operations": [
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
        ],
        "storage_state": "ready",
        "storage_error": None,
        "active_retention_days": 30,
        "settings_source": "existing_file",
    }


@pytest.mark.parametrize(
    "change",
    [
        {"active_retention_days": 2**63},
        {"active_retention_days": 0},
        {"active_retention_days": None},
        {"settings_source": None},
        {"state": "stopped"},
        {"control_ready": False},
        {"storage_error": "storage_failed"},
        {"raw_config": "SYNTHETIC_PRIVATE"},
        {"available_operations": ["ask.branch"]},
        {"available_operations": ["ask.branch"] * 10},
    ],
)
def test_live_projection_is_bounded_and_coherent(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DoctorRuntimeData.model_validate({**live_payload(), **change})


@pytest.mark.parametrize("change", ["absent", "not_ready", "schema", "configured", "capability"])
def test_healthy_summary_requires_verified_current_full_readiness(change: str) -> None:
    value = stopped_payload()
    value.update(
        summary="healthy",
        settings_schema_version=None,
        configured_retention_days=None,
        runtime=live_payload(),
    )
    if change == "absent":
        value["runtime"] = None
    elif change == "not_ready":
        value["runtime"]["application_ready"] = False
    elif change == "schema":
        value["database_schema_version"] = 3
    elif change == "configured":
        value.update(settings_schema_version=1, configured_retention_days=17)
    else:
        value["runtime"]["available_operations"].remove("activity.subscribe")
    with pytest.raises(ValidationError):
        DoctorReport.model_validate(value)


def test_stopping_cache_can_be_retained_only_as_unavailable_not_healthy() -> None:
    value = unavailable_doctor().data.model_dump(mode="json")
    value["runtime"] = {
        **live_payload(),
        "state": "stopping",
        "control_ready": False,
        "application_ready": False,
        "available_operations": [],
        "storage_state": "closing",
        "active_retention_days": None,
        "settings_source": None,
    }
    result = doctor_result(DoctorReport.model_validate(value))
    assert not result.ok and result.data.runtime is not None
    assert result.data.runtime.state.value == "stopping"
    assert len(result.model_dump_json().encode()) < 8192


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

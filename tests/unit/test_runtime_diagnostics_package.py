"""Diagnostic report bytes, public interfaces and continuous read-only observation seams."""

import inspect
import json
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import get_type_hints

import pytest

from jeff.application import runtime as runtime_contracts
from jeff.application.diagnostics import DoctorCheck
from jeff.application.runtime import (
    RUNTIME_PATH_ROLES,
    PathObservation,
    RuntimeLayout,
    RuntimePathInspection,
)
from jeff.application.storage import RuntimeSettings
from jeff.infrastructure import runtime_diagnostics as native

INTERFACE_DIGEST = "b8fa4eefd7925a55da16e45e9f1aed08c272999bcf0c42df3f0a01e190c143ff"
VECTOR_DIGEST = "f12477c900698954641a21c1f6bf74c54d83af0e2b6006f53f2dde4d478eea9b"
SCHEMA_DIGEST = "e3d6a985eef6392f39fdf7f5e90f6c2d0bd93703271b990250cc658d453fdc2a"


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _canonical_runtime(value: str) -> str:
    for name, item in vars(runtime_contracts).items():
        if inspect.isclass(item) and item.__module__.startswith("jeff.application.runtime"):
            value = value.replace(
                f"{item.__module__}.{item.__qualname__}", f"jeff.application.runtime.{name}"
            )
    return value


def _interfaces() -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in vars(native.RuntimeDiagnosticsAdapter).items():
        kind = "method"
        if isinstance(value, staticmethod):
            value, kind = value.__func__, "static"
        if inspect.isfunction(value):
            result[name] = {
                "signature": _canonical_runtime(str(inspect.signature(value))),
                "hints": {
                    key: _canonical_runtime(str(hint))
                    for key, hint in get_type_hints(value).items()
                },
                "kind": kind,
                "doc": inspect.getdoc(value),
            }
    return result


def _schemas() -> dict[str, object]:
    return {
        name: getattr(native, name).model_json_schema()
        for name in ("DoctorCheck", "DoctorReport", "DoctorResult", "DoctorRuntimeData")
    }


def _settings(days: int = 30) -> RuntimeSettings:
    return RuntimeSettings.model_validate(
        {"schema_version": 1, "history": {"retention_days": days}}
    )


def _vectors() -> dict[str, str]:
    layout = RuntimeLayout(data_dir=Path("/synthetic/d"), runtime_dir=Path("/synthetic/r"), uid=1)
    result = {}
    for mode in (
        "absent",
        "stopped",
        "large",
        "unsafe",
        "unknown",
        "incomplete",
        "invalid",
        "expired",
    ):
        clock = [10.0]
        entries = [
            PathObservation(role=role, state="present" if role != "socket" else "missing")
            for role in RUNTIME_PATH_ROLES
        ]
        if mode == "absent":
            entries = [PathObservation(role=role, state="missing") for role in RUNTIME_PATH_ROLES]
        elif mode == "unsafe":
            entries[0] = PathObservation(
                role=entries[0].role, state="unsafe", issue="unsafe_runtime_path"
            )
        elif mode == "unknown":
            entries[0] = PathObservation(
                role=entries[0].role, state="unknown", issue="runtime_inspection_failed"
            )
        elif mode == "incomplete":
            entries[0] = PathObservation(role=entries[0].role, state="missing")
        observed = RuntimePathInspection(entries=tuple(entries))

        def paths(
            selected: RuntimeLayout, *, mode: str = mode, observed: RuntimePathInspection = observed
        ) -> RuntimePathInspection:
            assert selected is layout
            if mode == "invalid":
                raise native.ControlFault("invalid_runtime_path")
            return observed

        def probe(
            *args: object, mode: str = mode, clock: list[float] = clock, **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            assert args[0] == ["/usr/bin/git", "--version"]
            assert kwargs["timeout"] == 5.0
            if mode == "expired":
                clock[0] = 16.0
            return subprocess.CompletedProcess([], 0, b"git version 2.39.5\n", b"")

        class Adapter(native.RuntimeDiagnosticsAdapter):
            @staticmethod
            def _runtime(
                layout: RuntimeLayout, deadline: float, mode: str = mode
            ) -> tuple[
                list[native.DoctorCheck],
                native.LoadedRuntimeSettings | None,
                int | None,
                native.DoctorRuntimeData | None,
            ]:
                assert deadline == 15.0
                return (
                    [
                        DoctorCheck.model_validate(dict(name=name, status="ok", required=True))
                        for name in ("runtime", "settings", "storage")
                    ],
                    _settings(10**60 if mode == "large" else 30),
                    10,
                    None,
                )

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sys, "platform", "darwin")
            patch.setattr(sys, "version_info", (3, 12, 0))
            patch.setattr(time, "monotonic", lambda clock=clock: clock[0])
            patch.setattr(subprocess, "run", probe)
            patch.setattr(native, "inspect_layout", paths)
            result[mode] = Adapter(lambda: layout).inspect().model_dump_json()
    return result


def test_original_interfaces_static_descriptors_and_resolved_hints_match() -> None:
    assert _digest(_interfaces()) == INTERFACE_DIGEST


def test_report_wire_schemas_remain_identical() -> None:
    assert _digest(_schemas()) == SCHEMA_DIGEST


def test_serialized_report_vectors_and_single_deadline_are_preserved() -> None:
    assert _digest(_vectors()) == VECTOR_DIGEST


def test_path_reason_override_remains_same_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []
    layout = RuntimeLayout(data_dir=Path("/synthetic/d"), runtime_dir=Path("/synthetic/r"), uid=1)

    adapter = native.RuntimeDiagnosticsAdapter(lambda: layout)

    def reason(code: str) -> native.DoctorReason:
        seen.append(adapter)
        return "invalid_runtime_path"

    def fail(layout: RuntimeLayout) -> native.RuntimePathInspection:
        raise native.ControlFault("invalid_runtime_path")

    monkeypatch.setattr(native, "inspect_layout", fail)
    monkeypatch.setattr(adapter, "_path_reason", reason)
    assert adapter.inspect().data.summary == "unhealthy"
    assert seen == [adapter]


def test_stopped_class_hook_runs_inside_observation_not_after_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    layout = RuntimeLayout(data_dir=Path("/synthetic/d"), runtime_dir=Path("/synthetic/r"), uid=1)

    class Observation:
        held = False

    class Store:
        def __init__(self, selected: RuntimeLayout) -> None:
            assert selected is layout

        @contextmanager
        def diagnostic_observation(self, deadline: float) -> Iterator[Observation]:
            assert deadline == 15.0
            events.append("enter")
            try:
                yield Observation()
            finally:
                events.append("exit")

    def stopped(
        selected: RuntimeLayout, deadline: float
    ) -> tuple[list[native.DoctorCheck], native.LoadedRuntimeSettings | None, int | None]:
        assert selected is layout and deadline == 15.0 and events == ["enter"]
        events.append("stopped")
        return [], None, None

    monkeypatch.setattr(native, "RuntimeStateStore", Store)
    monkeypatch.setattr(native.RuntimeDiagnosticsAdapter, "_stopped", staticmethod(stopped))
    assert native.RuntimeDiagnosticsAdapter._runtime(layout, 15.0) == ([], None, None, None)
    assert events == ["enter", "stopped", "exit"]


@pytest.mark.parametrize("fault", [None, "deadline", "ownership"])
def test_stopped_reads_keep_continuous_lease_and_discard_failed_facts(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    events: list[str] = []
    layout = RuntimeLayout(data_dir=Path("/synthetic/d"), runtime_dir=Path("/synthetic/r"), uid=1)

    class Lease:
        def check(self) -> None:
            events.append("check")
            if fault == "deadline":
                raise native.DiagnosticDeadlineExceeded
            if fault == "ownership":
                raise native.ControlFault("runtime_ownership_unconfirmed")

    lease = Lease()

    class Store:
        def __init__(self, selected: RuntimeLayout) -> None:
            assert selected is layout

        @contextmanager
        def diagnostic_lease(self, deadline: float) -> Iterator[Lease]:
            assert deadline == 15.0
            events.append("enter")
            try:
                yield lease
            finally:
                events.append("exit")

    def settings(selected: Lease) -> RuntimeSettings:
        assert selected is lease and events == ["enter"]
        events.append("settings")
        return _settings()

    def storage(selected: Lease, deadline: float) -> int:
        assert selected is lease and deadline == 15.0 and events == ["enter", "settings"]
        events.append("storage")
        return 10

    monkeypatch.setattr(native, "RuntimeStateStore", Store)
    monkeypatch.setattr(native, "read_existing_settings", settings)
    monkeypatch.setattr(native, "inspect_stopped_storage", storage)
    checks, settings_value, version = native.RuntimeDiagnosticsAdapter._stopped(layout, 15.0)
    assert events == ["enter", "settings", "storage", "check", "exit"]
    if fault is None:
        assert settings_value == _settings() and version == 10
    else:
        assert settings_value is None and version is None
        assert all(
            check.code
            == (
                "diagnostic_deadline_exceeded"
                if fault == "deadline"
                else "runtime_ownership_unconfirmed"
            )
            for check in checks
        )


@pytest.mark.parametrize("fault", [None, "capability", "ownership"])
def test_live_observation_keeps_cached_facts_and_never_reads_settings_or_sql(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from uuid import UUID

    from jeff.application.runtime import ControlObservation, ProjectCapability, RuntimeIdentityData
    from jeff.application.storage import StorageReadiness
    from jeff.domain.runtime import RuntimeState

    events: list[str] = []
    layout = RuntimeLayout(data_dir=Path("/synthetic/d"), runtime_dir=Path("/synthetic/r"), uid=1)
    operations: tuple[ProjectCapability, ...] = (
        "ask.branch",
        "ask.changes",
        "init",
        "config",
        "receipt.inspect",
        "activity.trace",
        "activity.replay",
        "debug.last",
        "history.list",
        "execution.review",
    )
    observed = ControlObservation(
        state=RuntimeState.RUNNING,
        identity=RuntimeIdentityData(instance_id=UUID(int=1), pid=1),
        control_ready=True,
        application_ready=True,
        available_operations=operations
        if fault == "capability"
        else (*operations, "activity.subscribe"),
        storage=StorageReadiness(
            state="ready", schema_version=11, retention_days=30, settings_source="existing_file"
        ),
    )

    class Observation:
        held = has_endpoint = True

        def check(self) -> None:
            events.append("check")
            if fault == "ownership":
                raise native.ControlFault("runtime_ownership_unconfirmed")

    class Store:
        def __init__(self, selected: RuntimeLayout) -> None:
            assert selected is layout

        @contextmanager
        def diagnostic_observation(self, deadline: float) -> Iterator[Observation]:
            assert deadline == 15.0
            events.append("enter")
            try:
                yield Observation()
            finally:
                events.append("exit")

    def cache(selected: RuntimeLayout, deadline: float) -> ControlObservation:
        assert selected is layout and deadline == 15.0 and events == ["enter"]
        events.append("cache")
        return observed

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("A live diagnostic must not read client-side settings or SQL")

    monkeypatch.setattr(native, "RuntimeStateStore", Store)
    monkeypatch.setattr(native, "inspect_control", cache)
    monkeypatch.setattr(time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(native, "read_existing_settings", forbidden)
    monkeypatch.setattr(native, "inspect_stopped_storage", forbidden)
    checks, settings, version, runtime = native.RuntimeDiagnosticsAdapter._runtime(layout, 15.0)
    assert events == ["enter", "cache", "check", "exit"]
    assert settings is None
    if fault is None:
        assert runtime is not None and runtime.identity == observed.identity and version == 10
        assert all(check.status == "ok" for check in checks)
    else:
        assert runtime is None and version is None
        expected = (
            "runtime_capability_unavailable"
            if fault == "capability"
            else "runtime_ownership_unconfirmed"
        )
        assert all(check.code == expected for check in checks)


def test_package_size_limit(code_line_count: Callable[[str], int]) -> None:
    for path in Path(native.__file__).parent.glob("*.py"):
        assert code_line_count(path.read_text()) <= 400, path.name

"""Bootstrap public composition metadata and lazy root hooks without runtime launches."""

import inspect
import json
import runpy
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import cast, get_type_hints

import pytest

from jeff.application.runtime import RuntimeLayout, RuntimePathInputs
from jeff.bootstrap import runtime
from jeff.infrastructure.postgres_execution_actor import PostgresExecutionActor
from jeff.infrastructure.runtime_diagnostics import RuntimeDiagnosticsAdapter

LEGACY_INTERFACE_DIGEST = "003a031373c3ec387159e7f92c4a70cb26cb87558ab9c6e611ba5114843dc71a"
INTERFACE_DIGEST = "f8a00b983bb5d0328fa92b336c103d762f146a134df0b802cb77a95a966a2e8c"


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _globals(value: Callable[..., object]) -> dict[str, object]:
    # The PostgreSQL actor is a type-only import in the cold composition module.
    assert inspect.isfunction(value)
    return dict(value.__globals__) | {"PostgresExecutionActor": PostgresExecutionActor}


def _interfaces() -> dict[str, object]:
    return {
        name: {
            "signature": str(inspect.signature(value, globals=_globals(value), eval_str=True)),
            "hints": {
                key: str(hint)
                for key, hint in get_type_hints(value, globalns=_globals(value)).items()
            },
            "doc": inspect.getdoc(value),
            "async": inspect.iscoroutinefunction(value),
        }
        for name, value in vars(runtime).items()
        if inspect.isfunction(value) and value.__module__.startswith("jeff.bootstrap.runtime")
    }


def test_composition_interfaces_add_postgresql_without_changing_legacy_parameters() -> None:
    interfaces = _interfaces()
    assert _digest(interfaces) == INTERFACE_DIGEST
    for name in ("create_internal_execution", "create_configured_execution"):
        factory = getattr(runtime, name)
        current = cast(dict[str, object], interfaces[name])
        signature = inspect.signature(factory, globals=_globals(factory), eval_str=True)
        storage = signature.parameters["storage"]
        assert storage.annotation == runtime.SQLiteStorage | PostgresExecutionActor
        hints = cast(dict[str, str], current["hints"])
        hints["storage"] = str(runtime.SQLiteStorage)
        omitted = set()
        if name == "create_internal_execution":
            for parameter, default in (("guard_policy", None), ("require_guard", False)):
                assert signature.parameters[parameter].default is default
                hints.pop(parameter)
                omitted.add(parameter)
        current["signature"] = str(
            signature.replace(
                parameters=[
                    p.replace(annotation=runtime.SQLiteStorage) if p.name == "storage" else p
                    for p in signature.parameters.values()
                    if p.name not in omitted
                ]
            )
        )
    assert _digest(interfaces) == LEGACY_INTERFACE_DIGEST


def test_client_factories_keep_location_selection_lazy_and_root_patchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected: list[object] = []
    layout = RuntimeLayout(data_dir=Path("/synthetic/d"), runtime_dir=Path("/synthetic/r"), uid=1)

    def select() -> RuntimeLayout:
        selected.append(layout)
        return layout

    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.args, self.kwargs = args, kwargs

    controller = object()
    monkeypatch.setattr(runtime, "_selected_layout", select)
    monkeypatch.setattr(runtime, "RuntimeProjectClient", Client)
    monkeypatch.setattr(runtime, "RuntimeController", lambda value: controller)
    for factory in (
        runtime.create_execution_client,
        runtime.create_cancellation_client,
        runtime.create_fresh_execution_client,
        runtime.create_activity_client,
    ):
        client = factory()
        assert isinstance(client, Client) and client.args == (select,) and not client.kwargs
    diagnostic = runtime.create_runtime_diagnostics()
    assert isinstance(diagnostic, RuntimeDiagnosticsAdapter) and diagnostic._layout is select
    assert not selected
    assert runtime.create_runtime_controller() is controller
    assert selected == [layout]


def test_project_client_keeps_readonly_status_composition_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[object, ...]] = []
    local, diagnostic = object(), object()

    def selected() -> RuntimeLayout:
        raise AssertionError("Constructing a client must not select a location")

    def projects(*args: object) -> object:
        seen.append(args)
        return local

    def status(projects: object, control: object) -> object:
        assert projects is local and control is runtime.create_runtime_controller
        return diagnostic

    def client(selector: object, *, diagnostics: object) -> object:
        assert selector is selected and diagnostics is diagnostic
        return diagnostic

    monkeypatch.setattr(runtime, "_selected_layout", selected)
    monkeypatch.setattr(runtime, "GitProjectDiscovery", lambda: "discovery")
    monkeypatch.setattr(runtime, "FileProjectStore", lambda: "files")
    monkeypatch.setattr(runtime, "ProjectService", projects)
    monkeypatch.setattr(runtime, "ProjectStatusService", status)
    monkeypatch.setattr(runtime, "RuntimeProjectClient", client)
    assert runtime.create_project_client() is diagnostic
    assert seen == [("discovery", "files")]


def test_explicit_layout_overrides_skip_unused_host_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[RuntimePathInputs] = []
    layout = RuntimeLayout(data_dir=Path("/synthetic/d"), runtime_dir=Path("/synthetic/r"), uid=1)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Unused home input must not be resolved")

    def resolve(inputs: RuntimePathInputs) -> RuntimeLayout:
        observed.append(inputs)
        return layout

    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(
        runtime.os, "environ", {"JEFF_DATA_DIR": "/synthetic/d", "JEFF_RUNTIME_DIR": "/synthetic/r"}
    )
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 1)
    monkeypatch.setattr(Path, "home", forbidden)
    monkeypatch.setattr(runtime, "resolve_layout", resolve)
    assert runtime._selected_layout() is layout
    assert observed == [
        RuntimePathInputs(
            platform="darwin",
            home=Path("/"),
            temp_dir=Path("/"),
            uid=1,
            data_dir="/synthetic/d",
            runtime_dir="/synthetic/r",
        )
    ]


def test_package_entrypoint_calls_canonical_main_without_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "main", lambda: 77)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("jeff.bootstrap.runtime", run_name="__main__")
    assert exit_info.value.code == 77


def test_package_size_limit(code_line_count: Callable[[str], int]) -> None:
    for path in Path(runtime.__file__).parent.glob("*.py"):
        assert code_line_count(path.read_text()) <= 400, path.name

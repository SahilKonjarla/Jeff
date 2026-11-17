"""Runtime value/wire contracts retain exact schemas, defaults, validators and public aliases."""

import inspect
import json
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast, get_args, get_origin, get_type_hints
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from jeff.application import runtime
from jeff.application.projects import ErrorData, Failure

SCHEMA_DIGEST = "7510be362ab2508be1f7798369f972f48a51c4855e6a8a302ac9313ad49af395"
INTERFACE_DIGEST = "57685a7755c889d4195084e4caf4597ba1e2d161e5c606ab92da710a369f2592"
VALUE_DIGEST = "96564898c80e68d8d2aa7a7ae5226366cc5f1a212997db8a9435ea212b310621"


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _canonical(value: str) -> str:
    for name, item in vars(runtime).items():
        if inspect.isclass(item) and item.__module__.startswith("jeff.application.runtime"):
            value = value.replace(
                f"{item.__module__}.{item.__qualname__}", f"{runtime.__name__}.{name}"
            )
    return value


def _schemas() -> dict[str, object]:
    return {
        name: item.model_json_schema()
        for name, item in vars(runtime).items()
        if inspect.isclass(item) and issubclass(item, runtime._RuntimeModel)
    }


def _interfaces() -> dict[str, object]:
    result: dict[str, object] = {}
    for name, item in vars(runtime).items():
        if not (
            (inspect.isclass(item) or inspect.isfunction(item))
            and item.__module__.startswith("jeff.application.runtime")
        ):
            continue
        members = vars(item).items() if inspect.isclass(item) else [("call", item)]
        for method_name, method in members:
            kind = "method"
            if isinstance(method, property):
                method, kind = method.fget, "property"
            if isinstance(method, (staticmethod, classmethod)):
                kind = type(method).__name__
                method = method.__func__
            if not inspect.isfunction(method):
                continue
            result[f"{name}.{method_name}"] = {
                "signature": _canonical(str(inspect.signature(method))),
                "hints": {
                    key: _canonical(str(hint)) for key, hint in get_type_hints(method).items()
                },
                "kind": kind,
                "doc": inspect.getdoc(method),
            }
    return result


def _codes(annotation: object) -> list[str]:
    if get_origin(annotation) is Literal:
        return list(get_args(annotation))
    return [value for part in get_args(annotation) for value in _codes(part)]


def _values() -> dict[str, object]:
    identity = runtime.RuntimeIdentityData(instance_id=UUID(int=1), pid=1)
    observation = runtime.ControlObservation(
        state=runtime.RuntimeState.RUNNING, identity=identity, control_ready=True
    )
    layout = runtime.RuntimeLayout(
        data_dir=Path("/synthetic/d"), runtime_dir=Path("/synthetic/r"), uid=1
    )
    values = {
        "identity": identity,
        "status": runtime.RuntimeStatusData(state=runtime.RuntimeState.RUNNING, identity=identity),
        "layout": layout,
        "paths": runtime.RuntimePathInspection(
            entries=tuple(
                runtime.PathObservation(role=role, state="missing")
                for role in runtime.RUNTIME_PATH_ROLES
            )
        ),
        "limits": runtime.DEFAULT_RUNTIME_LIMITS,
        "observation": observation,
        "success": runtime.ControlSuccess(data=observation),
        # Fixed synthetic input only: default serialization must remain masked.
        "auth_request": runtime.AuthRequest.model_validate({"revision": 12, "token": "0" * 64}),
        "auth_success": runtime.AuthAccepted(revision=12, authenticated=True, identity=identity),
        "auth_refused": runtime.AuthAccepted(
            revision=12, authenticated=False, error="runtime_authentication_failed"
        ),
        "hello": runtime.ControlRequest(revision=12, request_id=UUID(int=2), operation="hello"),
        "stop": runtime.ControlRequest(
            revision=12,
            request_id=UUID(int=3),
            operation="stop",
            expected_instance_id=identity.instance_id,
        ),
        "response": runtime.ControlResponse(
            revision=12, request_id=UUID(int=2), observation=observation
        ),
        "error": runtime.ControlResponse(revision=12, request_id=UUID(int=2), error="runtime_busy"),
        "startup": runtime.StartupResult(revision=12, instance_id=identity.instance_id),
    }
    return {
        "wire": {name: item.model_dump_json() for name, item in values.items()},
        "codes": _codes(runtime.ControlIssueCode),
        "failures": {
            code: runtime.control_failure(cast(runtime.ControlIssueCode, code)).model_dump_json()
            for code in _codes(runtime.ControlIssueCode)
        },
        "message_order": list(runtime._CONTROL_MESSAGES.items()),
        "path_messages": list(runtime._MESSAGES.items()),
        "constants": [
            runtime.PROTOCOL_REVISION,
            runtime.MAC_SOCKET_PATH_BYTES,
            runtime.MAC_PATH_BYTES,
            runtime.MAC_NAME_BYTES,
        ],
        "paths": [
            str(getattr(layout, role))
            for role in (
                "database",
                "settings",
                "credential",
                "socket",
                "lock",
                "endpoint_receipt",
                "recovery_permit",
            )
        ],
    }


def test_all_runtime_value_and_wire_schemas_are_identical() -> None:
    assert _digest(_schemas()) == SCHEMA_DIGEST


def test_original_interfaces_and_resolved_annotations_are_identical() -> None:
    assert _digest(_interfaces()) == INTERFACE_DIGEST


def test_wire_defaults_caps_and_failure_message_order_are_identical() -> None:
    assert _digest(_values()) == VALUE_DIGEST


@pytest.mark.parametrize("token", [SecretStr("0" * 64), "A" * 64, "short", 0, True])
def test_authentication_still_accepts_only_raw_lowercase_hex_input(token: object) -> None:
    with pytest.raises(ValidationError):
        runtime.AuthRequest.model_validate({"revision": 12, "token": token})
    auth = runtime.AuthRequest.model_validate({"revision": 12, "token": "0" * 64})
    assert "0" * 64 not in repr(auth) and "0" * 64 not in auth.model_dump_json()
    assert len(auth.token.get_secret_value()) == 64


def test_revision_field_bounds_are_captured_not_redefined_by_root_reassignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "PROTOCOL_REVISION", 13)
    assert (
        runtime.ControlRequest(revision=12, request_id=UUID(int=1), operation="hello").revision
        == 12
    )
    with pytest.raises(ValidationError):
        runtime.ControlRequest(revision=13, request_id=UUID(int=1), operation="hello")


def test_validator_keeps_runtime_root_role_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = tuple(
        runtime.PathObservation(role=role, state="missing") for role in runtime.RUNTIME_PATH_ROLES
    )
    monkeypatch.setattr(runtime, "RUNTIME_PATH_ROLES", ())
    with pytest.raises(ValidationError):
        runtime.RuntimePathInspection(entries=entries)


def test_control_fault_and_storage_fallback_keep_root_function_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = Failure(error=ErrorData(code="storage_busy", message="Synthetic bounded reason"))
    seen: list[str] = []

    def project(code: object) -> Failure:
        seen.append(str(code))
        return expected

    with monkeypatch.context() as patch:
        patch.setattr(runtime, "control_failure", project)
        assert str(runtime.ControlFault("runtime_busy")) == expected.error.message
    with monkeypatch.context() as patch:
        patch.setattr(runtime, "storage_failure", project)
        assert runtime.control_failure("storage_busy") is expected
    assert seen == ["runtime_busy", "storage_busy"]


def test_package_size_limit() -> None:
    for path in Path(runtime.__file__).parent.glob("*.py"):
        assert len(path.read_text().splitlines()) <= 400, path.name

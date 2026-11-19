"""Characterize dispatch wire contracts and interfaces across package extraction."""

import importlib
import inspect
import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

from pydantic import BaseModel

from jeff.application import dispatch
from jeff.application import runtime as runtime_contracts
from jeff.application.runtime import RuntimeIdentityData

# Wire 13 adds fresh read-only enumeration; service interfaces remain unchanged.
SCHEMA_DIGEST = "8490c51cf9731937d21900cbe10f231c7987e074a39d2dde1ce497c9451932e5"
WIRE_DIGEST = "7b01dce05c238a4db5d0511169cf6b3b7c358a4612954cf1ab8f5a2866d76f86"
SIGNATURE_DIGEST = "566af34c1f88990b1b1d48fb08b07dc388c89b588ebfa51e6faee3420ad12328"


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _schemas() -> dict[str, object]:
    return {
        name: value.model_json_schema()
        for name, value in vars(dispatch).items()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__.startswith("jeff.application.dispatch")
    }


def _wire() -> dict[str, str]:
    identity = RuntimeIdentityData(instance_id=UUID(int=1), pid=1)
    call = dispatch.ProjectCall(
        request_id=UUID(int=2), directory=Path("/synthetic/project"), operation="config"
    )
    receipt = dispatch.ReceiptQuery(
        query_id=UUID(int=3), directory=call.directory, request_id=call.request_id
    )
    history = dispatch.HistoryQuery(query_id=UUID(int=4), directory=call.directory)
    evidence = dispatch.EvidenceRequest(expected_instance_id=identity.instance_id, query=history)
    omitted_limit = evidence.model_dump(mode="json")
    del omitted_limit["query"]["limit"]
    subscription = dispatch.SubscriptionQuery(query_id=UUID(int=5), directory=call.directory)
    failure = dispatch.dispatch_failure("request_outcome_unknown")
    return {
        "project": dispatch.ProjectRequest(
            expected_instance_id=identity.instance_id, call=call
        ).model_dump_json(),
        "receipt": dispatch.ReceiptRequest(
            expected_instance_id=identity.instance_id, query=receipt
        ).model_dump_json(),
        "history": evidence.model_dump_json(),
        "history_default": dispatch.EvidenceRequest.model_validate(omitted_limit).model_dump_json(),
        "dispatch_reply": dispatch.DispatchReply(
            request_id=call.request_id, identity=identity, accepted=None, result=failure
        ).model_dump_json(),
        "evidence_reply": dispatch.EvidenceReply(
            request_id=history.query_id, identity=identity, operation="history", error=failure
        ).model_dump_json(),
        "receipt_reply": dispatch.ReceiptReply(
            request_id=receipt.query_id, identity=identity
        ).model_dump_json(),
        "subscription": dispatch.SubscriptionRequest(
            expected_instance_id=identity.instance_id, query=subscription
        ).model_dump_json(),
        "stream": dispatch.StreamFrame(
            kind="closed", request_id=subscription.query_id, identity=identity, error=failure
        ).model_dump_json(),
    }


def _signatures() -> dict[str, str]:
    result = {}
    for cls in (dispatch.ProjectDispatcher, dispatch.ProjectStatusService):
        for name, value in vars(cls).items():
            if inspect.isfunction(value) and (not name.startswith("_") or name == "__init__"):
                signature = str(inspect.signature(value))
                for alias, annotation in vars(dispatch).items():
                    if inspect.isclass(annotation) and annotation.__module__.startswith(
                        "jeff.application.dispatch"
                    ):
                        signature = signature.replace(
                            f"{annotation.__module__}.{annotation.__qualname__}",
                            f"jeff.application.dispatch.{alias}",
                        )
                for alias, annotation in vars(runtime_contracts).items():
                    if inspect.isclass(annotation) and annotation.__module__.startswith(
                        "jeff.application.runtime"
                    ):
                        signature = signature.replace(
                            f"{annotation.__module__}.{annotation.__qualname__}",
                            f"jeff.application.runtime.{alias}",
                        )
                result[f"{cls.__name__}.{name}"] = signature
                get_type_hints(value)
    return result


def test_original_model_schemas_remain_identical() -> None:
    assert _digest(_schemas()) == SCHEMA_DIGEST


def test_wire_serialization_and_history_union_defaults_remain_identical() -> None:
    assert _digest(_wire()) == WIRE_DIGEST


def test_public_service_signatures_remain_identical() -> None:
    assert _digest(_signatures()) == SIGNATURE_DIGEST


def test_facade_exports_alias_definitions_without_wrappers() -> None:
    for name in [*dispatch.__all__, "_DispatchModel", "_RequestObserver"]:
        value = getattr(dispatch, name)
        if inspect.isclass(value) or inspect.isfunction(value):
            assert getattr(importlib.import_module(value.__module__), name) is value


def test_package_files_stay_within_size_limit(code_line_count: Callable[[str], int]) -> None:
    for path in Path(dispatch.__file__).parent.glob("*.py"):
        assert code_line_count(path.read_text()) <= 400, path.name

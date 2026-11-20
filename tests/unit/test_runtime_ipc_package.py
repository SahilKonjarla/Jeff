"""Characterize native IPC exports, wire frames and public class interfaces."""

import inspect
import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

from jeff.application import runtime as runtime_contracts
from jeff.application.dispatch import (
    EvidenceRequest,
    ExecutionCancelRequest,
    ExecutionRequest,
    FreshExecutionRequest,
    HistoryQuery,
    ProjectCall,
    ProjectRequest,
    ReceiptQuery,
    ReceiptRequest,
    SubscriptionQuery,
    SubscriptionRequest,
)
from jeff.application.execution import ExecutionCall, ExecutionCancelCall, FreshExecutionCall
from jeff.application.runtime import ControlRequest, ControlResponse
from jeff.infrastructure import runtime_ipc as ipc
from jeff.infrastructure.runtime_state import Ownership

# Wire 14 enables guarded execution; only revision fields/frame bytes change here.
# Revision-normalized snapshots still match wire13; class interfaces are unchanged.
SCHEMA_DIGEST = "8ef1009a3e4a8c206376cde01f0c7a3b0e9ab0d47d8aa61f147bbba061699452"
WIRE_DIGEST = "4353815da931d51e7d4c02a3e3e00edf2e179906c57c7d1df30eeeaa22d9ec8d"
SIGNATURE_DIGEST = "ee18ba33d22549c92b1f74b9179c695f6a9b53d4694f7814a58d3d27ae03f2fe"


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _schema() -> dict[str, object]:
    return ipc.RpcRequest.model_json_schema()


def _wire() -> dict[str, object]:
    directory = Path("/synthetic/project")
    identity, request_id = UUID(int=1), UUID(int=2)
    messages = (
        ControlRequest(revision=14, request_id=request_id, operation="hello"),
        ProjectRequest(
            expected_instance_id=identity,
            call=ProjectCall(request_id=request_id, directory=directory, operation="config"),
        ),
        ReceiptRequest(
            expected_instance_id=identity,
            query=ReceiptQuery(query_id=UUID(int=3), directory=directory, request_id=request_id),
        ),
        EvidenceRequest(
            expected_instance_id=identity,
            query=HistoryQuery(query_id=UUID(int=3), directory=directory),
        ),
        ExecutionRequest(
            expected_instance_id=identity,
            call=ExecutionCall(
                request_id=request_id,
                directory=directory,
                operation="propose",
                argv=("python", "-V"),
            ),
        ),
        FreshExecutionRequest(
            expected_instance_id=identity,
            call=FreshExecutionCall(
                request_id=request_id,
                directory=directory,
                operation="propose",
                profile_name="python",
                argv=("python", "-V"),
                paths=("source.py",),
            ),
        ),
        ExecutionCancelRequest(
            expected_instance_id=identity,
            call=ExecutionCancelCall(
                request_id=request_id, approval_id=UUID(int=3), reviewed_manifest_digest="a" * 64
            ),
        ),
        SubscriptionRequest(
            expected_instance_id=identity,
            query=SubscriptionQuery(query_id=UUID(int=3), directory=directory),
        ),
    )
    result: dict[str, object] = {}
    for message in messages:
        frame = ipc.encode_message(message.model_dump(mode="json"), 65536)
        decoded = ipc.decode_message(frame[4:], ipc.RpcRequest)
        result[message.kind] = {"frame": frame.hex(), "decoded": decoded.model_dump_json()}
    return result


def _signatures() -> dict[str, str]:
    result = {}
    for name in (
        "ControlConnection",
        "ControlClient",
        "OwnedActivityStream",
        "RuntimeProjectClient",
        "ControlServer",
    ):
        cls = getattr(ipc, name)
        for method_name, value in vars(cls).items():
            if isinstance(value, property):
                value = value.fget
            if isinstance(value, staticmethod):
                value = value.__func__
            if not inspect.isfunction(value):
                continue
            signature = str(inspect.signature(value)).replace(
                f"{Ownership.__module__}.{Ownership.__qualname__}",
                "jeff.infrastructure.runtime_state.Ownership",
            )
            for alias in (
                "ControlConnection",
                "ControlClient",
                "OwnedActivityStream",
                "RuntimeProjectClient",
                "ControlServer",
                "RpcRequest",
            ):
                annotation = getattr(ipc, alias)
                signature = signature.replace(
                    f"{annotation.__module__}.{annotation.__qualname__}",
                    f"jeff.infrastructure.runtime_ipc.{alias}",
                )
            for alias, annotation in vars(runtime_contracts).items():
                if inspect.isclass(annotation) and annotation.__module__.startswith(
                    "jeff.application.runtime"
                ):
                    signature = signature.replace(
                        f"{annotation.__module__}.{annotation.__qualname__}",
                        f"jeff.application.runtime.{alias}",
                    )
            result[f"{name}.{method_name}"] = signature
            get_type_hints(value)
    return result


def test_rpc_discriminated_union_schema_stays_identical() -> None:
    assert _digest(_schema()) == SCHEMA_DIGEST


def test_all_eight_request_kinds_preserve_exact_framing_and_decoding() -> None:
    assert _digest(_wire()) == WIRE_DIGEST


def test_class_interfaces_and_resolvable_annotations_stay_identical() -> None:
    assert _digest(_signatures()) == SIGNATURE_DIGEST


def test_legacy_incidental_control_response_export_remains_available() -> None:
    assert ipc.ControlResponse is ControlResponse


def test_package_files_stay_within_size_limit(code_line_count: Callable[[str], int]) -> None:
    for path in Path(ipc.__file__).parent.glob("*.py"):
        assert code_line_count(path.read_text()) <= 400, path.name

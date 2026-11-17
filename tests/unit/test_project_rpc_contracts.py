"""Versioned branch calls are finite, scoped and privacy-safe values."""

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jeff.application.dispatch import ProjectCall, ProjectRequest
from jeff.application.runtime import PROTOCOL_REVISION, AuthAccepted, RuntimeIdentityData
from jeff.application.storage import DATABASE_SCHEMA, ReceiptTransition


def test_wire12_cancel_target_and_terminal_projection_are_strict_and_private() -> None:
    from jeff.application.dispatch import ExecutionCancelRequest
    from jeff.application.execution import ExecutionCancelCall, ExecutionStopView
    from jeff.infrastructure.runtime_ipc import RpcRequest, decode_message

    call = ExecutionCancelCall(
        request_id=uuid4(), approval_id=uuid4(), reviewed_manifest_digest="a" * 64
    )
    request = ExecutionCancelRequest(expected_instance_id=uuid4(), call=call)
    assert request.revision == 12 and request.kind == "execution.cancel.v1"
    assert decode_message(request.model_dump_json().encode(), RpcRequest).root == request
    for extra in (
        {"directory": "/SYNTHETIC_PRIVATE"},
        {"container_id": "a" * 64},
        {"reason": "shutdown"},
    ):
        with pytest.raises(ValidationError) as caught:
            ExecutionCancelCall.model_validate({**call.model_dump(), **extra})
        assert "SYNTHETIC_PRIVATE" not in str(caught.value)
    base = dict(
        request_id=call.request_id, reason="cancel", requested_at_ms=1, clock_confirmed=True
    )
    for terminal in (
        {"outcome": "cancelled", "evidence": "uncertain", "cleanup_verified": True},
        {"exit_code": 0},
        {"outcome": "completed", "evidence": "owned", "cleanup_verified": True, "exit_code": 0},
        {"outcome": "timed_out", "evidence": "not_started", "cleanup_verified": True},
    ):
        with pytest.raises(ValidationError):
            ExecutionStopView.model_validate({**base, **terminal})
    view = ExecutionStopView.model_validate(base)
    assert view.payload_state == "not_inspected"  # Intent ACK is not an archive read.
    with pytest.raises(ValidationError):
        ExecutionStopView.model_validate({**view.model_dump(), "ownership_digest": "a" * 64})


def test_versions_and_strict_instance_bound_project_call() -> None:
    assert PROTOCOL_REVISION == 12 and DATABASE_SCHEMA == 10
    call = ProjectCall(
        request_id=uuid4(),
        operation="ask",
        directory=Path("/fixture"),
        question="what branch am I on?",
    )
    message = ProjectRequest(expected_instance_id=uuid4(), call=call)
    assert message.kind == "project" and message.remaining_ms == 30000
    for budget in (True, "30", 0, 30001):
        with pytest.raises(ValidationError):
            ProjectRequest.model_validate({**message.model_dump(), "remaining_ms": budget})
    with pytest.raises(ValidationError) as caught:
        ProjectCall.model_validate({**call.model_dump(), "shell": "SYNTHETIC_PRIVATE"})
    assert "SYNTHETIC_PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("path", ["relative", "/tmp/../other", "/tmp/\0bad"])
def test_invalid_project_paths_have_no_transport_authority(path: str) -> None:
    with pytest.raises(ValidationError):
        ProjectCall(operation="ask", directory=Path(path), question="what branch am I on?")


def test_authentication_cannot_claim_identity_without_auth_or_omit_on_success() -> None:
    with pytest.raises(ValidationError):
        AuthAccepted(revision=12, authenticated=True)
    with pytest.raises(ValidationError):
        AuthAccepted(
            revision=12,
            authenticated=False,
            error="runtime_authentication_failed",
            identity=RuntimeIdentityData(instance_id=uuid4(), pid=1),
        )


def test_fresh_execution_envelope_is_distinct_strict_and_bound_to_wire12() -> None:
    from jeff.application.dispatch import FreshExecutionRequest
    from jeff.application.execution import (
        ExecutionAdmission,
        FreshExecutionCall,
        FreshExecutionReply,
    )
    from jeff.infrastructure.runtime_ipc import RpcRequest, decode_message

    call = FreshExecutionCall(
        request_id=uuid4(),
        directory=Path("/fixture"),
        operation="propose",
        profile_name="python",
        argv=("python", "-V"),
        paths=("source.py",),
    )
    request = FreshExecutionRequest(expected_instance_id=uuid4(), call=call)
    assert request.kind == "execution.v1" and request.revision == 12
    assert decode_message(request.model_dump_json().encode(), RpcRequest).root == request
    base = call.model_dump()
    for changes in (
        {"authority_kind": "review_only_v1"},
        {"current_manifest_digest": "0" * 64},
        {"launch_claim": str(uuid4())},
        {"shell": "SYNTHETIC_PRIVATE"},
        {"paths": ("file", "file/child")},
        {"paths": ("Case/a", "case/b")},
        {"operation": "start", "approval_id": uuid4(), "reviewed_manifest_digest": "0" * 64},
    ):
        with pytest.raises(ValidationError) as caught:
            FreshExecutionCall.model_validate({**base, **changes})
        assert "SYNTHETIC_PRIVATE" not in str(caught.value)
    for operation in ("approve", "deny", "start"):
        targeted = FreshExecutionCall(
            request_id=uuid4(),
            directory=Path("/fixture"),
            operation=operation,
            approval_id=uuid4(),
            reviewed_manifest_digest="0" * 64,
        )
        with pytest.raises(ValidationError):
            FreshExecutionCall.model_validate(
                {**targeted.model_dump(), "reviewed_manifest_digest": None}
            )
    with pytest.raises(ValidationError):
        FreshExecutionCall.model_validate({k: v for k, v in base.items() if k != "request_id"})
    admission = ExecutionAdmission(
        request_id=uuid4(),
        project_id=uuid4(),
        canonical_root=Path("/fixture"),
        approval_id=uuid4(),
        job_id=uuid4(),
        manifest_digest="0" * 64,
        claim_state="claimed",
        replayed=False,
    )
    reply = FreshExecutionReply(
        ok=True,
        request_id=admission.request_id,
        identity=uuid4(),
        operation="start",
        admission=admission,
    )
    assert FreshExecutionReply.model_validate_json(reply.model_dump_json()) == reply
    reply_changes: tuple[dict[str, object], ...] = (
        {"request_id": uuid4()},
        {"operation": "show"},
        {"completion_state": "claim_only"},
        {"identity": None},
        {"ok": 1},
        {"owned": {}},
        {"claim_id": uuid4()},
    )
    for reply_change in reply_changes:
        with pytest.raises(ValidationError):
            FreshExecutionReply.model_validate({**reply.model_dump(), **reply_change})


def test_receipt_transition_cannot_persist_arbitrary_messages_or_payloads() -> None:
    with pytest.raises(ValidationError) as caught:
        ReceiptTransition.model_validate(
            {
                "request_id": str(uuid4()),
                "owner_instance_id": str(uuid4()),
                "state": "succeeded",
                "outcome_code": "SYNTHETIC_PRIVATE",
            }
        )
    assert "SYNTHETIC_PRIVATE" not in str(caught.value)

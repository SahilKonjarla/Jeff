"""Versioned branch calls are finite, scoped and privacy-safe values."""

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jeff.application.dispatch import ProjectCall, ProjectRequest
from jeff.application.runtime import PROTOCOL_REVISION, AuthAccepted, RuntimeIdentityData
from jeff.application.storage import DATABASE_SCHEMA, ReceiptTransition


def test_versions_and_strict_instance_bound_project_call() -> None:
    assert PROTOCOL_REVISION == 4 and DATABASE_SCHEMA == 3
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
        AuthAccepted(revision=4, authenticated=True)
    with pytest.raises(ValidationError):
        AuthAccepted(
            revision=4,
            authenticated=False,
            error="runtime_authentication_failed",
            identity=RuntimeIdentityData(instance_id=uuid4(), pid=1),
        )


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

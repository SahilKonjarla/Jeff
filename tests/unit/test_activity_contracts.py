"""Finite evidence types, not a raw log/archive interface."""

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jeff.application.dispatch import EvidenceQuery
from jeff.application.history import ActivityRecord, GitObservation, ReplayScope, activity_code


def test_git_observation_is_frozen_bounded_and_rejects_raw_payloads() -> None:
    observation = GitObservation(
        category="status.branch", process_started=True, code="ok", elapsed_ms=4, output_bytes=120
    )
    assert observation.category == "status.branch"
    for update in (
        {"stdout": "SYNTHETIC_PRIVATE"},
        {"code": "SYNTHETIC_PRIVATE"},
        {"output_bytes": -1},
        {"elapsed_ms": True},
    ):
        with pytest.raises(ValidationError):
            GitObservation.model_validate({**observation.model_dump(), **update})
    with pytest.raises(ValidationError):
        observation.output_bytes = 0
    assert (
        ActivityRecord(request_id=uuid4(), owner_instance_id=uuid4(), git=observation).git
        == observation
    )
    with pytest.raises(ValidationError):
        ActivityRecord(request_id=uuid4(), owner_instance_id=uuid4())


def test_replay_is_scoped_and_has_strict_bounded_seek(tmp_path: Path) -> None:
    base = {"project_id": uuid4(), "canonical_root": tmp_path}
    assert ReplayScope.model_validate(base).limit == 64
    for update in (
        {"limit": 0},
        {"limit": 65},
        {"after": -1},
        {"after": True},
        {"canonical_root": "relative"},
    ):
        with pytest.raises(ValidationError):
            ReplayScope.model_validate({**base, **update})


def test_evidence_requests_are_finite_and_do_not_accept_raw_payloads() -> None:
    trace = EvidenceQuery(operation="trace", directory=Path("/fixture"), request_id=uuid4())
    assert (
        trace.operation == "trace" and activity_code("SYNTHETIC_PRIVATE_CODE") == "operation_failed"
    )
    for update in (
        {"operation": "shell"},
        {"stdout": "SYNTHETIC_PRIVATE"},
        {"limit": 1},
        {"request_id": None},
        {"after": True},
    ):
        with pytest.raises(ValidationError) as invalid:
            EvidenceQuery.model_validate({**trace.model_dump(), **update})
        assert "SYNTHETIC_PRIVATE" not in str(invalid.value)
    with pytest.raises(ValidationError):
        EvidenceQuery(operation="replay", directory=Path("/fixture"), request_id=uuid4())

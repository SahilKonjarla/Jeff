"""Runtime value/result contracts; no lifecycle or ownership proof is implied."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from jeff.application.runtime import (
    DEFAULT_RUNTIME_LIMITS,
    PROTOCOL_REVISION,
    ControlObservation,
    PathObservation,
    RuntimeIdentityData,
    RuntimeLimits,
    RuntimePathInspection,
    RuntimeStatusData,
)
from jeff.domain.runtime import RuntimeFault, RuntimeIdentity, RuntimeSnapshot, RuntimeState

INSTANCE = UUID("00000000-0000-4000-8000-000000000001")


def test_runtime_identity_and_snapshot_are_immutable_values() -> None:
    identity = RuntimeIdentity(INSTANCE, 42)
    snapshot = RuntimeSnapshot(RuntimeState.RUNNING, identity)

    assert snapshot.state is RuntimeState.RUNNING
    assert snapshot.identity == identity
    with pytest.raises(AttributeError):
        identity.pid = 43  # type: ignore[misc]
    with pytest.raises(AttributeError):
        snapshot.state = RuntimeState.STOPPED  # type: ignore[misc]


@pytest.mark.parametrize("pid", [0, -1, True, "42", 1.5])
def test_identity_rejects_nonpositive_or_noninteger_pid_without_echo(pid: object) -> None:
    with pytest.raises(RuntimeFault) as caught:
        RuntimeIdentity(INSTANCE, pid)  # type: ignore[arg-type]
    assert caught.value.code == "invalid_runtime_identity"


@pytest.mark.parametrize("state", [RuntimeState.RUNNING, RuntimeState.STOPPING])
def test_live_state_requires_identity(state: RuntimeState) -> None:
    with pytest.raises(RuntimeFault, match="inconsistent"):
        RuntimeSnapshot(state)


def test_stopped_rejects_claimed_live_identity() -> None:
    with pytest.raises(RuntimeFault, match="inconsistent"):
        RuntimeSnapshot(RuntimeState.STOPPED, RuntimeIdentity(INSTANCE, 42))


def test_unknown_and_starting_do_not_invent_identity_or_stopped_state() -> None:
    assert RuntimeSnapshot(RuntimeState.UNKNOWN).identity is None
    assert RuntimeSnapshot(RuntimeState.STARTING).state is RuntimeState.STARTING


def test_status_serialization_validates_structure_without_claiming_authentication() -> None:
    data = RuntimeStatusData(
        state=RuntimeState.RUNNING,
        identity=RuntimeIdentityData(instance_id=INSTANCE, pid=42),
    )
    assert data.model_dump(mode="json") == {
        "state": "running",
        "identity": {"instance_id": str(INSTANCE), "pid": 42},
        "issue": None,
    }
    assert RuntimeStatusData.model_validate_json(data.model_dump_json()) == data
    with pytest.raises(ValidationError):
        data.state = RuntimeState.STOPPED


@pytest.mark.parametrize("pid", [True, "42", 0, -1])
def test_public_identity_does_not_coerce_invalid_pids(pid: object) -> None:
    with pytest.raises(ValidationError):
        RuntimeIdentityData.model_validate({"instance_id": str(INSTANCE), "pid": pid})


def test_public_status_rejects_invalid_state_and_secret_fields_safely() -> None:
    with pytest.raises(ValidationError) as caught:
        RuntimeStatusData.model_validate({"state": "stopped", "credential": "SYNTHETIC_PRIVATE"})
    assert "SYNTHETIC_PRIVATE" not in str(caught.value)
    with pytest.raises(ValidationError):
        RuntimeStatusData(state=RuntimeState.RUNNING)
    with pytest.raises(ValidationError):
        RuntimeStatusData(state=RuntimeState.UNKNOWN)
    assert (
        RuntimeStatusData(state=RuntimeState.UNKNOWN, issue="runtime_inspection_failed").state
        is RuntimeState.UNKNOWN
    )


def test_inspection_is_fixed_bounded_metadata_not_a_running_status() -> None:
    roles = ("data_dir", "runtime_dir", "database", "settings", "credential", "socket", "lock")
    report = RuntimePathInspection.model_validate(
        {
            "entries": [{"role": role, "state": "missing"} for role in roles],
        }
    )
    assert len(report.entries) == 7
    assert "running" not in report.model_dump_json()
    with pytest.raises(ValidationError):
        RuntimePathInspection(entries=(report.entries[0],) * 7)
    with pytest.raises(ValidationError):
        PathObservation(role="credential", state="present", issue="unsafe_runtime_path")
    with pytest.raises(ValidationError):
        PathObservation(role="credential", state="unsafe")


def test_control_readiness_never_claims_storage_or_application_readiness() -> None:
    observation = ControlObservation(
        state=RuntimeState.RUNNING,
        identity=RuntimeIdentityData(instance_id=INSTANCE, pid=42),
        control_ready=True,
    )
    assert observation.application_ready is False
    assert observation.storage.state == "not_checked"
    for value in (True, 0, "false"):
        with pytest.raises(ValidationError):
            ControlObservation.model_validate(
                {
                    **observation.model_dump(),
                    "application_ready": value,
                }
            )
    with pytest.raises(ValidationError):
        ControlObservation(state=RuntimeState.RUNNING, control_ready=True)
    with pytest.raises(ValidationError):
        ControlObservation(state=RuntimeState.STOPPED, control_ready=True)


def test_limits_match_reviewed_budget_and_reject_invalid_configurations() -> None:
    assert PROTOCOL_REVISION == 2
    assert DEFAULT_RUNTIME_LIMITS.request_bytes == 65536
    assert DEFAULT_RUNTIME_LIMITS.response_bytes == 16777216
    assert DEFAULT_RUNTIME_LIMITS.active_requests == 4
    with pytest.raises(ValidationError):
        RuntimeLimits(active_requests=0)
    with pytest.raises(ValidationError):
        RuntimeLimits(connections=True)
    with pytest.raises(ValidationError):
        RuntimeLimits(subscriptions=17)
    with pytest.raises(ValidationError):
        RuntimeLimits(history_page_size=101)

"""Guard drafts bind artifact/policy without becoming executable approvals."""

from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from jeff.application.execution import (
    ActionManifest,
    AuthorityOperation,
    ExecutionManifest,
    ExecutionProfile,
    GuardArtifact,
    GuardAuthorize,
    GuardedExecutionDraft,
    GuardLifetime,
    GuardProtocolFault,
    PreparedFile,
    PreparedInputs,
    candidate_guard_authorization,
    encode_guard_frame,
)


@pytest.fixture
def original() -> ExecutionManifest:
    return ExecutionManifest(
        profile=ExecutionProfile(
            name="python",
            project_id=UUID(int=1),
            canonical_root=Path("/synthetic/project"),
            engine_id="synthetic-engine",
            endpoint="unix:///synthetic/docker.sock",
            image_id="sha256:" + "1" * 64,
            platform="linux/arm64",
            toolchain="Python 3.12",
            executable="/usr/local/bin/python",
        ),
        argv=("/usr/local/bin/python", "-V"),
        inputs=PreparedInputs(
            snapshot_id=UUID(int=2),
            files=(PreparedFile(path="source.py", size=1, sha256="2" * 64),),
        ),
    )


def test_guard_draft_binds_artifact_without_changing_original_manifest(
    original: ExecutionManifest,
) -> None:
    before = original.model_dump_json(), original.fingerprint
    artifact = GuardArtifact(
        sha256="3" * 64, size=4096, platform="linux/arm64", workload_filter_sha256="4" * 64
    )
    draft = GuardedExecutionDraft(
        original=original,
        artifact=artifact,
        startup_wait_ns=100,
        clock_rate_floor_ppm=900000,
        deadline_margin_ns=20,
    )
    assert not draft.execution_available and not draft.launch_authority_issued
    assert draft.artifact.filename == "jeff-job-guard-linux-arm64"
    assert (original.model_dump_json(), original.fingerprint) == before
    assert draft.fingerprint != original.fingerprint
    assert GuardedExecutionDraft.model_validate_json(draft.model_dump_json()) == draft
    for model in (ExecutionManifest, ActionManifest):
        with pytest.raises(ValidationError):
            model.model_validate(draft.model_dump())


@pytest.fixture
def draft(original: ExecutionManifest) -> GuardedExecutionDraft:
    return GuardedExecutionDraft(
        original=original,
        artifact=GuardArtifact(
            sha256="3" * 64, size=4096, platform="linux/arm64", workload_filter_sha256="4" * 64
        ),
        startup_wait_ns=100,
        clock_rate_floor_ppm=900000,
        deadline_margin_ns=20,
    )


def test_original_budget_arms_once_without_renewal(draft: GuardedExecutionDraft) -> None:
    lifetime = GuardLifetime(draft, now_ns=1000)
    authorization = candidate_guard_authorization(
        draft, lifetime.challenge, host_now_ns=500, host_deadline_ns=1500
    )
    # Independent worked example: 1000ns remaining, 20ns margin, 90% rate floor.
    assert authorization.deadline_ns == 1882
    assert lifetime.accept(encode_guard_frame(authorization), now_ns=1050)
    assert lifetime.poll(1881) == "armed"  # Not a claim that a workload started.
    assert lifetime.poll(1882) == "exit_required"
    assert not lifetime.accept(encode_guard_frame(authorization), now_ns=1883)


@pytest.mark.parametrize("kind", ["artifact", "filter", "margin", "rate", "startup"])
def test_each_guard_declaration_change_changes_digest(
    draft: GuardedExecutionDraft,
    kind: str,
) -> None:
    data = draft.model_dump(mode="json")
    if kind in ("artifact", "filter"):
        data["artifact"]["sha256" if kind == "artifact" else "workload_filter_sha256"] = "5" * 64
    else:
        key = {
            "margin": "deadline_margin_ns",
            "rate": "clock_rate_floor_ppm",
            "startup": "startup_wait_ns",
        }[kind]
        data[key] += 1
    changed = GuardedExecutionDraft.model_validate(data)
    assert changed.fingerprint != draft.fingerprint
    assert changed.original.fingerprint == draft.original.fingerprint


@pytest.mark.parametrize(
    "fields",
    [
        {"clock_rate_floor_ppm": 0},
        {"clock_rate_floor_ppm": 1000001},
        {"clock_rate_floor_ppm": True},
        {"deadline_margin_ns": 0},
        {"startup_wait_ns": 0},
        {"execution_available": True},
        {"launch_authority_issued": True},
        {"clock": "CLOCK_REALTIME"},
        {"deadline_margin_ns": 300000000000},
        {"startup_wait_ns": 300000000001},
    ],
)
def test_invalid_draft_is_not_repaired(
    draft: GuardedExecutionDraft, fields: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        GuardedExecutionDraft.model_validate({**draft.model_dump(), **fields})


def test_draft_cannot_enter_the_existing_authority_store(draft: GuardedExecutionDraft) -> None:
    profile = draft.original.profile
    common = dict(
        operation="propose",
        project_id=profile.project_id,
        canonical_root=profile.canonical_root,
        owner_instance_id=UUID(int=3),
        current_manifest_digest=draft.original.fingerprint,
    )
    control = AuthorityOperation.model_validate(dict(common, manifest=draft.original.model_dump()))
    assert control.manifest == draft.original
    with pytest.raises(ValidationError):
        AuthorityOperation.model_validate(dict(common, manifest=draft.model_dump()))


@pytest.mark.parametrize(
    "condition",
    ["late", "wrong_nonce", "wrong_digest", "too_long", "too_long_for_rate", "malformed"],
)
def test_invalid_first_message_permanently_requires_exit(
    draft: GuardedExecutionDraft,
    condition: str,
) -> None:
    lifetime = GuardLifetime(draft, now_ns=1000)
    grant = candidate_guard_authorization(
        draft, lifetime.challenge, host_now_ns=500, host_deadline_ns=1500
    )
    data = grant.model_dump()
    if condition == "wrong_nonce":
        data["nonce"] = UUID(int=99)
    elif condition == "wrong_digest":
        data["manifest_digest"] = "5" * 64
    elif condition == "too_long":
        data["deadline_ns"] = lifetime.challenge.issued_ns + lifetime.challenge.max_runtime_ns + 1
    elif condition == "too_long_for_rate":
        data["deadline_ns"] = lifetime.challenge.issued_ns + lifetime.challenge.max_runtime_ns
    wire = encode_guard_frame(GuardAuthorize.model_validate(data))
    if condition == "malformed":
        wire = wire[:-1]
    assert not lifetime.accept(wire, now_ns=1100 if condition == "late" else 1050)
    assert lifetime.poll(1051) == "exit_required"
    assert not lifetime.accept(encode_guard_frame(grant), now_ns=1052)


def test_duplicate_authorization_and_owner_loss_never_rearm(draft: GuardedExecutionDraft) -> None:
    first, replacement = GuardLifetime(draft, now_ns=1000), GuardLifetime(draft, now_ns=1000)
    assert first.challenge.nonce != replacement.challenge.nonce
    grant = candidate_guard_authorization(
        draft, first.challenge, host_now_ns=500, host_deadline_ns=1500
    )
    wire = encode_guard_frame(grant)
    assert first.accept(wire, now_ns=1050)
    assert not first.accept(wire, now_ns=1051)
    assert first.poll(1052) == "exit_required"
    assert not replacement.accept(wire, now_ns=1050)
    third = GuardLifetime(draft, now_ns=1000)
    third.close()  # Original owner's private channel lost, not CLI observer loss.
    assert third.poll(1001) == "exit_required"


@pytest.mark.parametrize("now", [999, 1100, 1000000000000])
def test_clock_rollback_or_startup_expiry_requires_exit(
    draft: GuardedExecutionDraft, now: int
) -> None:
    lifetime = GuardLifetime(draft, now_ns=1000)
    assert lifetime.poll(now) == "exit_required"


@pytest.mark.parametrize("deadline", [500, 519, 520])
def test_insufficient_original_budget_refuses(draft: GuardedExecutionDraft, deadline: int) -> None:
    lifetime = GuardLifetime(draft, now_ns=1000)
    with pytest.raises(GuardProtocolFault):
        candidate_guard_authorization(
            draft, lifetime.challenge, host_now_ns=500, host_deadline_ns=deadline
        )


def test_deadline_proposal_caps_at_original_policy(draft: GuardedExecutionDraft) -> None:
    lifetime = GuardLifetime(draft, now_ns=1000)
    grant = candidate_guard_authorization(
        draft, lifetime.challenge, host_now_ns=0, host_deadline_ns=900000000000
    )
    assert grant.deadline_ns == 269999999982 + 1000


@pytest.mark.parametrize("field", ["issued_ns", "startup_deadline_ns", "max_runtime_ns"])
def test_changed_challenge_policy_cannot_be_authorized(
    draft: GuardedExecutionDraft,
    field: str,
) -> None:
    lifetime = GuardLifetime(draft, now_ns=1000)
    data = lifetime.challenge.model_dump()
    data[field] += 1
    changed = type(lifetime.challenge).model_validate(data)
    with pytest.raises(GuardProtocolFault):
        candidate_guard_authorization(draft, changed, host_now_ns=500, host_deadline_ns=1500)


def test_different_artifact_cannot_use_the_old_challenge(draft: GuardedExecutionDraft) -> None:
    lifetime = GuardLifetime(draft, now_ns=1000)
    data = draft.model_dump(mode="json")
    data["artifact"]["size"] += 1
    changed = GuardedExecutionDraft.model_validate(data)
    with pytest.raises(GuardProtocolFault):
        candidate_guard_authorization(
            changed, lifetime.challenge, host_now_ns=500, host_deadline_ns=1500
        )


def test_platform_mismatch_and_mutable_inputs_refuse(draft: GuardedExecutionDraft) -> None:
    data = draft.model_dump(mode="json")
    data["artifact"]["platform"] = "linux/amd64"
    with pytest.raises(ValidationError):
        GuardedExecutionDraft.model_validate(data)
    with pytest.raises(ValidationError):
        draft.deadline_margin_ns = 1


def test_contract_construction_performs_no_filesystem_io(
    draft: GuardedExecutionDraft,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Contract construction touched filesystem")

    payload = draft.model_dump()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", forbidden)
        patch.setattr(Path, "read_bytes", forbidden)
        restored = GuardedExecutionDraft.model_validate(payload)
        assert restored == draft
        assert GuardLifetime(restored, now_ns=1000).poll(1001) == "parked"


@pytest.mark.parametrize("bad_clock", [True, -1, 9223372036854775808])
def test_invalid_clock_input_never_arms(draft: GuardedExecutionDraft, bad_clock: int) -> None:
    with pytest.raises(GuardProtocolFault):
        GuardLifetime(draft, now_ns=bad_clock)
    lifetime = GuardLifetime(draft, now_ns=1000)
    assert lifetime.poll(bad_clock) == "exit_required"
    with pytest.raises(GuardProtocolFault):
        candidate_guard_authorization(
            draft, lifetime.challenge, host_now_ns=bad_clock, host_deadline_ns=1500
        )

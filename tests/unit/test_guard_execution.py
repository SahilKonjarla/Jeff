"""Guarded identities remain distinct from every currently admitted format."""

from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from jeff.application.execution import (
    AuthorityOperation,
    AuthorityRecord,
    ExecutionManifest,
    ExecutionProfile,
    GuardArtifact,
    GuardedExecutionDraft,
    GuardedExecutionManifest,
    GuardExecutionArtifact,
    PreparedFile,
    PreparedInputs,
)


@pytest.fixture
def guarded() -> GuardedExecutionManifest:
    original = ExecutionManifest(
        profile=ExecutionProfile(
            name="python",
            project_id=UUID(int=1),
            canonical_root=Path("/synthetic/project"),
            engine_id="synthetic-engine",
            endpoint="unix:///synthetic/docker.sock",
            image_id="sha256:" + "1" * 64,
            platform="linux/arm64",
            toolchain="Python3.12",
            executable="/usr/local/bin/python",
            timeout_seconds=5,
        ),
        argv=("/usr/local/bin/python", "-V"),
        inputs=PreparedInputs(
            snapshot_id=UUID(int=2),
            files=(PreparedFile(path="source.py", size=1, sha256="2" * 64),),
        ),
    )
    return GuardedExecutionManifest(
        original=original,
        artifact=GuardExecutionArtifact(
            sha256="3" * 64,
            size=4096,
            platform="linux/arm64",
            workload_filter_sha256="4" * 64,
        ),
        startup_wait_ns=100,
        clock_rate_floor_ppm=900000,
        deadline_margin_ns=20,
    )


def test_guarded_identity_never_reinterprets_legacy_authority(
    guarded: GuardedExecutionManifest,
) -> None:
    original = guarded.original
    before = original.model_dump_json(), original.fingerprint
    assert guarded.authority_kind == "guarded_execution_v1"
    assert guarded.artifact.protocol == "jfg2"
    assert guarded.artifact.filename == "jeff-job-guard-linux-arm64"
    assert guarded.fingerprint != original.fingerprint
    assert GuardedExecutionManifest.model_validate_json(guarded.model_dump_json()) == guarded
    assert (original.model_dump_json(), original.fingerprint) == before
    profile = original.profile
    common = dict(
        operation="propose",
        project_id=profile.project_id,
        canonical_root=profile.canonical_root,
        owner_instance_id=UUID(int=3),
        current_manifest_digest=guarded.fingerprint,
    )
    assert (
        AuthorityOperation.model_validate(common | {"manifest": guarded.model_dump()}).manifest
        == guarded
    )
    with pytest.raises(ValidationError):
        ExecutionManifest.model_validate(guarded.model_dump())


@pytest.mark.parametrize(
    "field", ["artifact", "filter", "size", "startup", "margin", "rate", "argv", "inputs"]
)
def test_every_launch_affecting_identity_change_invalidates_outer_digest(
    guarded: GuardedExecutionManifest,
    field: str,
) -> None:
    data = guarded.model_dump(mode="json")
    if field in ("artifact", "filter", "size"):
        key = {"artifact": "sha256", "filter": "workload_filter_sha256", "size": "size"}[field]
        data["artifact"][key] = 4097 if field == "size" else "5" * 64
    elif field == "argv":
        data["original"]["argv"] = ["/usr/local/bin/python", "-h"]
    elif field == "inputs":
        data["original"]["inputs"]["files"][0]["sha256"] = "5" * 64
    else:
        data[
            {
                "startup": "startup_wait_ns",
                "margin": "deadline_margin_ns",
                "rate": "clock_rate_floor_ppm",
            }[field]
        ] += 1
    changed = GuardedExecutionManifest.model_validate(data)
    assert changed.fingerprint != guarded.fingerprint
    if field not in ("argv", "inputs"):
        assert changed.original.fingerprint == guarded.original.fingerprint


@pytest.mark.parametrize(
    "fields",
    [
        {"clock_rate_floor_ppm": True},
        {"deadline_margin_ns": 0},
        {"startup_wait_ns": 0},
        {"startup_wait_ns": 5000000001},
        {"deadline_margin_ns": 5000000000},
        {"clock": "CLOCK_REALTIME"},
        {"channel_layout": "merged_stdout_stderr"},
        {"terminal_delivery": "guaranteed"},
        {"launch_authority_issued": True},
    ],
)
def test_unsupported_policy_is_never_repaired(
    guarded: GuardedExecutionManifest,
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        GuardedExecutionManifest.model_validate(guarded.model_dump() | fields)


def test_wrong_artifact_protocol_platform_and_old_draft_are_rejected(
    guarded: GuardedExecutionManifest,
) -> None:
    for fields in ({"protocol": "jfg1"}, {"platform": "linux/amd64"}):
        data = guarded.model_dump()
        data["artifact"] |= fields
        with pytest.raises(ValidationError):
            GuardedExecutionManifest.model_validate(data)
    draft = GuardedExecutionDraft(
        original=guarded.original,
        artifact=GuardArtifact(
            sha256="3" * 64,
            size=4096,
            platform="linux/arm64",
            workload_filter_sha256="4" * 64,
        ),
        startup_wait_ns=100,
        clock_rate_floor_ppm=900000,
        deadline_margin_ns=20,
    )
    with pytest.raises(ValidationError):
        GuardedExecutionManifest.model_validate(draft.model_dump())
    original = guarded.original
    record = dict(
        proposal_id=UUID(int=4),
        approval_id=UUID(int=5),
        job_id=UUID(int=6),
        project_id=original.profile.project_id,
        canonical_root=original.profile.canonical_root,
        owner_instance_id=UUID(int=3),
        manifest=original,
        manifest_digest=original.fingerprint,
        state="pending",
        created_at_ms=100,
        expires_at_ms=600100,
    )
    assert AuthorityRecord.model_validate(record).manifest == original
    assert (
        AuthorityRecord.model_validate(
            record | {"manifest": guarded, "manifest_digest": guarded.fingerprint}
        ).manifest
        == guarded
    )
    for unsupported in (draft,):
        for payload in (unsupported, unsupported.model_dump()):
            with pytest.raises(ValidationError):
                AuthorityRecord.model_validate(
                    record | {"manifest": payload, "manifest_digest": unsupported.fingerprint}
                )
            with pytest.raises(ValidationError):
                AuthorityOperation.model_validate(
                    dict(
                        operation="propose",
                        project_id=original.profile.project_id,
                        canonical_root=original.profile.canonical_root,
                        owner_instance_id=UUID(int=3),
                        manifest=payload,
                        current_manifest_digest=unsupported.fingerprint,
                    )
                )

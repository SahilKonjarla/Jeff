"""Guarded execution uses distinct reviewed authority, never upgraded old approvals."""

import json
import time
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from test_guard_execution import guarded as guarded

from jeff.application.execution import (
    AuthorityOperation,
    DockerBackendFault,
    DockerCLIIdentity,
    DockerTargetDeclaration,
    GuardedDockerBackendConfiguration,
    GuardedExecutionManifest,
    GuardExecutionPolicy,
)
from jeff.infrastructure.runtime_execution.guard_artifacts import resource_path


def test_guarded_action_is_admitted_with_unchanged_original_identity(
    guarded: GuardedExecutionManifest,
) -> None:
    before = guarded.original.model_dump_json(), guarded.original.fingerprint
    operation = AuthorityOperation(
        operation="propose",
        project_id=guarded.original.profile.project_id,
        canonical_root=Path("/synthetic/project"),
        owner_instance_id=UUID(int=3),
        manifest=guarded,
        current_manifest_digest=guarded.fingerprint,
    )
    assert operation.manifest == guarded
    assert guarded.profile == guarded.original.profile
    assert guarded.argv == guarded.original.argv
    assert guarded.inputs == guarded.original.inputs
    assert (guarded.original.model_dump_json(), guarded.original.fingerprint) == before
    assert "profile" not in guarded.model_dump()


def test_settings4_requires_explicit_guard_and_preserves_settings3(
    guarded: GuardedExecutionManifest,
) -> None:
    from jeff.application.storage import GuardedRuntimeSettings, StorageFault
    from jeff.infrastructure.runtime_settings import parse_settings

    profile = guarded.profile
    policy = GuardExecutionPolicy(
        artifacts=(guarded.artifact,),
        startup_wait_ns=guarded.startup_wait_ns,
        clock_rate_floor_ppm=guarded.clock_rate_floor_ppm,
        deadline_margin_ns=guarded.deadline_margin_ns,
    )
    backend = GuardedDockerBackendConfiguration(
        binary=DockerCLIIdentity(path=Path("/synthetic/bin/docker"), sha256="1" * 64, size=10),
        config=Path("/synthetic/config"),
        declarations=(DockerTargetDeclaration(profile=profile),),
        guard=policy,
    )
    data = dict(
        schema_version=4,
        history=dict(retention_days=30),
        execution=dict(profiles=[profile.model_dump(mode="json")]),
        backend=backend.model_dump(mode="json"),
    )
    parsed = parse_settings(json.dumps(data).encode())
    assert isinstance(parsed, GuardedRuntimeSettings) and parsed.backend == backend
    for location in ("configured", "declaration", "both"):
        unclassified = json.loads(json.dumps(data))
        if location in ("configured", "both"):
            unclassified["execution"]["profiles"][0].pop("environment")
        if location in ("declaration", "both"):
            unclassified["backend"]["declarations"][0]["profile"].pop("environment")
        with pytest.raises(StorageFault) as rejected:
            parse_settings(json.dumps(unclassified).encode())
        assert rejected.value.code == "settings_invalid"
    disabled = json.loads(json.dumps(data))
    disabled["execution"]["profiles"][0].pop("environment")
    disabled["backend"] = None
    disabled_settings = parse_settings(json.dumps(disabled).encode())
    assert (
        isinstance(disabled_settings, GuardedRuntimeSettings) and disabled_settings.backend is None
    )
    legacy = json.loads(json.dumps(data))
    legacy["schema_version"] = 3
    legacy["backend"].pop("guard")
    legacy["execution"]["profiles"][0].pop("environment")
    legacy["backend"]["declarations"][0]["profile"].pop("environment")
    assert parse_settings(json.dumps(legacy).encode()).schema_version == 3
    for classification in ("production", "unclassified", ""):
        forbidden = json.loads(json.dumps(data))
        forbidden["execution"]["profiles"][0]["environment"] = classification
        forbidden["backend"]["declarations"][0]["profile"]["environment"] = classification
        with pytest.raises(StorageFault):
            parse_settings(json.dumps(forbidden).encode())
    for bad in (
        data | {"schema_version": 3},
        data
        | {"backend": {k: v for k, v in backend.model_dump(mode="json").items() if k != "guard"}},
    ):
        with pytest.raises(StorageFault):
            parse_settings(json.dumps(bad).encode())
    from jeff.application.execution import ExecutionSettings

    bad_profile = profile.model_copy(update={"environment": "production"})
    bad_backend = backend.model_copy(
        update={"declarations": (DockerTargetDeclaration.model_construct(profile=bad_profile),)}
    )
    with pytest.raises(ValidationError):
        GuardedRuntimeSettings(
            schema_version=4,
            history=parsed.history,
            execution=ExecutionSettings(profiles=(bad_profile,)),
            backend=bad_backend,
        )
    with pytest.raises(ValidationError):
        GuardExecutionPolicy.model_validate(dict(artifacts=[guarded.artifact.model_dump()]))


@pytest.mark.parametrize(
    "platform,digest,size,filter_digest",
    [
        (
            "linux/arm64",
            "7d86843d91d42ffa3bb53d1823d33fbde9397e5d473671b083560e6ba559406c",
            9888,
            "cf66160cb6e8b0c792af7ebe6bbb9d41ea5732af1f6ef01af203d6e576373849",
        ),
        (
            "linux/amd64",
            "7fde0150f7bb0fad5e76e70cc77b22416ba26d64d892e3a22ee6b9347c5aecf1",
            10048,
            "8065cc245d98b80cee958a3add26b51335a7ec7d1804b052a8bc254e2977d973",
        ),
    ],
)
def test_only_pinned_resources_are_listed_as_package_data(
    platform: str,
    digest: str,
    size: int,
    filter_digest: str,
) -> None:
    import tomllib

    from jeff.application.execution import GuardExecutionArtifact
    from jeff.infrastructure.runtime_execution.guard_artifacts import artifact_path

    artifact = GuardExecutionArtifact.model_validate(
        dict(platform=platform, sha256=digest, size=size, workload_filter_sha256=filter_digest)
    )
    path = artifact_path(artifact, deadline=time.monotonic() + 1)
    assert path == resource_path(artifact) and sha256(path.read_bytes()).hexdigest() == digest
    project = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    assert (
        artifact.filename
        in project["tool"]["setuptools"]["package-data"]["jeff.infrastructure.runtime_execution"]
    )


def test_artifact_verification_refuses_unshipped_identity_before_any_launch(
    guarded: GuardedExecutionManifest,
) -> None:
    from jeff.infrastructure.runtime_execution.guard_artifacts import artifact_path

    with pytest.raises(DockerBackendFault):
        artifact_path(guarded.artifact, deadline=time.monotonic() + 1)

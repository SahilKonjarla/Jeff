"""Public review contracts: exact declared actions, never launch authority."""

from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from jeff.application.dispatch import ExecutionRequest
from jeff.application.execution import ExecutionCall, ExecutionProfile, build_manifest
from jeff.application.runtime import PROTOCOL_REVISION
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_settings import parse_settings


def test_manifest_resolves_only_the_declared_executable_and_is_review_only() -> None:
    project = Project(UUID("00000000-0000-4000-8000-000000000001"), Path("/work/demo"))
    profile = ExecutionProfile(
        name="python",
        project_id=project.project_id,
        canonical_root=project.root,
        engine_id="declared-local-engine",
        endpoint="unix:///private/tmp/declared-docker.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="Python 3.12",
        executable="/usr/local/bin/python",
    )
    manifest = build_manifest(project, profile, ("python", "-c", "print(42)"))
    assert manifest.argv == ("/usr/local/bin/python", "-c", "print(42)")
    assert manifest.authority_kind == "review_only_v1"
    assert manifest.target_verification == "declared_only"
    assert manifest.execution_available is False
    assert manifest.canonical_root == project.root
    assert len(manifest.fingerprint) == 64
    assert manifest.fingerprint == build_manifest(project, profile, manifest.argv).fingerprint


def test_old_private_settings_remain_readable_and_new_empty_settings_do_not_enable_execution() -> (
    None
):
    old = parse_settings(b"schema_version: 1\nhistory:\n  retention_days: 30\n")
    assert old.schema_version == 1
    assert old.execution.profiles == ()
    new = parse_settings(
        b"schema_version: 2\nhistory:\n  retention_days: 30\nexecution:\n  profiles: []\n"
    )
    assert new.schema_version == 2
    assert new.execution.profiles == ()


def test_review_request_is_an_explicit_bounded_wire_kind() -> None:
    call = ExecutionCall(directory=Path("/work/demo"), operation="propose", argv=("python", "-V"))
    request = ExecutionRequest(
        expected_instance_id=UUID("00000000-0000-4000-8000-000000000002"), call=call
    )
    assert request.kind == "execution"
    assert request.revision == PROTOCOL_REVISION == 7
    assert request.call == call


@pytest.mark.parametrize(
    "argument",
    [
        "--password=synthetic",
        "--token",
        "API_KEY=synthetic",
        "https://user:synthetic@example.invalid",
    ],
)
def test_sensitive_argument_channels_refuse_before_review(argument: str) -> None:
    with pytest.raises(ValidationError):
        ExecutionCall(directory=Path("/work/demo"), operation="propose", argv=("python", argument))


def test_client_cannot_change_review_only_provenance() -> None:
    from jeff.application.execution import ActionManifest

    project = Project(UUID("00000000-0000-4000-8000-000000000001"), Path("/work/demo"))
    profile = ExecutionProfile(
        name="default",
        project_id=project.project_id,
        canonical_root=project.root,
        engine_id="declared",
        endpoint="unix:///private/tmp/socket",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="Python",
        executable="/usr/local/bin/python",
    )
    payload = build_manifest(project, profile, ("python", "-V")).model_dump(mode="json")
    assert ActionManifest.model_validate(payload).execution_available is False
    payload.update(authority_kind="live", execution_available=True)
    with pytest.raises(ValidationError):
        ActionManifest.model_validate(payload)

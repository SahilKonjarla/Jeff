"""Pure fresh-authority contracts; constructing these does not verify Docker or inputs."""

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jeff.application.execution import (
    ActionManifest,
    DockerCLIIdentity,
    DockerTargetDeclaration,
    ExecutionManifest,
    ExecutionProfile,
    PreparedFile,
    PreparedInputs,
)


def test_private_backend_configuration_is_explicit_bounded_and_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jeff.application.execution import DockerBackendConfiguration

    profile = ExecutionProfile(
        project_id=uuid4(),
        canonical_root=Path("/synthetic/project"),
        name="python",
        engine_id="synthetic-engine",
        endpoint="unix:///synthetic/docker.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="synthetic",
        executable="/usr/local/bin/python",
    )
    identity = DockerCLIIdentity(
        path=Path("/synthetic/private-cli/bin/docker"), sha256="a" * 64, size=10
    )
    declaration = DockerTargetDeclaration(profile=profile)
    declared = [declaration.model_dump(mode="json")]
    payload = {
        "binary": identity.model_dump(mode="json"),
        "config": "/synthetic/private-config",
        "declarations": declared,
    }

    def no_io(*args: object, **kwargs: object) -> None:
        raise AssertionError("Configuration construction must not inspect filesystem or Docker")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", no_io)
        patch.setattr(Path, "read_bytes", no_io)
        config = DockerBackendConfiguration.model_validate(payload)
        assert config.binary == identity and config.declarations == (declaration,)
        assert config.config == Path("/synthetic/private-config") and len(config.fingerprint) == 64
    assert DockerBackendConfiguration.model_validate_json(config.model_dump_json()) == config
    with pytest.raises(ValidationError):
        config.config = Path("/synthetic/changed")

    cases: tuple[dict[str, object], ...] = (
        {"declarations": []},
        {"declarations": declared * 17},
        {"declarations": declared * 2},
        {"config": "relative-config"},
        {"config": "/synthetic/private-cli/bin"},
        {"config": str(identity.path)},
        {"config": str(identity.path / "nested")},
        {"config": "/synthetic/../elsewhere"},
        {"binary": {"path": "docker", "sha256": "a" * 64, "size": 10}},
        {"binary": {"path": str(identity.path), "sha256": "SYNTHETIC_PRIVATE_VALUE", "size": 10}},
        {"credential": "SYNTHETIC_PRIVATE_VALUE"},
    )
    for changes in cases:
        with pytest.raises(ValidationError) as caught:
            DockerBackendConfiguration.model_validate({**payload, **changes})
        assert "SYNTHETIC_PRIVATE_VALUE" not in str(caught.value)

    # Aggregate declarations share the existing32KiB canonicalization bound;
    # individually valid declarations cannot multiply it without limit.
    large = tuple(
        DockerTargetDeclaration(
            profile=profile.model_copy(update={"name": f"python-{i}"}),
            image_environment=tuple((f"PUBLIC_{j}", "x" * 1000) for j in range(10)),
        )
        for i in range(4)
    )
    with pytest.raises(ValidationError):
        DockerBackendConfiguration(
            binary=identity, config=Path("/synthetic/private-config"), declarations=large
        )
    with pytest.raises(ValidationError):
        DockerBackendConfiguration.model_validate({})


def test_fresh_manifest_is_distinct_and_input_inventory_is_bounded() -> None:
    profile = ExecutionProfile(
        project_id=uuid4(),
        canonical_root=Path("/synthetic/project"),
        name="python",
        engine_id="synthetic-engine",
        endpoint="unix:///synthetic/docker.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="synthetic",
        executable="/usr/local/bin/python",
    )
    inputs = PreparedInputs(files=(PreparedFile(path="hello.py", size=7, sha256="2" * 64),))
    fresh = ExecutionManifest(profile=profile, argv=(profile.executable, "hello.py"), inputs=inputs)
    assert fresh.authority_kind == "execution_v1"
    assert len(fresh.fingerprint) == 64 and inputs.total_bytes == 7
    legacy = ActionManifest(profile=profile, argv=fresh.argv)
    with pytest.raises(ValidationError):
        ExecutionManifest.model_validate(legacy.model_dump())
    with pytest.raises(ValidationError):
        ActionManifest.model_validate(fresh.model_dump())
    with pytest.raises(ValidationError):
        PreparedFile(path="nested/.env", size=7, sha256="2" * 64)
    with pytest.raises(ValidationError):
        PreparedFile(path="../outside", size=7, sha256="2" * 64)
    with pytest.raises(ValidationError):
        PreparedInputs(files=(inputs.files[0], inputs.files[0]))
    with pytest.raises(ValidationError):
        PreparedFile(path="large.py", size=1048577, sha256="2" * 64)
    with pytest.raises(ValidationError):
        PreparedFile(path="*.py", size=7, sha256="2" * 64)
    with pytest.raises(ValidationError):
        PreparedInputs(
            files=tuple(
                PreparedFile(path=f"file-{i}.py", size=1048576, sha256="2" * 64) for i in range(9)
            )
        )

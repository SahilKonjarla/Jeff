"""Pure fresh-authority contracts; constructing these does not verify Docker or inputs."""

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jeff.application.execution import (
    ActionManifest,
    ExecutionManifest,
    ExecutionProfile,
    PreparedFile,
    PreparedInputs,
)


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

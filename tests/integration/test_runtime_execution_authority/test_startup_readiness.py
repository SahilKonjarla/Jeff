"""Cold configured readiness caches and conservative restarted-authority quarantine."""

import json
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.execution import (
    DockerBackendConfiguration,
    DockerCLIIdentity,
    DockerTargetDeclaration,
    ExecutionProfile,
    ReviewFault,
)
from jeff.application.runtime import RuntimeLayout
from jeff.infrastructure.runtime_diagnostics import RuntimeDiagnosticsAdapter
from jeff.infrastructure.runtime_settings import load_or_create_settings
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_authority import authority_store, command


def test_settings3_backend_cache_requires_ready_and_stopped_doctor_stays_cold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jeff.infrastructure import docker_cli

    def no_backend(*args: object, **kwargs: object) -> None:
        raise AssertionError("Settings loading must not construct a Docker adapter")

    monkeypatch.setattr(docker_cli.DockerTargetInspector, "__init__", no_backend)
    monkeypatch.setattr(docker_cli.DockerLifecycleCLI, "__init__", no_backend)
    for kind in ("configured", "disabled", "failed_database", "legacy"):
        root = tmp_path / kind[0]  # Keep synthetic native socket paths below the existing bound.
        layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
        owner = RuntimeStateStore(layout).acquire(create=True)
        assert owner is not None
        profile = ExecutionProfile(
            project_id=uuid4(),
            canonical_root=root / "project",
            name="python",
            engine_id="synthetic-no-query",
            endpoint="unix:///synthetic/nonexistent.sock",
            image_id="sha256:" + "1" * 64,
            platform="linux/arm64",
            toolchain="synthetic",
            executable="/usr/local/bin/python",
        )
        backend = DockerBackendConfiguration(
            binary=DockerCLIIdentity(path=Path("/synthetic/missing-cli"), sha256="a" * 64, size=1),
            config=Path("/synthetic/missing-config"),
            declarations=(DockerTargetDeclaration(profile=profile),),
        )
        value = {
            "schema_version": 3,
            "history": {"retention_days": 30},
            "execution": {"profiles": [profile.model_dump(mode="json")]},
            "backend": None if kind == "disabled" else backend.model_dump(mode="json"),
        }
        if kind == "legacy":
            value.pop("backend")
            value["schema_version"] = 2
        payload = json.dumps(value).encode()
        with owner:
            owner.store.credential(owner)
            load_or_create_settings(owner)
            layout.settings.write_bytes(payload)
            if kind == "failed_database":
                layout.database.write_bytes(b"SYNTHETIC_NON_DATABASE")
                layout.database.chmod(0o600)
            storage = SQLiteStorage(owner, instance_id=uuid4())
            assert storage.execution_backend is None
            storage.start()
            try:
                state = storage.wait_ready(5)
                if kind == "failed_database":
                    assert state.state == "failed" and storage.execution_backend is None
                else:
                    assert state.state == "ready"
                    expected = backend if kind == "configured" else None
                    assert storage.execution_backend == expected
                    # No hot reload or backend construction: paths are nonexistent.
                    if kind != "legacy":
                        layout.settings.write_bytes(json.dumps({**value, "backend": None}).encode())
                    assert storage.execution_backend == expected
                    layout.settings.write_bytes(payload)
                storage.request_close()
                assert storage.execution_backend is None
            finally:
                storage.close(timeout=5)
            assert storage.closed and storage.execution_backend is None
            assert layout.settings.read_bytes() == payload
        if kind != "failed_database":
            before = layout.settings.stat()

            def selected(current: RuntimeLayout = layout) -> RuntimeLayout:
                return current

            result = RuntimeDiagnosticsAdapter(selected).inspect()
            assert (
                result.ok
                and result.data.summary == "stopped"
                and result.data.settings_schema_version == (2 if kind == "legacy" else 3)
            )
            assert "synthetic/missing" not in result.model_dump_json()
            after = layout.settings.stat()
            assert (before.st_ino, before.st_mtime_ns, before.st_size) == (
                after.st_ino,
                after.st_mtime_ns,
                after.st_size,
            )
            assert layout.settings.read_bytes() == payload


def test_restart_invalidates_unused_authority_and_quarantines_unknown_claim(tmp_path: Path) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        first = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        second = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        for record in (first, second):
            store.authority(
                command(
                    project,
                    generation,
                    operation="approve",
                    approval_id=record.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        store.authority(
            command(
                project,
                generation,
                operation="claim",
                approval_id=first.approval_id,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        with pytest.raises(ReviewFault) as occupied:
            store.authority(
                command(
                    project,
                    generation,
                    operation="claim",
                    approval_id=second.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        assert occupied.value.code == "execution_capacity"
        store.close(timeout=5)
        new_generation = uuid4()
        again = SQLiteStorage(store.owner, instance_id=new_generation)
        again.start()
        try:
            assert again.wait_ready(5).state == "ready"
            old = again.authority(
                command(project, new_generation, operation="show", approval_id=first.approval_id),
                deadline=time.monotonic() + 5,
            )
            assert old.record.state == "consumed" and old.record.claim_state == "unknown"
            assert old.launch_claim is None
            unused = again.authority(
                command(project, new_generation, operation="show", approval_id=second.approval_id),
                deadline=time.monotonic() + 5,
            )
            assert unused.record.state == "invalidated" and unused.launch_claim is None
            fresh = again.authority(
                command(
                    project,
                    new_generation,
                    operation="propose",
                    manifest=manifest,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            ).record
            again.authority(
                command(
                    project,
                    new_generation,
                    operation="approve",
                    approval_id=fresh.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
            with pytest.raises(ReviewFault) as quarantined:
                again.authority(
                    command(
                        project,
                        new_generation,
                        operation="claim",
                        approval_id=fresh.approval_id,
                        current_manifest_digest=manifest.fingerprint,
                    ),
                    deadline=time.monotonic() + 5,
                )
            assert quarantined.value.code == "execution_outcome_unknown"
        finally:
            again.close(timeout=5)

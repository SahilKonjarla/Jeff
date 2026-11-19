"""Strict private settings parser and owned exclusive default creation."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.execution import (
    DockerBackendConfiguration,
    DockerCLIIdentity,
    DockerTargetDeclaration,
    ExecutionProfile,
)
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import StorageFault
from jeff.infrastructure.runtime_settings import load_or_create_settings, parse_settings
from jeff.infrastructure.runtime_state import RuntimeStateStore


def test_settings3_backend_is_opt_in_bound_and_legacy_bytes_are_preserved(tmp_path: Path) -> None:
    profile = ExecutionProfile(
        project_id=uuid4(),
        canonical_root=Path("/synthetic/project"),
        name="python",
        engine_id="synthetic",
        endpoint="unix:///synthetic/docker.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="synthetic",
        executable="/usr/local/bin/python",
    )
    backend = DockerBackendConfiguration(
        binary=DockerCLIIdentity(path=Path("/synthetic/bin/docker"), sha256="a" * 64, size=10),
        config=Path("/synthetic/config"),
        declarations=(DockerTargetDeclaration(profile=profile),),
    )
    value = {
        "schema_version": 3,
        "history": {"retention_days": 30},
        "execution": {"profiles": [profile.model_dump(mode="json")]},
        "backend": backend.model_dump(mode="json"),
    }
    payload = json.dumps(value).encode()
    settings = parse_settings(payload)
    from jeff.application.storage import ConfiguredRuntimeSettings

    assert isinstance(settings, ConfiguredRuntimeSettings)
    assert settings.schema_version == 3 and settings.backend == backend
    disabled = parse_settings(b"schema_version: 3\nhistory: {retention_days: 30}\n")
    assert isinstance(disabled, ConfiguredRuntimeSettings) and disabled.backend is None
    null_backend = parse_settings(json.dumps({**value, "backend": None}).encode())
    assert isinstance(null_backend, ConfiguredRuntimeSettings) and null_backend.backend is None
    # Other configured profiles may remain review-only, but every backend target
    # must match a whole configured profile, not just its ID/name.
    extra = {**profile.model_dump(mode="json"), "name": "review-only"}
    subset = parse_settings(
        json.dumps(
            {**value, "execution": {"profiles": [profile.model_dump(mode="json"), extra]}}
        ).encode()
    )
    assert isinstance(subset, ConfiguredRuntimeSettings) and subset.backend == backend
    invalid: list[dict[str, object]] = [
        {**value, "schema_version": v} for v in (1, 2, True, 3.0, "3", 4)
    ]
    invalid.extend(
        (
            {**value, "execution": {"profiles": []}},
            {**value, "backend": "SYNTHETIC_PRIVATE"},
            {**value, "unknown": "SYNTHETIC_PRIVATE"},
        )
    )
    for change in (
        {"canonical_root": "/synthetic/elsewhere"},
        {"image_id": "sha256:" + "2" * 64},
        {"memory_bytes": 16777216},
    ):
        invalid.append(
            {**value, "execution": {"profiles": [{**profile.model_dump(mode="json"), **change}]}}
        )
    for bad in invalid:
        with pytest.raises(StorageFault) as caught:
            parse_settings(json.dumps(bad).encode())
        assert caught.value.code == "settings_invalid" and "SYNTHETIC_PRIVATE" not in str(
            caught.value
        )
    for version in (1, 2):
        with pytest.raises(StorageFault):
            parse_settings(
                json.dumps(
                    {"schema_version": version, "history": {"retention_days": 30}, "backend": None}
                ).encode()
            )

    old_files = []
    for version in (1, 2):
        old = f"# keep me\nschema_version: {version}\nhistory: {{retention_days: 7}}\n".encode()
        old_files.append(old)
        legacy = parse_settings(old)
        assert legacy.model_dump(mode="json") == {
            "schema_version": version,
            "history": {"retention_days": 7},
            "execution": {"profiles": []},
        }
        assert "backend" not in legacy.model_dump(mode="json")

    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        _, source = load_or_create_settings(owner)
        assert source == "default_file"
        assert (
            layout.settings.read_bytes()
            == b"schema_version: 2\nhistory:\n  retention_days: 30\nexecution:\n  profiles: []\n"
        )
        for existing in (*old_files, payload):
            layout.settings.write_bytes(existing)
            observed, source = load_or_create_settings(owner)
            assert source == "existing_file" and layout.settings.read_bytes() == existing
            assert observed == parse_settings(existing)


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"schema_version: 5\nhistory: {retention_days: 30}",
        b"schema_version: 1\nhistory: {retention_days: true}",
        b"schema_version: 1\nhistory: {retention_days: '30'}",
        b"schema_version: 1\nhistory: {retention_days: 0}",
        b"schema_version: 1\nhistory: {retention_days: 30}\nunknown: SYNTHETIC_PRIVATE",
        b"schema_version: 1\nschema_version: 1\nhistory: {retention_days: 30}",
        b"schema_version: 1\nhistory: &unused {retention_days: 30}",
        b"!!python/object/apply:os.system ['SYNTHETIC_PRIVATE']",
        b"!!map {schema_version: 1, history: {retention_days: 30}}",
        b"\xff",
        b"x" * 65537,
    ],
)
def test_invalid_settings_fail_without_echo(payload: bytes) -> None:
    with pytest.raises(StorageFault) as caught:
        parse_settings(payload)
    assert caught.value.code == "settings_invalid"
    assert "SYNTHETIC_PRIVATE" not in str(caught.value)


def test_valid_settings_preserved_and_defaults_created_only_when_absent(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        settings, source = load_or_create_settings(owner)
        assert settings.history.retention_days == 30 and source == "default_file"
        assert layout.settings.stat().st_mode & 0o7777 == 0o600
        payload = b"# retained\nschema_version: 1\nhistory:\n  retention_days: 7\n"
        layout.settings.write_bytes(payload)
        settings, source = load_or_create_settings(owner)
        assert settings.history.retention_days == 7 and source == "existing_file"
        assert layout.settings.read_bytes() == payload


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "mode", "oversized"])
def test_unsafe_existing_settings_are_not_read_replaced_or_repaired(
    tmp_path: Path, kind: str
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        layout.data_dir.mkdir(mode=0o700)
        target = tmp_path / "synthetic"
        target.write_bytes(b"SYNTHETIC_PRIVATE")
        target.chmod(0o600)
        if kind == "symlink":
            layout.settings.symlink_to(target)
        elif kind == "hardlink":
            os.link(target, layout.settings)
        elif kind == "fifo":
            os.mkfifo(layout.settings, mode=0o600)
        elif kind == "directory":
            layout.settings.mkdir(mode=0o700)
        else:
            layout.settings.write_bytes(b"x" * 65537 if kind == "oversized" else b"{}")
            layout.settings.chmod(0o644 if kind == "mode" else 0o600)
        before = layout.settings.lstat()
        with pytest.raises(StorageFault) as caught:
            load_or_create_settings(owner)
        assert caught.value.code == "settings_invalid"
        assert target.read_bytes() == b"SYNTHETIC_PRIVATE"
        after = layout.settings.lstat()
        assert (before.st_ino, before.st_mode, before.st_nlink, before.st_size) == (
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
        )


def test_observed_settings_edit_during_read_fails_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        load_or_create_settings(owner)
        selected = layout.settings.stat()
        original = os.read
        changed = b"schema_version: 1\nhistory: {retention_days: 7}\n"
        modified = False

        def read(fd: int, size: int) -> bytes:
            nonlocal modified
            result = original(fd, size)
            info = os.fstat(fd)
            if not modified and (info.st_dev, info.st_ino) == (selected.st_dev, selected.st_ino):
                modified = True
                layout.settings.write_bytes(changed)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(os, "read", read)
            with pytest.raises(StorageFault) as caught:
                load_or_create_settings(owner)
        assert modified and caught.value.code == "settings_invalid"
        assert layout.settings.read_bytes() == changed

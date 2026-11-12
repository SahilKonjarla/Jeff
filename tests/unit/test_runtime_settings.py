"""Strict private settings parser and owned exclusive default creation."""

import os
from pathlib import Path

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import StorageFault
from jeff.infrastructure.runtime_settings import load_or_create_settings, parse_settings
from jeff.infrastructure.runtime_state import RuntimeStateStore


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"schema_version: 2\nhistory: {retention_days: 30}",
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

"""Missing/private metadata, ownership/type/mode policy and strictly read-only traversal."""

import os
import stat
from pathlib import Path

import pytest

from jeff.application.runtime import RuntimeLayout, RuntimePathInspection
from jeff.infrastructure.runtime_paths import inspect_layout

from .support_inspection import observations, private_files


def test_missing_installation_remains_missing_not_stopped(layout: RuntimeLayout) -> None:
    report = inspect_layout(layout)
    assert isinstance(report, RuntimePathInspection)
    assert {entry.state for entry in report.entries} == {"missing"}
    assert not layout.data_dir.exists()
    assert not layout.runtime_dir.exists()
    assert "stopped" not in report.model_dump_json()


def test_private_metadata_does_not_prove_a_running_process(layout: RuntimeLayout) -> None:
    private_files(layout)
    report = inspect_layout(layout)
    assert isinstance(report, RuntimePathInspection)
    assert {entry.role for entry in report.entries if entry.state == "present"} == {
        "data_dir",
        "runtime_dir",
        "database",
        "settings",
        "credential",
        "lock",
    }
    assert "SYNTHETIC_FIXTURE_CONTENT" not in report.model_dump_json()
    assert "running" not in report.model_dump_json()


@pytest.mark.parametrize(
    "role", ["data_dir", "runtime_dir", "database", "settings", "credential", "lock"]
)
def test_insecure_modes_are_rejected_not_repaired(layout: RuntimeLayout, role: str) -> None:
    private_files(layout)
    path = getattr(layout, role)
    assert isinstance(path, Path)
    mode = 0o755 if role.endswith("dir") else 0o644
    path.chmod(mode)
    result = observations(layout)
    assert result[role] == ("unsafe", "unsafe_runtime_path")
    assert stat.S_IMODE(path.stat().st_mode) == mode
    if role == "data_dir":
        assert result["credential"] == ("unknown", "unsafe_runtime_path")


def test_regular_state_file_hardlinks_are_not_adopted(layout: RuntimeLayout) -> None:
    private_files(layout)
    alias = layout.data_dir / "alias"
    os.link(layout.credential, alias)
    assert observations(layout)["credential"] == ("unsafe", "unsafe_runtime_path")
    assert alias.exists() and layout.credential.stat().st_nlink == 2


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_nonregular_credential_is_rejected_without_opening_it(
    layout: RuntimeLayout, kind: str
) -> None:
    private_files(layout)
    layout.credential.unlink()
    if kind == "symlink":
        layout.credential.symlink_to(layout.settings)
    elif kind == "directory":
        layout.credential.mkdir(mode=0o700)
    else:
        os.mkfifo(layout.credential, 0o600)
    assert observations(layout)["credential"] == ("unsafe", "unsafe_runtime_path")


def test_socket_path_requires_socket_type_not_a_pid_or_regular_file(layout: RuntimeLayout) -> None:
    private_files(layout)
    layout.socket.write_bytes(b"42")
    layout.socket.chmod(0o600)
    assert observations(layout)["socket"] == ("unsafe", "unsafe_runtime_path")


def test_untrusted_directory_and_parent_links_are_not_traversed(
    layout: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_files(layout)
    alias = layout.data_dir.parent / "alias"
    alias.symlink_to(layout.data_dir, target_is_directory=True)
    linked = RuntimeLayout(data_dir=alias, runtime_dir=layout.runtime_dir, uid=layout.uid)
    real_stat = os.stat

    def guarded_stat(
        path: str, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        assert path != "ipc.key", "Must not inspect below a rejected root"
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", guarded_stat)
        result = observations(linked)
    assert result["data_dir"] == ("unsafe", "unsafe_runtime_path")
    assert result["credential"] == ("unknown", "unsafe_runtime_path")
    parent_linked = RuntimeLayout(
        data_dir=alias / "nested", runtime_dir=layout.runtime_dir, uid=layout.uid
    )
    assert observations(parent_linked)["data_dir"] == ("unsafe", "unsafe_runtime_path")


def test_wrong_owner_policy_uses_metadata_not_permission_changes(layout: RuntimeLayout) -> None:
    private_files(layout)
    other_owner = RuntimeLayout(
        data_dir=layout.data_dir, runtime_dir=layout.runtime_dir, uid=layout.uid + 1
    )
    assert observations(other_owner)["data_dir"] == ("unsafe", "unsafe_runtime_path")


def test_inspection_opens_only_readonly_directories_and_preserves_fixture(
    layout: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_files(layout)
    paths = (layout.database, layout.settings, layout.credential, layout.lock)

    def snapshot() -> dict[Path, tuple[bytes, tuple[int, ...]]]:
        result: dict[Path, tuple[bytes, tuple[int, ...]]] = {}
        for path in paths:
            content, metadata = path.read_bytes(), path.stat()
            result[path] = (
                content,
                (
                    metadata.st_ino,
                    metadata.st_uid,
                    metadata.st_mode,
                    metadata.st_nlink,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                ),
            )
        return result

    before = snapshot()
    directory_entries = {
        root: sorted(path.name for path in root.iterdir())
        for root in (layout.data_dir, layout.runtime_dir)
    }
    original_open = os.open

    def directory_only(
        path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        assert flags & os.O_ACCMODE == os.O_RDONLY
        assert flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW
        assert not flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def no_content_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("Inspection may not read contents")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", directory_only)
        patch.setattr(os, "read", no_content_read)
        patch.setattr(Path, "read_bytes", no_content_read)
        patch.setattr(Path, "read_text", no_content_read)
        result = observations(layout)
    assert result["credential"][0] == "present"
    assert snapshot() == before
    assert {
        root: sorted(path.name for path in root.iterdir())
        for root in (layout.data_dir, layout.runtime_dir)
    } == directory_entries


def test_nested_runtime_does_not_traverse_a_rejected_data_directory(
    layout: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = RuntimeLayout(
        data_dir=layout.data_dir, runtime_dir=layout.data_dir / "r", uid=layout.uid
    )
    private_files(nested)
    nested.data_dir.chmod(0o755)
    real_stat = os.stat

    def guarded_stat(
        path: str, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        assert path != "daemon.lock", "Do not inspect beneath a rejected Jeff directory"
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", guarded_stat)
        result = observations(nested)
    assert result["runtime_dir"] == ("unsafe", "unsafe_runtime_path")
    assert result["lock"] == ("unknown", "unsafe_runtime_path")

"""Owned external filesystem fixtures; inspection never creates or adopts a runtime."""

import os
import sqlite3
import stat
import subprocess
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path

import pytest

from jeff.application.runtime import (
    ControlFault,
    RuntimeLayout,
    RuntimePathInputs,
    RuntimePathInspection,
)
from jeff.infrastructure.runtime_paths import inspect_layout, resolve_layout
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


@pytest.fixture
def layout(tmp_path_factory: pytest.TempPathFactory) -> RuntimeLayout:
    arena = tmp_path_factory.mktemp("rt").resolve()
    result = resolve_layout(
        RuntimePathInputs(
            platform="darwin",
            home=arena,
            temp_dir=arena,
            uid=os.geteuid(),
            data_dir=str(arena / "d"),
            runtime_dir=str(arena / "r"),
        )
    )
    assert isinstance(result, RuntimeLayout)
    return result


def test_missing_installation_remains_missing_not_stopped(layout: RuntimeLayout) -> None:
    report = inspect_layout(layout)
    assert isinstance(report, RuntimePathInspection)
    assert {entry.state for entry in report.entries} == {"missing"}
    assert not layout.data_dir.exists()
    assert not layout.runtime_dir.exists()
    assert "stopped" not in report.model_dump_json()


def private_files(layout: RuntimeLayout) -> None:
    layout.data_dir.mkdir(mode=0o700)
    layout.runtime_dir.mkdir(mode=0o700)
    for path in (layout.database, layout.settings, layout.credential, layout.lock):
        path.write_bytes(b"SYNTHETIC_FIXTURE_CONTENT")
        path.chmod(0o600)


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


def observations(layout: RuntimeLayout) -> dict[str, tuple[str, str | None]]:
    report = inspect_layout(layout)
    assert isinstance(report, RuntimePathInspection)
    return {entry.role: (entry.state, entry.issue) for entry in report.entries}


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


def test_metadata_denial_is_unknown_and_never_echoes_os_error(
    layout: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_files(layout)
    real_stat = os.stat

    def denied_stat(
        path: str, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        if path == "ipc.key":
            raise PermissionError("SYNTHETIC_PRIVATE_OS_ERROR")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", denied_stat)
        report = inspect_layout(layout)
    assert isinstance(report, RuntimePathInspection)
    assert {entry.role: entry.state for entry in report.entries}["credential"] == "unknown"
    assert "SYNTHETIC_PRIVATE_OS_ERROR" not in report.model_dump_json()


def test_disappearance_after_observation_is_not_reported_missing(
    layout: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_files(layout)
    real_stat = os.stat
    seen = False

    def disappearing_stat(
        path: str, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        nonlocal seen
        if path == "ipc.key":
            if seen:
                raise FileNotFoundError("SYNTHETIC_DISAPPEARANCE")
            seen = True
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", disappearing_stat)
        result = observations(layout)
    assert result["credential"] == ("unknown", "runtime_inspection_failed")


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


@pytest.mark.parametrize("change", ["owner", "inode", "socket_type"])
def test_file_metadata_policy_and_detected_replacements(
    layout: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    private_files(layout)
    if change == "socket_type":
        layout.socket.touch(mode=0o600)
    real_stat = os.stat
    calls = 0

    def changed_stat(
        path: str, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        nonlocal calls
        metadata = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        target = "daemon.sock" if change == "socket_type" else "ipc.key"
        if path != target:
            return metadata
        calls += 1
        fields = list(metadata)
        if change == "owner":
            fields[4] = layout.uid + 1
        elif change == "inode" and calls > 1:
            fields[1] += 1
        elif change == "socket_type":
            fields[0] = stat.S_IFSOCK | 0o600
        return os.stat_result(fields)

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", changed_stat)
        result = observations(layout)
    if change == "owner":
        assert result["credential"] == ("unsafe", "unsafe_runtime_path")
    elif change == "inode":
        assert result["credential"] == ("unknown", "runtime_inspection_failed")
    else:
        # Synthetic socket metadata verifies type policy only, never a bind/handshake.
        assert result["socket"] == ("present", None)


def test_directory_replacement_invalidates_its_whole_snapshot(
    layout: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_files(layout)
    real_stat = os.stat
    calls = 0

    def replaced_stat(
        path: str, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        nonlocal calls
        metadata = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if path == layout.data_dir.name:
            calls += 1
            if calls > 1:
                fields = list(metadata)
                fields[1] += 1
                return os.stat_result(fields)
        return metadata

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", replaced_stat)
        result = observations(layout)
    assert result["data_dir"] == ("unknown", "runtime_inspection_failed")
    assert result["credential"] == ("unknown", "runtime_inspection_failed")


def test_directory_open_failure_is_unknown_and_closes_owned_handles(
    layout: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_files(layout)
    original_open, original_close = os.open, os.close
    opened: list[int] = []
    closed: list[int] = []

    def open_directory(path: str, flags: int, *, dir_fd: int | None = None) -> int:
        if path == layout.data_dir.name:
            raise FileNotFoundError("SYNTHETIC_CHANGED_AFTER_STAT")
        fd = original_open(path, flags, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    def close_directory(fd: int) -> None:
        original_close(fd)
        closed.append(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_directory)
        patch.setattr(os, "close", close_directory)
        result = observations(layout)
    assert result["data_dir"] == ("unknown", "runtime_inspection_failed")
    assert Counter(opened) == Counter(closed)


def test_missing_root_with_changed_ancestor_is_unknown_not_absent(
    layout: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_stat = os.stat
    calls = 0

    def changed_parent(
        path: str, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        nonlocal calls
        metadata = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if path == layout.data_dir.parent.name:
            calls += 1
            if calls > 1:
                fields = list(metadata)
                fields[1] += 1
                return os.stat_result(fields)
        return metadata

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", changed_parent)
        result = observations(layout)
    assert result["data_dir"] == ("unknown", "runtime_inspection_failed")
    assert result["credential"] == ("unknown", "runtime_inspection_failed")


@pytest.mark.parametrize("phase", ["open", "final", "continued", "mode"])
def test_real_sqlite_journal_metadata_requires_one_coherent_directory_sample(
    layout: RuntimeLayout, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        owner.store.credential(owner)
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
        finally:
            storage.close()
        with closing(sqlite3.connect(layout.database, isolation_level=None)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            real_stat = os.stat
            directory_calls, key_calls = 0, 0
            primed = False
            observed: list[tuple[int, ...]] = []

            def interleaved(
                path: str, *, dir_fd: int | None = None, follow_symlinks: bool = True
            ) -> os.stat_result:
                nonlocal directory_calls, key_calls, primed
                result = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
                if path == layout.data_dir.name:
                    directory_calls += 1
                if path == "ipc.key":
                    key_calls += 1
                trigger = (
                    path == layout.data_dir.name and directory_calls == 1 and phase != "final"
                ) or (path == "ipc.key" and key_calls == 2 and phase == "final")
                if trigger:
                    before = real_stat(layout.data_dir, follow_symlinks=False)
                    connection.execute("PRAGMA user_version=4")  # Real journal, no business DML.
                    after = real_stat(layout.data_dir, follow_symlinks=False)
                    assert (before.st_dev, before.st_ino, before.st_mode, before.st_uid) == (
                        after.st_dev,
                        after.st_ino,
                        after.st_mode,
                        after.st_uid,
                    )
                    assert after.st_nlink != before.st_nlink
                    observed.append((before.st_nlink, after.st_nlink))
                    primed = True
                    if phase == "mode":
                        layout.data_dir.chmod(0o755)
                elif phase == "continued" and path == layout.data_dir.name and directory_calls == 2:
                    (layout.data_dir / "continued-fixture-churn").touch(mode=0o600)
                return result

            try:
                with monkeypatch.context() as patch:
                    patch.setattr(os, "stat", interleaved)
                    if phase in ("open", "final"):
                        assert RuntimeStateStore(layout).layout == layout
                    else:
                        with pytest.raises(ControlFault) as refused:
                            RuntimeStateStore(layout)
                        assert refused.value.code == "runtime_inspection_failed"
                assert primed and len(observed) == 1
                if phase == "continued":
                    assert directory_calls == 3
                    assert (layout.data_dir / "continued-fixture-churn").exists()
            finally:
                layout.data_dir.chmod(0o700)
                connection.execute("ROLLBACK")  # SQLite alone retires the real journal.


def test_fresh_imports_do_not_resolve_defaults_generate_entropy_or_create_state() -> None:
    code = """
import os, tempfile
from pathlib import Path
import jeff.application.projects

def forbidden(*args, **kwargs):
    raise AssertionError("Import attempted installation I/O/default selection")

Path.home = forbidden
tempfile.gettempdir = forbidden
os.getuid = os.geteuid = os.mkdir = os.open = os.urandom = forbidden
import jeff.domain.runtime
import jeff.application.runtime
import jeff.infrastructure.runtime_paths
print("import-safe")
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == "import-safe"

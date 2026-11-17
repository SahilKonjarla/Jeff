"""Metadata denial/replacement/ancestor races, owned-handle closure and real journal churn."""

import os
import sqlite3
import stat
from collections import Counter
from contextlib import closing

import pytest

from jeff.application.runtime import ControlFault, RuntimeLayout, RuntimePathInspection
from jeff.infrastructure.runtime_paths import inspect_layout
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_inspection import observations, private_files


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

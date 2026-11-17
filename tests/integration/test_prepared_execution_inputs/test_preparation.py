"""Reviewed copy/scope, unsafe aliases, finite limits and independent-preparer capacity."""

import fcntl
import hashlib
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock, get_ident
from uuid import UUID, uuid4

import pytest

from jeff.application.execution import PreparedInputFault
from jeff.infrastructure.prepared_inputs import FilePreparedInputStore

from .support_inputs import inputs_store, manifest


def test_copy_verifies_reviewed_snapshot_without_recopied_live_source(tmp_path: Path) -> None:
    with inputs_store(tmp_path) as (store, project, data):
        (project.root / "src").mkdir()
        (project.root / "src/code.py").write_bytes(b"print('synthetic')\n")
        (project.root / "empty.txt").write_bytes(b"")
        prepared = store.prepare(
            project, ("src/code.py", "empty.txt"), deadline=time.monotonic() + 10
        )
        action = manifest(project, prepared)
        binding = store.verify(action, deadline=time.monotonic() + 5)
        assert binding.path == data / "jobs" / str(prepared.snapshot_id) / "input"
        assert binding.inventory_digest == prepared.digest
        assert tuple(item.path for item in prepared.files) == ("empty.txt", "src/code.py")
        assert prepared.files[1].sha256 == hashlib.sha256(b"print('synthetic')\n").hexdigest()
        assert (binding.path / "src/code.py").read_bytes() == b"print('synthetic')\n"
        assert stat.S_IMODE(binding.path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(binding.path.stat().st_mode) == 0o555
        assert stat.S_IMODE((binding.path / "src/code.py").stat().st_mode) == 0o444
        (project.root / "src/code.py").write_bytes(b"changed after preparation\n")
        store.check(binding, action, deadline=time.monotonic() + 5)
        assert (binding.path / "src/code.py").read_bytes() == b"print('synthetic')\n"
        with pytest.raises(PreparedInputFault):
            store.verify(
                action.model_copy(
                    update={"profile": action.profile.model_copy(update={"project_id": uuid4()})}
                ),
                deadline=time.monotonic() + 5,
            )
        with pytest.raises(PreparedInputFault):
            store.check(
                binding.model_copy(update={"path": project.root}),
                action,
                deadline=time.monotonic() + 5,
            )


def test_literal_aliases_and_unsafe_source_types_refuse_without_secret_reads(
    tmp_path: Path,
) -> None:
    with inputs_store(tmp_path / "p") as (store, project, data):
        for names in (
            (".env",),
            ("a/.env.example",),
            ("../outside",),
            ("*.py",),
            ("A/x", "a/y"),
            ("x/é.py", "x/e\u0301.py"),
            ("x", "x/y"),
        ):
            with pytest.raises(PreparedInputFault):
                store.prepare(project, names, deadline=time.monotonic() + 5)
        assert not (data / "jobs").exists()  # No protected source was opened or copied.
        target = project.root / "public.txt"
        target.write_bytes(b"synthetic public")
        (project.root / "linked").symlink_to(target)
        os.link(target, project.root / "hard")
        (project.root / "folder").mkdir()
        (project.root / "dirlink").symlink_to(project.root / "folder", target_is_directory=True)
        os.mkfifo(project.root / "pipe", mode=0o600)
        for name in ("linked", "hard", "folder", "dirlink/leaf", "pipe"):
            with pytest.raises(PreparedInputFault) as error:
                store.prepare(project, (name,), deadline=time.monotonic() + 5)
            assert error.value.code == "input_unsafe"
        assert tuple((data / "jobs").iterdir()) == (data / "jobs/.prepare.lock",)


def test_file_inventory_total_and_deadline_limits_are_finite(tmp_path: Path) -> None:
    with inputs_store(tmp_path) as (store, project, data):
        with pytest.raises(PreparedInputFault) as deadline:
            store.prepare(project, (), deadline=time.monotonic() - 1)
        assert deadline.value.code == "input_timeout"
        with pytest.raises(PreparedInputFault) as count:
            store.prepare(project, tuple(f"f{i}" for i in range(65)), deadline=time.monotonic() + 5)
        assert count.value.code == "input_limit"
        with pytest.raises(PreparedInputFault):
            store.prepare(
                project,
                tuple(f"d{i}/" + "x" * 600 for i in range(64)),
                deadline=time.monotonic() + 5,
            )
        assert not (data / "jobs").exists()
        for index in range(9):
            with (project.root / f"f{index}").open("wb") as stream:
                stream.truncate(1048576)
        with pytest.raises(PreparedInputFault) as total:
            store.prepare(project, tuple(f"f{i}" for i in range(9)), deadline=time.monotonic() + 5)
        assert total.value.code == "input_limit"
        with (project.root / "large").open("wb") as stream:
            stream.truncate(1048577)
        with pytest.raises(PreparedInputFault) as large:
            store.prepare(project, ("large",), deadline=time.monotonic() + 5)
        assert large.value.code == "input_limit"
        prepared = store.prepare(project, ("f0",), deadline=time.monotonic() + 10)
        assert prepared.total_bytes == 1048576
        store.verify(manifest(project, prepared), deadline=time.monotonic() + 5)


def test_capacity_includes_failed_residue_and_serializes_independent_preparers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with inputs_store(tmp_path / "a") as (store, project, data):
        (project.root / "file.txt").write_bytes(b"synthetic")

        def failed_sync(fd: int) -> None:
            raise OSError("synthetic preparation interruption")

        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", failed_sync)
            with pytest.raises(PreparedInputFault):
                store.prepare(project, ("file.txt",), deadline=time.monotonic() + 5)
        for _ in range(16):
            store.prepare(project, (), deadline=time.monotonic() + 5)
        with pytest.raises(PreparedInputFault) as full:
            store.prepare(project, (), deadline=time.monotonic() + 5)
        assert full.value.code == "input_capacity"
        assert len([p for p in (data / "jobs").iterdir() if p.is_dir()]) == 17

    def check_contenders(root: Path, existing: int) -> None:
        with inputs_store(root) as (store, project, data):
            for _ in range(existing):
                store.prepare(project, (), deadline=time.monotonic() + 5)
            barrier = Barrier(8)
            simultaneous_open = Barrier(8)
            completed = Event()
            mutex = Lock()
            finished = 0
            selected = False
            opened_threads: set[int] = set()
            original_flock = fcntl.flock
            original_open = os.open

            def simultaneous_lock_open(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                if path == ".prepare.lock":
                    with mutex:
                        first_open = get_ident() not in opened_threads
                        opened_threads.add(get_ident())
                    if first_open:
                        simultaneous_open.wait(timeout=5)
                return original_open(path, flags, mode, dir_fd=dir_fd)

            def contended_flock(fd: int, operation: int) -> None:
                nonlocal selected
                original_flock(fd, operation)
                with mutex:
                    hold = not selected
                    selected = True
                if hold:
                    assert completed.wait(timeout=5), "contenders did not finish within bound"

            def prepare(index: int) -> UUID | str:
                nonlocal finished
                independent = FilePreparedInputStore(store.owner)
                barrier.wait(timeout=5)
                try:
                    return independent.prepare(
                        project, (), deadline=time.monotonic() + 5
                    ).snapshot_id
                except PreparedInputFault as error:
                    return error.code
                finally:
                    with mutex:
                        finished += 1
                        if finished >= 7:
                            completed.set()

            # Force the actual cold-open race and retain the winner's actual flock
            # until contenders finish. No synthetic errno or weakened assertion.
            with monkeypatch.context() as patch:
                patch.setattr(os, "open", simultaneous_lock_open)
                patch.setattr(fcntl, "flock", contended_flock)
                with ThreadPoolExecutor(max_workers=8) as callers:
                    outcomes = list(callers.map(prepare, range(8)))
            assert sum(isinstance(value, UUID) for value in outcomes) == 1, outcomes
            assert outcomes.count("input_busy") == 7, outcomes
            assert len([p for p in (data / "jobs").iterdir() if p.is_dir()]) == existing + 1
            for _ in range(16 - existing):
                store.prepare(project, (), deadline=time.monotonic() + 5)
            with pytest.raises(PreparedInputFault) as full:
                store.prepare(project, (), deadline=time.monotonic() + 5)
            assert full.value.code == "input_capacity"

    check_contenders(tmp_path / "b", 0)
    check_contenders(tmp_path / "c", 16)

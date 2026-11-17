"""Source drift, sealing/postcheck/close interruption and prepared-namespace tampering."""

import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from jeff.application.execution import PreparedInputFault, PreparedInputs
from jeff.infrastructure.prepared_inputs import FilePreparedInputStore

from .support_inputs import inputs_store, manifest


def test_source_change_and_sealing_interruption_leave_unusable_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with inputs_store(tmp_path / "a") as (store, project, data):
        source = project.root / "code.txt"
        source.write_bytes(b"a" * 131072)
        info = source.stat()
        original_read = os.read
        changed = False

        def changing_read(fd: int, count: int) -> bytes:
            nonlocal changed
            value = original_read(fd, count)
            current = os.fstat(fd)
            if not changed and (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                changed = True
                source.write_bytes(b"b" * 131072)
                os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
            return value

        with monkeypatch.context() as patch:
            patch.setattr(os, "read", changing_read)
            with pytest.raises(PreparedInputFault) as failure:
                store.prepare(project, ("code.txt",), deadline=time.monotonic() + 5)
        assert changed and failure.value.code == "input_changed"
        residues = [path for path in (data / "jobs").iterdir() if path.is_dir()]
        assert len(residues) == 1 and not (residues[0] / "prepared.json").exists()
    with inputs_store(tmp_path / "b") as (store, project, data):
        (project.root / "code.txt").write_bytes(b"synthetic")
        original_sync = os.fsync
        interrupted = False

        def failed_sync(fd: int) -> None:
            nonlocal interrupted
            if not interrupted and any(
                (p / "prepared.json").exists() for p in (data / "jobs").iterdir() if p.is_dir()
            ):
                interrupted = True
                raise OSError("synthetic sealing failure")
            original_sync(fd)

        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", failed_sync)
            with pytest.raises(PreparedInputFault) as failure:
                store.prepare(project, ("code.txt",), deadline=time.monotonic() + 5)
        assert interrupted and failure.value.code == "input_unconfirmed"
        residues = [path for path in (data / "jobs").iterdir() if path.is_dir()]
        assert len(residues) == 1 and not (residues[0] / "prepared.json").exists()
        assert (residues[0] / "input/code.txt").read_bytes() == b"synthetic"
        unusable = PreparedInputs(snapshot_id=UUID(residues[0].name))
        with pytest.raises(PreparedInputFault):
            store.verify(manifest(project, unusable), deadline=time.monotonic() + 5)
    with inputs_store(tmp_path / "c") as (store, project, data):
        original_link = os.link
        original_stat = os.stat
        published = False
        interrupted = False

        def published_link(*args: Any, **kwargs: Any) -> None:
            nonlocal published
            original_link(*args, **kwargs)
            if len(args) > 1 and args[1] == "prepared.json":
                published = True

        def late_stat(*args: Any, **kwargs: Any) -> os.stat_result:
            nonlocal interrupted
            if published and not interrupted and args and args[0] == "input":
                interrupted = True
                raise OSError("synthetic final namespace postcheck failure")
            return original_stat(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, "link", published_link)
            patch.setattr(os, "stat", late_stat)
            with pytest.raises(PreparedInputFault):
                store.prepare(project, (), deadline=time.monotonic() + 5)
        assert published and interrupted
        residues = [path for path in (data / "jobs").iterdir() if path.is_dir()]
        assert len(residues) == 1 and (residues[0] / "input").is_dir()
        rejected = PreparedInputs(snapshot_id=UUID(residues[0].name))
        with pytest.raises(PreparedInputFault):
            FilePreparedInputStore(store.owner).verify(
                manifest(project, rejected), deadline=time.monotonic() + 5
            )
        assert not (residues[0] / "prepared.json").exists()
    with inputs_store(tmp_path / "d") as (store, project, data):
        original_close = os.close
        interrupted = False

        def late_close(fd: int) -> None:
            nonlocal interrupted
            info = os.fstat(fd)
            original_close(fd)
            if not interrupted and stat.S_ISREG(info.st_mode) and info.st_size > 0:
                interrupted = True
                raise OSError("synthetic completed close with uncertain acknowledgement")

        with monkeypatch.context() as patch:
            patch.setattr(os, "close", late_close)
            with pytest.raises(PreparedInputFault):
                store.prepare(project, (), deadline=time.monotonic() + 5)
        assert interrupted
        residues = [path for path in (data / "jobs").iterdir() if path.is_dir()]
        assert len(residues) == 1
        rejected = PreparedInputs(snapshot_id=UUID(residues[0].name))
        with pytest.raises(PreparedInputFault):
            FilePreparedInputStore(store.owner).verify(
                manifest(project, rejected), deadline=time.monotonic() + 5
            )
        assert not (residues[0] / "prepared.json").exists()


def test_snapshot_tampering_and_namespace_swaps_never_return_a_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index, kind in enumerate(("bytes", "extra", "metadata", "hard", "directory", "during")):
        with inputs_store(tmp_path / str(index)) as (store, project, data):
            (project.root / "file.txt").write_bytes(b"reviewed bytes")
            prepared = store.prepare(project, ("file.txt",), deadline=time.monotonic() + 5)
            action = manifest(project, prepared)
            binding = store.verify(action, deadline=time.monotonic() + 5)
            root = binding.path
            leaf = root / "file.txt"
            if kind == "bytes":
                leaf.chmod(0o600)
                leaf.write_bytes(b"changed bytes")
                leaf.chmod(0o444)
            elif kind == "extra":
                root.chmod(0o700)
                (root / "extra").write_bytes(b"not selected")
                root.chmod(0o555)
            elif kind == "metadata":
                marker = root.parent / "prepared.json"
                raw = marker.read_bytes()
                marker.write_bytes(raw.replace(b'{"version":1', b'{"version":1,"version":1', 1))
            elif kind == "hard":
                os.link(leaf, root.parent / "other-link")
            elif kind == "directory":
                root.rename(root.parent / "old")
                root.mkdir(mode=0o555)
            if kind == "during":
                original = os.read
                before = leaf.stat()
                swapped = False

                def swap_read(
                    fd: int,
                    count: int,
                    read: Callable[[int, int], bytes] = original,
                    identity: tuple[int, int] = (before.st_dev, before.st_ino),
                    directory: Path = root,
                ) -> bytes:
                    nonlocal swapped
                    value = read(fd, count)
                    info = os.fstat(fd)
                    if not swapped and (info.st_dev, info.st_ino) == identity:
                        swapped = True
                        directory.rename(directory.parent / "old")
                        directory.mkdir(mode=0o700)
                        (directory / "file.txt").write_bytes(b"unreviewed bytes")
                        (directory / "file.txt").chmod(0o444)
                        directory.chmod(0o555)
                    return value

                with monkeypatch.context() as patch:
                    patch.setattr(os, "read", swap_read)
                    with pytest.raises(PreparedInputFault):
                        store.verify(action, deadline=time.monotonic() + 5)
                assert swapped
            else:
                with pytest.raises(PreparedInputFault):
                    store.verify(action, deadline=time.monotonic() + 5)

"""Private state/ownership through owned roots only; never normal installation state."""

import hashlib
import os
import select
import subprocess
import sys
from collections.abc import Iterator

import pytest

from jeff.application.runtime import ControlFault, RuntimeLayout
from jeff.infrastructure.runtime_state import RuntimeStateStore


@pytest.fixture
def layout(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RuntimeLayout]:
    root = tmp_path_factory.mktemp("st").resolve()
    result = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    yield result
    result.credential.unlink(missing_ok=True)


def test_stopped_inspection_never_creates_state(layout: RuntimeLayout) -> None:
    store = RuntimeStateStore(layout)
    with pytest.raises(ControlFault) as caught:
        store.acquire(create=False)
    assert caught.value.code == "runtime_absent"
    assert not layout.runtime_dir.exists()
    assert not layout.data_dir.exists()


def test_private_creation_lock_exclusion_and_stable_anchor(layout: RuntimeLayout) -> None:
    store = RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    try:
        token = store.credential(owner)
        assert len(token) == 32
        digest = hashlib.sha256(token).digest()
        anchor = layout.lock.stat().st_ino
        assert store.acquire(create=True) is None
        assert layout.credential.stat().st_mode & 0o7777 == 0o600
        assert layout.runtime_dir.stat().st_mode & 0o7777 == 0o700
    finally:
        owner.close()
    again = store.acquire(create=True)
    assert again is not None
    try:
        preserved = hashlib.sha256(store.credential(again)).digest() == digest
        assert preserved
        assert layout.lock.stat().st_ino == anchor
    finally:
        again.close()
    assert layout.lock.exists()
    assert not layout.database.exists() and not layout.settings.exists()


def test_lock_is_really_inherited_across_exec_after_launcher_closes_copy(
    layout: RuntimeLayout,
) -> None:
    store = RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    code = """
import os,sys
from pathlib import Path
from jeff.application.runtime import RuntimeLayout
from jeff.infrastructure.runtime_state import RuntimeStateStore
store=RuntimeStateStore(RuntimeLayout(data_dir=Path(sys.argv[1]),runtime_dir=Path(sys.argv[2]),uid=os.geteuid()))
owner=store.adopt(int(sys.argv[3]))
print("adopted",flush=True)
sys.stdin.read()
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            code,
            str(layout.data_dir),
            str(layout.runtime_dir),
            str(owner.fd),
        ],
        pass_fds=(owner.fd,),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline() == b"adopted\n"
        owner.close()
        assert store.acquire(create=False) is None
    finally:
        owner.close()
        assert child.stdin is not None
        child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
    assert child.returncode == 0
    unlocked = store.acquire(create=False)
    assert unlocked is not None
    unlocked.close()


def test_state_adapter_rejects_paths_outside_its_selected_roots(layout: RuntimeLayout) -> None:
    store = RuntimeStateStore(layout)
    with pytest.raises(ControlFault) as caught:
        with store.directory(layout.runtime_dir.parent / "unselected", create=True):
            pytest.fail("Unselected directory was accepted")
    assert caught.value.code == "invalid_runtime_path"
    assert not (layout.runtime_dir.parent / "unselected").exists()


def test_roots_cannot_collide_with_fixed_file_roles(layout: RuntimeLayout) -> None:
    for data, runtime in (
        (layout.runtime_dir / "daemon.sock", layout.runtime_dir),
        (layout.data_dir, layout.data_dir / "ipc.key/nested"),
    ):
        with pytest.raises(ControlFault) as caught:
            RuntimeStateStore(RuntimeLayout(data_dir=data, runtime_dir=runtime, uid=layout.uid))
        assert caught.value.code == "invalid_runtime_path"
    assert not layout.runtime_dir.exists() and not layout.data_dir.exists()


@pytest.mark.parametrize("mode", [0o777, 0o770])
def test_writable_nonsticky_ancestor_cannot_split_runtime_ownership(
    layout: RuntimeLayout, mode: int
) -> None:
    parent = layout.runtime_dir.parent / "writable"
    parent.mkdir(mode=mode)
    parent.chmod(mode)
    selected = RuntimeLayout(data_dir=layout.data_dir, runtime_dir=parent / "r", uid=layout.uid)
    store = RuntimeStateStore(selected)
    with pytest.raises(ControlFault) as caught:
        store.acquire(create=True)
    assert caught.value.code == "unsafe_runtime_path"
    assert not selected.runtime_dir.exists()


def test_sticky_owned_temp_parent_is_allowed_without_requiring_parent_mode_0700(
    layout: RuntimeLayout,
) -> None:
    parent = layout.runtime_dir.parent / "sticky"
    parent.mkdir(mode=0o1777)
    parent.chmod(0o1777)
    selected = RuntimeLayout(data_dir=layout.data_dir, runtime_dir=parent / "r", uid=layout.uid)
    owner = RuntimeStateStore(selected).acquire(create=True)
    assert owner is not None
    owner.close()


@pytest.mark.parametrize("failure_at", range(1, 50))
def test_metadata_failures_never_leak_owned_directory_or_lock_descriptors(
    layout: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
    failure_at: int,
) -> None:
    layout.runtime_dir.mkdir(mode=0o700)
    store = RuntimeStateStore(layout)
    original_open, original_close, original_dup, original_stat = os.open, os.close, os.dup, os.stat
    live: set[int] = set()
    count = 0

    def opened(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        live.add(fd)
        return fd

    def duplicated(fd: int) -> int:
        result = original_dup(fd)
        live.add(result)
        return result

    def closed(fd: int) -> None:
        original_close(fd)
        live.discard(fd)

    def denied(
        path: str, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        nonlocal count
        count += 1
        if count == failure_at:
            raise PermissionError("SYNTHETIC_METADATA_DENIAL")
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opened)
        patch.setattr(os, "close", closed)
        patch.setattr(os, "dup", duplicated)
        patch.setattr(os, "stat", denied)
        try:
            try:
                owner = store.acquire(create=True)
            except ControlFault:
                pass
            else:
                assert owner is not None
                owner.close()
            assert not live, "Metadata failure leaked owned descriptors"
        finally:
            for fd in live:
                original_close(fd)


@pytest.mark.parametrize("bad", ["mode", "link", "hardlink", "length"])
def test_unsafe_existing_credential_is_not_replaced(layout: RuntimeLayout, bad: str) -> None:
    store = RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    try:
        store.credential(owner)
        if bad == "mode":
            layout.credential.chmod(0o644)
        elif bad == "link":
            layout.credential.unlink()
            layout.credential.symlink_to(layout.lock)
        elif bad == "hardlink":
            os.link(layout.credential, layout.data_dir / "alias")
        else:
            layout.credential.write_bytes(b"synthetic-invalid")
        before = layout.credential.lstat()
        with pytest.raises(ControlFault):
            store.credential(owner)
        assert layout.credential.lstat() == before
    finally:
        owner.close()

"""Private state/ownership through owned roots only; never normal installation state."""

import errno
import hashlib
import json
import os
import select
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.runtime import ControlFault, RuntimeLayout
from jeff.infrastructure.runtime_state import Ownership, RuntimeStateStore


@pytest.fixture
def layout(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RuntimeLayout]:
    root = tmp_path_factory.mktemp("st").resolve()
    result = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    yield result
    result.credential.unlink(missing_ok=True)


@pytest.mark.parametrize("mutation", ["directory_content", "directory_mode", "file_content"])
def test_exact_birth_sampling_handles_only_benign_directory_metadata_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from jeff.infrastructure import runtime_state

    target = tmp_path / "sample"
    if mutation == "file_content":
        target.write_bytes(b"a")
        target.chmod(0o600)
    else:
        target.mkdir(mode=0o700)
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    original = os.fstat
    changed = False
    try:
        expected = runtime_state._native_identity(fd)

        def sample(descriptor: int) -> os.stat_result:
            nonlocal changed
            before = original(descriptor)
            if descriptor == fd and not changed:
                changed = True
                if mutation == "directory_content":
                    (target / "created").write_bytes(b"")
                elif mutation == "directory_mode":
                    target.chmod(0o755)
                else:
                    target.write_bytes(b"ab")
            return before

        monkeypatch.setattr(os, "fstat", sample)
        if mutation == "directory_content":
            assert runtime_state._native_identity(fd) == expected
            calls = 0

            def changing(descriptor: int) -> os.stat_result:
                nonlocal calls
                before = original(descriptor)
                if descriptor == fd:
                    calls += 1
                    (target / f"later-{calls}").write_bytes(b"")
                    if calls > 1:
                        return original(descriptor)
                return before

            monkeypatch.setattr(os, "fstat", changing)
            with pytest.raises(ControlFault) as unstable:
                runtime_state._native_identity(fd)
            assert unstable.value.code == "runtime_ownership_unconfirmed"
            assert calls == 2  # One fresh observation, never an unbounded retry loop.
        else:
            with pytest.raises(ControlFault) as raised:
                runtime_state._native_identity(fd)
            assert raised.value.code == "runtime_ownership_unconfirmed"
        assert changed
    finally:
        os.close(fd)


def test_owned_endpoint_publishes_bounded_private_proof_and_retires_it(
    layout: RuntimeLayout,
) -> None:
    store = RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    with owner, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        token = store.credential(owner)
        endpoint.bind(str(layout.socket))
        layout.socket.chmod(0o600)
        identity = owner.endpoint_identity()
        proof = owner.publish_endpoint(uuid4(), token)
        receipt = layout.runtime_dir / "endpoint.json"
        assert 0 < receipt.stat().st_size <= 2048
        assert receipt.stat().st_mode & 0o7777 == 0o600 and receipt.stat().st_nlink == 1
        assert token.hex().encode() not in receipt.read_bytes()
        endpoint.close()
        owner.remove_endpoint(identity, receipt=proof, token=token)
        assert not receipt.exists() and not layout.socket.exists()
        assert layout.lock.exists()


@pytest.mark.parametrize("residue", ["socket", "receipt_only"])
def test_explicit_owned_preparation_recovers_only_proven_residue(
    layout: RuntimeLayout, residue: str
) -> None:
    store = RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    with owner, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        token = store.credential(owner)
        endpoint.bind(str(layout.socket))
        layout.socket.chmod(0o600)
        owner.publish_endpoint(uuid4(), token)
        endpoint.close()
        if residue == "receipt_only":
            layout.socket.unlink()  # exact synthetic entry simulating the between-unlinks crash
        key = layout.credential.read_bytes()
        anchor = layout.lock.stat().st_ino
        owner.prepare_endpoint(time.monotonic() + 5)
        assert not layout.socket.exists() and not (layout.runtime_dir / "endpoint.json").exists()
        assert layout.credential.read_bytes() == key and layout.lock.stat().st_ino == anchor


@pytest.fixture
def recorded(layout: RuntimeLayout) -> Iterator[tuple[Ownership, bytes]]:
    store = RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    with owner, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        token = store.credential(owner)
        endpoint.bind(str(layout.socket))
        layout.socket.chmod(0o600)
        owner.publish_endpoint(uuid4(), token)
        endpoint.close()
        yield owner, token


def proof_snapshot(root: Path) -> dict[str, tuple[int, int, int, int, str | None]]:
    return {
        str(path.relative_to(root)): (
            path.lstat().st_ino,
            path.lstat().st_mode,
            path.lstat().st_nlink,
            path.lstat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() and not path.is_symlink()
            else None,
        )
        for path in root.rglob("*")
    }


@pytest.mark.parametrize(
    "change",
    [
        "mac",
        "version",
        "duplicate",
        "unknown",
        "oversized",
        "self_copy",
        "socket_replace",
        "key_replace",
        "key_missing",
        "mode",
        "symlink",
        "hardlink",
        "namespace_mode",
        "namespace_swap",
    ],
)
def test_ambiguous_endpoint_proof_is_refused_without_mutation(
    layout: RuntimeLayout,
    recorded: tuple[Ownership, bytes],
    change: str,
) -> None:
    owner, _ = recorded
    receipt = layout.endpoint_receipt
    data = receipt.read_bytes()
    if change in ("mac", "version", "unknown"):
        obj = json.loads(data)
        if change == "mac":
            obj["mac"] = "0" * 64
        elif change == "version":
            obj["version"] = 2
        else:
            obj["unexpected"] = True
        receipt.write_text(json.dumps(obj, sort_keys=True, separators=(",", ":")))
    elif change == "duplicate":
        receipt.write_bytes(b'{"version":1,' + data[1:])
    elif change == "oversized":
        receipt.write_bytes(b"x" * 2049)
    elif change == "self_copy":
        receipt.rename(layout.runtime_dir / "original-receipt")
        receipt.write_bytes(data)
        receipt.chmod(0o600)
    elif change == "socket_replace":
        layout.socket.rename(layout.runtime_dir / "original-socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as other:
            other.bind(str(layout.socket))
            layout.socket.chmod(0o600)
    elif change == "key_replace":
        layout.credential.write_bytes(b"\x01" * 32)
    elif change == "key_missing":
        layout.credential.rename(layout.data_dir / "saved-key")
    elif change == "mode":
        receipt.chmod(0o644)
    elif change == "symlink":
        receipt.rename(layout.runtime_dir / "saved-receipt")
        receipt.symlink_to(layout.runtime_dir / "saved-receipt")
    elif change == "hardlink":
        os.link(receipt, layout.runtime_dir / "receipt-alias")
    elif change == "namespace_mode":
        layout.runtime_dir.chmod(0o755)
    elif change == "namespace_swap":
        layout.runtime_dir.rename(layout.runtime_dir.parent / "original-runtime")
        layout.runtime_dir.mkdir(mode=0o700)
    before = proof_snapshot(layout.runtime_dir.parent)
    with pytest.raises(ControlFault):
        owner.prepare_endpoint(time.monotonic() + 5)
    assert proof_snapshot(layout.runtime_dir.parent) == before


def test_receipt_without_existing_anchor_cannot_mint_new_authority(layout: RuntimeLayout) -> None:
    layout.runtime_dir.mkdir(mode=0o700)
    layout.endpoint_receipt.write_bytes(b"{}")
    layout.endpoint_receipt.chmod(0o600)
    before = proof_snapshot(layout.runtime_dir.parent)
    with pytest.raises(ControlFault) as error:
        RuntimeStateStore(layout).acquire(create=True)
    assert error.value.code == "runtime_endpoint_collision"
    assert proof_snapshot(layout.runtime_dir.parent) == before and not layout.lock.exists()


def test_unrecorded_socket_cannot_be_removed_by_preparation(layout: RuntimeLayout) -> None:
    store = RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    with owner, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        store.credential(owner)
        endpoint.bind(str(layout.socket))
        layout.socket.chmod(0o600)
        endpoint.close()
        before = proof_snapshot(layout.runtime_dir.parent)
        with pytest.raises(ControlFault) as error:
            owner.prepare_endpoint(time.monotonic() + 5)
        assert error.value.code == "runtime_endpoint_collision"
        assert proof_snapshot(layout.runtime_dir.parent) == before


def test_expired_endpoint_preparation_does_not_mutate_proven_pair(
    layout: RuntimeLayout,
    recorded: tuple[Ownership, bytes],
) -> None:
    owner, _ = recorded
    before = proof_snapshot(layout.runtime_dir.parent)
    with pytest.raises(ControlFault) as error:
        owner.prepare_endpoint(time.monotonic() - 1)
    assert error.value.code == "runtime_start_timeout"
    assert proof_snapshot(layout.runtime_dir.parent) == before


def test_responding_endpoint_blocks_cleanup_even_with_valid_proof(layout: RuntimeLayout) -> None:
    store = RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    with owner, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        token = store.credential(owner)
        endpoint.bind(str(layout.socket))
        layout.socket.chmod(0o600)
        endpoint.listen(1)
        owner.publish_endpoint(uuid4(), token)
        before = proof_snapshot(layout.runtime_dir.parent)
        with pytest.raises(ControlFault) as error:
            owner.prepare_endpoint(time.monotonic() + 5)
        assert error.value.code == "runtime_endpoint_collision"
        assert proof_snapshot(layout.runtime_dir.parent) == before


@pytest.mark.parametrize("failure", ["write_enospc", "fsync_eio", "target_race"])
def test_failed_publication_never_overwrites_foreign_receipt_or_grants_readiness(
    layout: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    store = RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    original_link = os.link
    with owner, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        token = store.credential(owner)
        endpoint.bind(str(layout.socket))
        layout.socket.chmod(0o600)

        def broken_write(fd: int, value: bytes) -> int:
            raise OSError(errno.ENOSPC, "SYNTHETIC_DISK_FULL")

        def broken_sync(fd: int) -> None:
            raise OSError(errno.EIO, "SYNTHETIC_SYNC_IO")

        def raced_link(src: str, dst: str, **kwargs: object) -> None:
            layout.endpoint_receipt.write_bytes(b"FOREIGN_RECEIPT")
            layout.endpoint_receipt.chmod(0o600)
            original_link(src, dst, **kwargs)  # type: ignore[arg-type]

        with monkeypatch.context() as patch:
            if failure == "write_enospc":
                patch.setattr(os, "write", broken_write)
            elif failure == "fsync_eio":
                patch.setattr(os, "fsync", broken_sync)
            else:
                patch.setattr(os, "link", raced_link)
            with pytest.raises(ControlFault):
                owner.publish_endpoint(uuid4(), token)
        assert layout.socket.exists() and layout.lock.exists()
        if failure == "target_race":
            assert layout.endpoint_receipt.read_bytes() == b"FOREIGN_RECEIPT"
        else:
            assert not layout.endpoint_receipt.exists()
        assert not list(layout.runtime_dir.glob(".endpoint-*"))


@pytest.mark.parametrize("after_socket", [False, True])
def test_observed_receipt_replacement_is_not_deleted_during_preparation(
    layout: RuntimeLayout,
    recorded: tuple[Ownership, bytes],
    monkeypatch: pytest.MonkeyPatch,
    after_socket: bool,
) -> None:
    owner, _ = recorded
    original_stat, original_unlink = os.stat, os.unlink
    count = 0
    changed = False
    expected: dict[str, tuple[int, int, int, int, str | None]] = {}

    def replace() -> None:
        nonlocal changed, expected
        layout.endpoint_receipt.rename(layout.runtime_dir / "saved-proof")
        layout.endpoint_receipt.write_bytes(b"FOREIGN_RECEIPT")
        layout.endpoint_receipt.chmod(0o600)
        changed = True
        expected = proof_snapshot(layout.runtime_dir.parent)

    def checked_stat(path: str, **kwargs: object) -> os.stat_result:
        nonlocal count
        if path == "endpoint.json":
            count += 1
            if not after_socket and count == 3:
                replace()
        return original_stat(path, **kwargs)  # type: ignore[arg-type]

    def checked_unlink(path: str, **kwargs: object) -> None:
        original_unlink(path, **kwargs)  # type: ignore[arg-type]
        if path == "daemon.sock" and after_socket:
            replace()

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", checked_stat)
        patch.setattr(os, "unlink", checked_unlink)
        with pytest.raises(ControlFault):
            owner.prepare_endpoint(time.monotonic() + 5)
    assert changed and proof_snapshot(layout.runtime_dir.parent) == expected
    assert layout.socket.exists() is not after_socket


def test_normal_cleanup_refuses_a_replaced_published_receipt(layout: RuntimeLayout) -> None:
    store = RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    with owner, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        token = store.credential(owner)
        endpoint.bind(str(layout.socket))
        layout.socket.chmod(0o600)
        identity = owner.endpoint_identity()
        receipt = owner.publish_endpoint(uuid4(), token)
        layout.endpoint_receipt.rename(layout.runtime_dir / "original-proof")
        layout.endpoint_receipt.write_bytes(b"FOREIGN_RECEIPT")
        layout.endpoint_receipt.chmod(0o600)
        before = proof_snapshot(layout.runtime_dir.parent)
        with pytest.raises(ControlFault):
            owner.remove_endpoint(identity, receipt=receipt, token=token)
        assert proof_snapshot(layout.runtime_dir.parent) == before


def test_observed_socket_replacement_after_probe_is_preserved(
    layout: RuntimeLayout,
    recorded: tuple[Ownership, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, _ = recorded
    original = socket.socket.connect
    changed = False

    def connect(self: socket.socket, path: str) -> None:
        nonlocal changed
        try:
            original(self, path)
        except OSError:
            layout.socket.rename(layout.runtime_dir / "saved-socket")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as replacement:
                replacement.bind(str(layout.socket))
                layout.socket.chmod(0o600)
            changed = True
            raise

    with monkeypatch.context() as patch:
        patch.setattr(socket.socket, "connect", connect)
        with pytest.raises(ControlFault):
            owner.prepare_endpoint(time.monotonic() + 5)
    assert changed and layout.socket.exists() and layout.endpoint_receipt.exists()
    assert (layout.runtime_dir / "saved-socket").exists()


def test_published_receipt_birth_matches_compiled_sdk_oracle(
    layout: RuntimeLayout,
    recorded: tuple[Ownership, bytes],
) -> None:
    oracle = os.environ.get("JEFF_TEST_STAT_ORACLE")
    if oracle is None:
        pytest.skip("Compiled SDK oracle required; mandatory in guarded acceptance")
    result = subprocess.run([oracle, str(layout.endpoint_receipt)], capture_output=True, timeout=5)
    assert result.returncode == 0
    expected = json.loads(result.stdout)
    actual = json.loads(layout.endpoint_receipt.read_bytes())["bindings"]["receipt"]
    assert actual["birth_ns"] == expected["birth_s"] * 1000000000 + expected["birth_ns"]
    assert actual["device"] == expected["dev"] and actual["inode"] == expected["ino"]


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

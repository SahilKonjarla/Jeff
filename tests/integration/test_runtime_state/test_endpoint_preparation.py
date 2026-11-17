"""Ambiguous proofs, absent authority, active peers and replacement races fail closed."""

import json
import os
import socket
import time
from uuid import uuid4

import pytest

from jeff.application.runtime import ControlFault, RuntimeLayout
from jeff.infrastructure.runtime_state import Ownership, RuntimeStateStore

from .support_proof import proof_snapshot


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

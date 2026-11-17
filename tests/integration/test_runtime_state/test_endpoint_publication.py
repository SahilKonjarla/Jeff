"""Bounded private proof publication, proven residue retirement and no-clobber failures."""

import errno
import os
import socket
import time
from uuid import uuid4

import pytest

from jeff.application.runtime import ControlFault, RuntimeLayout
from jeff.infrastructure.runtime_state import RuntimeStateStore

from .support_proof import proof_snapshot


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

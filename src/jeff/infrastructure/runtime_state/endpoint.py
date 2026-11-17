"""Signed endpoint publication and exact-owned recovery/removal without adoption."""

import errno
import hmac
import json
import os
import secrets
import socket
import stat
import time
from contextlib import ExitStack
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import ValidationError

from jeff.application.runtime import ControlFault

from .identity import _FILE_FLAGS, _diagnostic_signature, _identity, _native_identity, _private_file
from .proofs import (
    _EndpointBindings,
    _EndpointReceipt,
    _FileIdentity,
    _receipt_entry,
    _receipt_pairs,
    _receipt_tag,
)

if TYPE_CHECKING:
    from .ownership import Ownership


def require_no_endpoint(self: "Ownership") -> None:
    self.check()
    try:
        if _receipt_entry(self.directory_fd, self.store.layout.uid) is not None:
            raise ControlFault("runtime_endpoint_collision")
        os.stat("daemon.sock", dir_fd=self.directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise ControlFault("runtime_inspection_failed") from None
    raise ControlFault("runtime_endpoint_collision")


def endpoint_identity(self: "Ownership") -> tuple[int, int]:
    self.check()
    try:
        info = os.stat("daemon.sock", dir_fd=self.directory_fd, follow_symlinks=False)
        if not (
            stat.S_ISSOCK(info.st_mode)
            and info.st_uid == self.store.layout.uid
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
        ):
            raise ControlFault("unsafe_runtime_path")
        return info.st_dev, info.st_ino
    except OSError:
        raise ControlFault("runtime_ownership_unconfirmed") from None


def _endpoint_bindings(
    self: "Ownership", receipt_fd: int, socket_identity: _FileIdentity | None = None
) -> _EndpointBindings:
    self.check()
    with self.store.directory(self.store.layout.data_dir, create=False) as data:
        key = os.stat("ipc.key", dir_fd=data, follow_symlinks=False)
        if not _private_file(key, self.store.layout.uid, 32):
            raise ControlFault("runtime_credentials_invalid")
        return _EndpointBindings(
            runtime=_native_identity(self.directory_fd),
            data=_native_identity(data),
            lock=_native_identity(self.fd),
            key=_native_identity(data, "ipc.key"),
            socket=socket_identity or _native_identity(self.directory_fd, "daemon.sock"),
            receipt=_native_identity(receipt_fd),
        )


def publish_endpoint(
    self: "Ownership", instance_id: UUID, token: bytes, *, endpoint: tuple[int, int] | None = None
) -> _EndpointReceipt:
    """Publish a signed actual binding without overwriting any existing receipt."""
    self.check()
    if _receipt_entry(self.directory_fd, self.store.layout.uid) is not None:
        raise ControlFault("runtime_endpoint_collision")
    if not hmac.compare_digest(token, self.store.read_credential()):
        raise ControlFault("runtime_credentials_invalid")
    expected = endpoint or self.endpoint_identity()
    temporary = ".endpoint-" + secrets.token_hex(16)
    fd = os.open(
        temporary,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
        0o600,
        dir_fd=self.directory_fd,
    )
    original = os.fstat(fd)
    try:
        bindings = self._endpoint_bindings(fd)
        if (bindings.socket.device, bindings.socket.inode) != expected:
            raise ControlFault("runtime_ownership_unconfirmed")
        receipt = _EndpointReceipt(
            version=1,
            instance_id=str(instance_id),
            nonce=secrets.token_hex(16),
            bindings=bindings,
            mac="0" * 64,
        )
        receipt = receipt.model_copy(update={"mac": _receipt_tag(receipt, token)})
        payload = json.dumps(
            receipt.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        if not 0 < len(payload) <= 2048:
            raise ControlFault("runtime_ownership_unconfirmed")
        offset = 0
        while offset < len(payload):
            count = os.write(fd, payload[offset:])
            if count <= 0:
                raise ControlFault("runtime_inspection_failed")
            offset += count
        os.fsync(fd)
        self.check()
        if self._endpoint_bindings(fd) != bindings:
            raise ControlFault("runtime_ownership_unconfirmed")
        # link is atomic/no-clobber; nlink2 is never declared ready or recoverable.
        os.link(
            temporary,
            "endpoint.json",
            src_dir_fd=self.directory_fd,
            dst_dir_fd=self.directory_fd,
            follow_symlinks=False,
        )
        if _identity(
            os.stat(temporary, dir_fd=self.directory_fd, follow_symlinks=False)
        ) != _identity(original):
            raise ControlFault("runtime_ownership_unconfirmed")
        os.unlink(temporary, dir_fd=self.directory_fd)
        os.fsync(self.directory_fd)
        if self._read_endpoint_receipt(fd, token) != receipt:
            raise ControlFault("runtime_ownership_unconfirmed")
        return receipt
    except OSError:
        raise ControlFault("runtime_inspection_failed") from None
    finally:
        try:
            current = os.stat(temporary, dir_fd=self.directory_fd, follow_symlinks=False)
            if _identity(current) == _identity(original):
                os.unlink(temporary, dir_fd=self.directory_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(fd)


def _read_endpoint_receipt(self: "Ownership", fd: int, token: bytes) -> _EndpointReceipt:
    self.check()
    if not hmac.compare_digest(token, self.store.read_credential()):
        raise ControlFault("runtime_credentials_invalid")
    before = os.fstat(fd)
    entry = _receipt_entry(self.directory_fd, self.store.layout.uid)
    if entry is None or _diagnostic_signature(before) != _diagnostic_signature(entry):
        raise ControlFault("runtime_ownership_unconfirmed")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        payload = os.read(fd, 2049)
        if len(payload) != before.st_size:
            raise ControlFault("runtime_ownership_unconfirmed")
        receipt = _EndpointReceipt.model_validate(
            json.loads(payload, object_pairs_hook=_receipt_pairs)
        )
        canonical = json.dumps(
            receipt.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        if payload != canonical or not hmac.compare_digest(
            receipt.mac, _receipt_tag(receipt, token)
        ):
            raise ControlFault("runtime_ownership_unconfirmed")
    except (ValueError, ValidationError, RecursionError):
        raise ControlFault("runtime_ownership_unconfirmed") from None
    if _diagnostic_signature(os.fstat(fd)) != _diagnostic_signature(before):
        raise ControlFault("runtime_ownership_unconfirmed")
    return receipt


def prepare_endpoint(self: "Ownership", deadline: float) -> None:
    """Explicit start only: retire proven stale entries under this exclusive owner."""
    self.check()
    if time.monotonic() >= deadline:
        raise ControlFault("runtime_start_timeout")
    receipt_entry = _receipt_entry(self.directory_fd, self.store.layout.uid)
    try:
        os.stat("daemon.sock", dir_fd=self.directory_fd, follow_symlinks=False)
        has_socket = True
    except FileNotFoundError:
        has_socket = False
    if receipt_entry is None:
        if has_socket:
            raise ControlFault("runtime_endpoint_collision")
        return
    token = self.store.read_credential()  # never create/rekey when residue exists
    fd = os.open("endpoint.json", os.O_RDONLY | _FILE_FLAGS, dir_fd=self.directory_fd)
    try:
        receipt = self._read_endpoint_receipt(fd, token)
        expected = self._endpoint_bindings(fd, None if has_socket else receipt.bindings.socket)
        if expected != receipt.bindings:
            raise ControlFault("runtime_ownership_unconfirmed")
        if has_socket:
            self.endpoint_identity()  # enforce type/mode/UID/link policy, not just identity
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlFault("runtime_start_timeout")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(min(0.25, remaining))
                try:
                    probe.connect(str(self.store.layout.socket))
                except OSError as error:
                    if error.errno != errno.ECONNREFUSED:
                        raise ControlFault("runtime_ownership_unconfirmed") from None
                else:
                    raise ControlFault("runtime_endpoint_collision")
            self.check()
            if (
                self._read_endpoint_receipt(fd, token) != receipt
                or self._endpoint_bindings(fd) != expected
            ):
                raise ControlFault("runtime_ownership_unconfirmed")
            self.endpoint_identity()
            if time.monotonic() >= deadline:
                raise ControlFault("runtime_start_timeout")
            os.unlink("daemon.sock", dir_fd=self.directory_fd)
            os.fsync(self.directory_fd)
        self.check()
        try:
            os.stat("daemon.sock", dir_fd=self.directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ControlFault("runtime_endpoint_collision")
        if (
            self._read_endpoint_receipt(fd, token) != receipt
            or self._endpoint_bindings(fd, receipt.bindings.socket) != expected
        ):
            raise ControlFault("runtime_ownership_unconfirmed")
        if time.monotonic() >= deadline:
            raise ControlFault("runtime_start_timeout")
        os.unlink("endpoint.json", dir_fd=self.directory_fd)
        os.fsync(self.directory_fd)
        self.require_no_endpoint()
    except OSError:
        raise ControlFault("runtime_ownership_unconfirmed") from None
    finally:
        os.close(fd)


def remove_endpoint(
    self: "Ownership",
    identity: tuple[int, int],
    *,
    receipt: _EndpointReceipt | None = None,
    token: bytes | None = None,
) -> None:
    """Current binding authority only; a published receipt must still match exactly."""
    self.check()
    try:
        if self.endpoint_identity() != identity:
            raise ControlFault("runtime_ownership_unconfirmed")
        with ExitStack() as stack:
            proof_fd = -1
            if receipt is not None:
                if token is None:
                    raise ControlFault("runtime_credentials_invalid")
                proof_fd = os.open(
                    "endpoint.json", os.O_RDONLY | _FILE_FLAGS, dir_fd=self.directory_fd
                )
                stack.callback(os.close, proof_fd)
                if (
                    self._read_endpoint_receipt(proof_fd, token) != receipt
                    or self._endpoint_bindings(proof_fd) != receipt.bindings
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
            self.check()
            if self.endpoint_identity() != identity:
                raise ControlFault("runtime_ownership_unconfirmed")
            os.unlink("daemon.sock", dir_fd=self.directory_fd)
            if receipt is not None:
                assert token is not None
                if (
                    self._read_endpoint_receipt(proof_fd, token) != receipt
                    or self._endpoint_bindings(proof_fd, receipt.bindings.socket)
                    != receipt.bindings
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
                os.unlink("endpoint.json", dir_fd=self.directory_fd)
                os.fsync(self.directory_fd)
    except OSError:
        raise ControlFault("runtime_ownership_unconfirmed") from None

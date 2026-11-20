"""Descriptor-bound journal permits; only SQLite performs journal recovery."""

import hmac
import json
import os
import secrets
from typing import TYPE_CHECKING

from pydantic import ValidationError

from jeff.application.runtime import ControlFault

from .identity import _FILE_FLAGS, _diagnostic_signature, _identity, _native_identity, _private_file
from .proofs import (
    _FileIdentity,
    _receipt_pairs,
    _recovery_entry,
    _recovery_tag,
    _RecoveryBindings,
    _RecoveryPermit,
)

if TYPE_CHECKING:
    from .ownership import Ownership


def _storage_file(self: "Ownership", root: int, name: str, descriptor: int) -> _FileIdentity:
    held = os.fstat(descriptor)
    current = os.stat(name, dir_fd=root, follow_symlinks=False)
    if not (
        _private_file(held, self.store.layout.uid, held.st_size)
        and _private_file(current, self.store.layout.uid, held.st_size)
        and _identity(held) == _identity(current)
    ):
        raise ControlFault("runtime_ownership_unconfirmed")
    return _native_identity(descriptor)


def _storage_bindings(
    self: "Ownership", root: int, database_fd: int, journal: _FileIdentity, permit_fd: int
) -> _RecoveryBindings:
    self.check()
    key = os.stat("ipc.key", dir_fd=root, follow_symlinks=False)
    if not _private_file(key, self.store.layout.uid, 32):
        raise ControlFault("runtime_credentials_invalid")
    return _RecoveryBindings(
        runtime=_native_identity(self.directory_fd),
        data=_native_identity(root),
        lock=_native_identity(self.fd),
        key=_native_identity(root, "ipc.key"),
        database=self._storage_file(root, "state.sqlite3", database_fd),
        journal=journal,
        permit=_native_identity(permit_fd),
    )


def _read_storage_permit(self: "Ownership", root: int, fd: int, token: bytes) -> _RecoveryPermit:
    self.check()
    before = os.fstat(fd)
    entry = _recovery_entry(root, self.store.layout.uid)
    if entry is None or _diagnostic_signature(before) != _diagnostic_signature(entry):
        raise ControlFault("runtime_ownership_unconfirmed")
    os.lseek(fd, 0, os.SEEK_SET)
    payload = os.read(fd, 2049)
    try:
        permit = _RecoveryPermit.model_validate(
            json.loads(payload, object_pairs_hook=_receipt_pairs)
        )
        canonical = json.dumps(
            permit.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        if (
            len(payload) != before.st_size
            or payload != canonical
            or not hmac.compare_digest(permit.mac, _recovery_tag(permit, token))
            or permit.page_size & (permit.page_size - 1)
        ):
            raise ControlFault("runtime_ownership_unconfirmed")
    except (ValueError, ValidationError, RecursionError):
        raise ControlFault("runtime_ownership_unconfirmed") from None
    if _diagnostic_signature(os.fstat(fd)) != _diagnostic_signature(before):
        raise ControlFault("runtime_ownership_unconfirmed")
    return permit


def arm_storage_recovery(
    self: "Ownership",
    database_fd: int,
    journal_fd: int,
    *,
    page_size: int,
    original_pages: int,
    schema_version: int = 4,
) -> _RecoveryPermit:
    """Bind the actual SQLite-created journal before any business DML; never create it."""
    self.check()
    if type(schema_version) is not int or not 4 <= schema_version <= 11:
        raise ControlFault("runtime_ownership_unconfirmed")
    token = self.store.read_credential()
    with self.store.directory(self.store.layout.data_dir, create=False) as root:
        if _recovery_entry(root, self.store.layout.uid) is not None:
            raise ControlFault("runtime_endpoint_collision")
        journal = self._storage_file(root, "state.sqlite3-journal", journal_fd)
        temporary = ".recovery-" + secrets.token_hex(16)
        fd = os.open(
            temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_FLAGS, 0o600, dir_fd=root
        )
        original = os.fstat(fd)
        try:
            bindings = self._storage_bindings(root, database_fd, journal, fd)
            permit = _RecoveryPermit(
                version=1,
                schema_version=schema_version,
                nonce=secrets.token_hex(16),
                page_size=page_size,
                original_pages=original_pages,
                bindings=bindings,
                mac="0" * 64,
            )
            if page_size & (page_size - 1):
                raise ControlFault("runtime_ownership_unconfirmed")
            permit = permit.model_copy(update={"mac": _recovery_tag(permit, token)})
            payload = json.dumps(
                permit.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
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
            if (
                not hmac.compare_digest(token, self.store.read_credential())
                or self._storage_file(root, "state.sqlite3-journal", journal_fd) != journal
                or self._storage_bindings(root, database_fd, journal, fd) != bindings
            ):
                raise ControlFault("runtime_ownership_unconfirmed")
            os.link(
                temporary,
                "recovery.json",
                src_dir_fd=root,
                dst_dir_fd=root,
                follow_symlinks=False,
            )
            if _identity(os.stat(temporary, dir_fd=root, follow_symlinks=False)) != _identity(
                original
            ):
                raise ControlFault("runtime_ownership_unconfirmed")
            os.unlink(temporary, dir_fd=root)
            os.fsync(root)
            if self._read_storage_permit(root, fd, token) != permit:
                raise ControlFault("runtime_ownership_unconfirmed")
            return permit
        finally:
            try:
                if _identity(os.stat(temporary, dir_fd=root, follow_symlinks=False)) == _identity(
                    original
                ):
                    os.unlink(temporary, dir_fd=root)
            except FileNotFoundError:
                pass
            finally:
                os.close(fd)


def verify_storage_recovery(
    self: "Ownership", database_fd: int, journal_fd: int
) -> _RecoveryPermit:
    """Authenticate exact namespace/file bindings; leave journal recovery to SQLite."""
    self.check()
    token = self.store.read_credential()
    with self.store.directory(self.store.layout.data_dir, create=False) as root:
        fd = os.open("recovery.json", os.O_RDONLY | _FILE_FLAGS, dir_fd=root)
        try:
            permit = self._read_storage_permit(root, fd, token)
            journal = self._storage_file(root, "state.sqlite3-journal", journal_fd)
            if self._storage_bindings(root, database_fd, journal, fd) != permit.bindings:
                raise ControlFault("runtime_ownership_unconfirmed")
            if not hmac.compare_digest(token, self.store.read_credential()):
                raise ControlFault("runtime_credentials_invalid")
            return permit
        finally:
            os.close(fd)


def retire_storage_recovery(self: "Ownership", database_fd: int) -> None:
    """Retire only matching self-bound permission after SQLite removed its journal."""
    self.check()
    with self.store.directory(self.store.layout.data_dir, create=False) as root:
        if _recovery_entry(root, self.store.layout.uid) is None:
            return
        try:
            os.stat("state.sqlite3-journal", dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ControlFault("runtime_ownership_unconfirmed")
        token = self.store.read_credential()
        fd = os.open("recovery.json", os.O_RDONLY | _FILE_FLAGS, dir_fd=root)
        try:
            permit = self._read_storage_permit(root, fd, token)
            if (
                self._storage_bindings(root, database_fd, permit.bindings.journal, fd)
                != permit.bindings
            ):
                raise ControlFault("runtime_ownership_unconfirmed")
            if not hmac.compare_digest(token, self.store.read_credential()):
                raise ControlFault("runtime_credentials_invalid")
            self.check()
            os.unlink("recovery.json", dir_fd=root)
            os.fsync(root)
        finally:
            os.close(fd)

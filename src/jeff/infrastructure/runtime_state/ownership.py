"""One acquired descriptor lifetime, exact ownership checks and bounded execution homes."""

import math
import os
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Self
from uuid import UUID

from jeff.application.execution import DockerBackendFault
from jeff.application.runtime import ControlFault

from .. import runtime_state as _state
from . import endpoint as _endpoint
from . import storage_recovery
from .identity import _FILE_FLAGS, _identity, _private_file
from .proofs import (
    _EndpointBindings,
    _EndpointReceipt,
    _FileIdentity,
    _RecoveryBindings,
    _RecoveryPermit,
)


class Ownership:
    """An actual acquired descriptor; close duplicates, NEVER unlock the shared description."""

    def __init__(self, store: "_state.RuntimeStateStore", fd: int, directory_fd: int) -> None:
        self.store = store
        self.fd = fd
        self.directory_fd = directory_fd
        self._execution_homes_lock = threading.Lock()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close this copy only; LOCK_UN would also release inherited live copies."""
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.directory_fd >= 0:
            os.close(self.directory_fd)
            self.directory_fd = -1

    def check(self) -> None:
        """Revalidate that the captured descriptors still name the selected stable anchor."""
        try:
            with self.store.directory(self.store.layout.runtime_dir, create=False) as root:
                if _identity(os.fstat(root)) != _identity(os.fstat(self.directory_fd)):
                    raise ControlFault("runtime_ownership_unconfirmed")
                descriptor = os.fstat(self.fd)
                entry = os.stat("daemon.lock", dir_fd=root, follow_symlinks=False)
                if not _private_file(descriptor, self.store.layout.uid, 0) or (
                    _identity(descriptor) != _identity(entry) or entry.st_nlink != 1
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
        except OSError:
            raise ControlFault("runtime_ownership_unconfirmed") from None

    @contextmanager
    def execution_homes(self, instance_id: UUID, *, deadline: float) -> Iterator[tuple[Path, Path]]:
        """New bounded private generation only; paths are facts, never launch authority."""
        if not isinstance(instance_id, UUID):
            raise DockerBackendFault("docker_unsafe_configuration")
        if not self._execution_homes_lock.acquire(blocking=False):
            raise DockerBackendFault("docker_busy")

        def budget() -> None:
            if not math.isfinite(deadline) or time.monotonic() >= deadline:
                raise DockerBackendFault("docker_timeout")

        def stable(parent: int, name: str, fd: int) -> None:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                _identity(current) != _identity(os.fstat(fd))
                or stat.S_IMODE(current.st_mode) != 0o700
                or current.st_uid != self.store.layout.uid
            ):
                raise DockerBackendFault("docker_unsafe_configuration")

        try:
            budget()
            self.check()
            with ExitStack() as stack:

                def child(parent: int, name: str, *, existing: bool = False) -> int:
                    budget()
                    created = False
                    try:
                        os.mkdir(name, 0o700, dir_fd=parent)
                        created = True
                    except FileExistsError:
                        if not existing:
                            raise DockerBackendFault("docker_unsafe_configuration") from None
                    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    mode = stat.S_IMODE(before.st_mode)
                    if (
                        not stat.S_ISDIR(before.st_mode)
                        or before.st_uid != self.store.layout.uid
                        or (mode & ~0o700 if created else mode != 0o700)
                    ):
                        raise DockerBackendFault("docker_unsafe_configuration")
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | _FILE_FLAGS, dir_fd=parent)
                    stack.callback(os.close, fd)
                    if _identity(before) != _identity(os.fstat(fd)):
                        raise DockerBackendFault("docker_unsafe_configuration")
                    if created:
                        os.fchmod(fd, 0o700)
                    stable(parent, name, fd)
                    stack.callback(stable, parent, name, fd)
                    if created:
                        os.fsync(fd)
                        os.fsync(parent)
                    return fd

                base = child(self.directory_fd, "execution-backend", existing=True)
                count = 0
                with os.scandir(base) as entries:
                    for entry in entries:
                        budget()
                        count += 1
                        if count > 16:
                            raise DockerBackendFault("docker_home_capacity")
                        try:
                            valid_name = str(UUID(entry.name)) == entry.name
                        except ValueError:
                            valid_name = False
                        info = entry.stat(follow_symlinks=False)
                        if (
                            not valid_name
                            or not stat.S_ISDIR(info.st_mode)
                            or info.st_uid != self.store.layout.uid
                            or stat.S_IMODE(info.st_mode) != 0o700
                        ):
                            raise DockerBackendFault("docker_unsafe_configuration")
                if count >= 16:
                    raise DockerBackendFault("docker_home_capacity")
                generation = child(base, str(instance_id))
                child(generation, "metadata")
                child(generation, "lifecycle")
                budget()
                self.check()
                path = self.store.layout.runtime_dir / "execution-backend" / str(instance_id)
                yield path / "metadata", path / "lifecycle"
                budget()
                self.check()
        except OSError:
            raise DockerBackendFault("docker_unsafe_configuration") from None
        finally:
            self._execution_homes_lock.release()

    def _storage_file(self, root: int, name: str, descriptor: int) -> _FileIdentity:
        return storage_recovery._storage_file(self, root, name, descriptor)

    def _storage_bindings(
        self, root: int, database_fd: int, journal: _FileIdentity, permit_fd: int
    ) -> _RecoveryBindings:
        return storage_recovery._storage_bindings(self, root, database_fd, journal, permit_fd)

    def _read_storage_permit(self, root: int, fd: int, token: bytes) -> _RecoveryPermit:
        return storage_recovery._read_storage_permit(self, root, fd, token)

    def arm_storage_recovery(
        self,
        database_fd: int,
        journal_fd: int,
        *,
        page_size: int,
        original_pages: int,
        schema_version: int = 4,
    ) -> _RecoveryPermit:
        """Bind the actual SQLite-created journal before any business DML; never create it."""
        return storage_recovery.arm_storage_recovery(
            self,
            database_fd,
            journal_fd,
            page_size=page_size,
            original_pages=original_pages,
            schema_version=schema_version,
        )

    def verify_storage_recovery(self, database_fd: int, journal_fd: int) -> _RecoveryPermit:
        """Authenticate exact namespace/file bindings; leave journal recovery to SQLite."""
        return storage_recovery.verify_storage_recovery(self, database_fd, journal_fd)

    def retire_storage_recovery(self, database_fd: int) -> None:
        """Retire only matching self-bound permission after SQLite removed its journal."""
        return storage_recovery.retire_storage_recovery(self, database_fd)

    def require_no_endpoint(self) -> None:
        return _endpoint.require_no_endpoint(self)

    def endpoint_identity(self) -> tuple[int, int]:
        return _endpoint.endpoint_identity(self)

    def _endpoint_bindings(
        self, receipt_fd: int, socket_identity: _FileIdentity | None = None
    ) -> _EndpointBindings:
        return _endpoint._endpoint_bindings(self, receipt_fd, socket_identity)

    def publish_endpoint(
        self, instance_id: UUID, token: bytes, *, endpoint: tuple[int, int] | None = None
    ) -> _EndpointReceipt:
        """Publish a signed actual binding without overwriting any existing receipt."""
        return _endpoint.publish_endpoint(self, instance_id, token, endpoint=endpoint)

    def _read_endpoint_receipt(self, fd: int, token: bytes) -> _EndpointReceipt:
        return _endpoint._read_endpoint_receipt(self, fd, token)

    def prepare_endpoint(self, deadline: float) -> None:
        """Explicit start only: retire proven stale entries under this exclusive owner."""
        return _endpoint.prepare_endpoint(self, deadline)

    def remove_endpoint(
        self,
        identity: tuple[int, int],
        *,
        receipt: _EndpointReceipt | None = None,
        token: bytes | None = None,
    ) -> None:
        """Current binding authority only; a published receipt must still match exactly."""
        return _endpoint.remove_endpoint(self, identity, receipt=receipt, token=token)

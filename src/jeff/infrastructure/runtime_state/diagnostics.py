"""Read-only held/free observations and continuously pinned shared diagnostic leases."""

import fcntl
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from jeff.application.diagnostics import DiagnosticDeadlineExceeded
from jeff.application.runtime import ControlFault, RuntimeLayout

from .. import runtime_state as _state
from .identity import _FILE_FLAGS, _diagnostic_signature, _identity, _private_file
from .proofs import _receipt_entry, _recovery_entry


class DiagnosticLease:
    """Read-only shared lock, fixed-file reads and revalidation; no mutable Ownership."""

    def __init__(
        self, store: "_state.RuntimeStateStore", fd: int, root: int, deadline: float
    ) -> None:
        self._store = store
        self._fd = fd
        self._root = root
        self._deadline = deadline
        self._data_identity: tuple[int, ...] | None = None
        self._observed: dict[Literal["settings", "database"], tuple[int, ...]] = {}

    @property
    def layout(self) -> RuntimeLayout:
        return self._store.layout

    def check(self) -> None:
        if time.monotonic() >= self._deadline:
            raise DiagnosticDeadlineExceeded
        if self._fd < 0:
            raise ControlFault("runtime_ownership_unconfirmed")
        try:
            with self._store.directory(self.layout.runtime_dir, create=False) as root:
                if _identity(os.fstat(root)) != _identity(os.fstat(self._root)):
                    raise ControlFault("runtime_ownership_unconfirmed")
                held = os.fstat(self._fd)
                current = os.stat("daemon.lock", dir_fd=root, follow_symlinks=False)
                if not (
                    _private_file(held, self.layout.uid, 0)
                    and _private_file(current, self.layout.uid, 0)
                    and _identity(held) == _identity(current)
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
                if _receipt_entry(root, self.layout.uid) is not None:
                    raise ControlFault("runtime_endpoint_collision")
                try:
                    os.stat("daemon.sock", dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ControlFault("runtime_endpoint_collision")
            if self._data_identity is not None:
                with self._store.directory(self.layout.data_dir, create=False) as root:
                    if _identity(os.fstat(root)) != self._data_identity:
                        raise ControlFault("runtime_ownership_unconfirmed")
                    if _recovery_entry(root, self.layout.uid) is not None:
                        raise ControlFault("runtime_ownership_unconfirmed")
                    credential = os.stat("ipc.key", dir_fd=root, follow_symlinks=False)
                    if not _private_file(credential, self.layout.uid, 32):
                        raise ControlFault("runtime_credentials_invalid")
                    for role, before in self._observed.items():
                        name = "settings.yaml" if role == "settings" else "state.sqlite3"
                        after = os.stat(name, dir_fd=root, follow_symlinks=False)
                        if before != _diagnostic_signature(after):
                            raise ControlFault("runtime_ownership_unconfirmed")
        except OSError:
            raise ControlFault("runtime_ownership_unconfirmed") from None
        if time.monotonic() >= self._deadline:
            raise DiagnosticDeadlineExceeded

    @contextmanager
    def data_directory(self) -> Iterator[int]:
        self.check()
        with self._store.directory(self.layout.data_dir, create=False) as root:
            identity = _identity(os.fstat(root))
            if self._data_identity is not None and self._data_identity != identity:
                raise ControlFault("runtime_ownership_unconfirmed")
            self._data_identity = identity
            self.check()
            yield root
            self.check()

    @contextmanager
    def open_file(self, role: Literal["settings", "database"]) -> Iterator[int]:
        """Only declared files, O_RDONLY; observed identities remain checked until lease exit."""
        self.check()
        if role not in ("settings", "database"):
            raise ControlFault("invalid_runtime_path")
        name = "settings.yaml" if role == "settings" else "state.sqlite3"
        try:
            with self._store.directory(self.layout.data_dir, create=False) as root:
                identity = _identity(os.fstat(root))
                if self._data_identity is not None and self._data_identity != identity:
                    raise ControlFault("runtime_ownership_unconfirmed")
                self._data_identity = identity
                self.check()
                fd = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=root)
                try:
                    before = os.fstat(fd)
                    if not (
                        stat.S_ISREG(before.st_mode)
                        and before.st_uid == self.layout.uid
                        and stat.S_IMODE(before.st_mode) == 0o600
                        and before.st_nlink == 1
                    ):
                        raise ControlFault("unsafe_runtime_path")
                    signature = _diagnostic_signature(before)
                    if role in self._observed and self._observed[role] != signature:
                        raise ControlFault("runtime_ownership_unconfirmed")
                    self._observed[role] = signature
                    self.check()
                    yield fd
                    if _diagnostic_signature(os.fstat(fd)) != signature:
                        raise ControlFault("runtime_ownership_unconfirmed")
                    self.check()
                finally:
                    os.close(fd)
        except OSError:
            raise ControlFault("runtime_inspection_failed") from None


class DiagnosticObservation:
    """Pinned existing lock namespace; free holds SH, held never impersonates ownership."""

    def __init__(
        self, store: "_state.RuntimeStateStore", fd: int, root: int, deadline: float, held: bool
    ) -> None:
        self._store, self._fd, self._root = store, fd, root
        self._deadline, self._held = deadline, held
        self._issued_lease: DiagnosticLease | None = None
        self._endpoint = self._endpoint_identity()
        receipt = _receipt_entry(self._root, self._store.layout.uid)
        self._receipt = _diagnostic_signature(receipt) if receipt is not None else None

    @property
    def held(self) -> bool:
        return self._held

    @property
    def has_endpoint(self) -> bool:
        return self._endpoint is not None

    def _endpoint_identity(self) -> tuple[int, ...] | None:
        try:
            info = os.stat("daemon.sock", dir_fd=self._root, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not (
            stat.S_ISSOCK(info.st_mode)
            and info.st_uid == self._store.layout.uid
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
        ):
            raise ControlFault("unsafe_runtime_path")
        return _identity(info)

    def check(self) -> None:
        if time.monotonic() >= self._deadline:
            raise DiagnosticDeadlineExceeded
        if self._fd < 0:
            raise ControlFault("runtime_ownership_unconfirmed")
        try:
            with self._store.directory(self._store.layout.runtime_dir, create=False) as root:
                if _identity(os.fstat(root)) != _identity(os.fstat(self._root)):
                    raise ControlFault("runtime_ownership_unconfirmed")
                held = os.fstat(self._fd)
                current = os.stat("daemon.lock", dir_fd=root, follow_symlinks=False)
                if not (
                    _private_file(held, self._store.layout.uid, 0)
                    and _private_file(current, self._store.layout.uid, 0)
                    and _identity(held) == _identity(current)
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
                if self._endpoint_identity() != self._endpoint:
                    raise ControlFault("runtime_ownership_unconfirmed")
                receipt = _receipt_entry(self._root, self._store.layout.uid)
                signature = _diagnostic_signature(receipt) if receipt is not None else None
                if signature != self._receipt:
                    raise ControlFault("runtime_ownership_unconfirmed")
                if self.held:
                    try:
                        fcntl.flock(self._fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        # This description now holds SH; context close releases ONLY our copy.
                        raise ControlFault("runtime_ownership_unconfirmed")
        except OSError:
            raise ControlFault("runtime_ownership_unconfirmed") from None
        if time.monotonic() >= self._deadline:
            raise DiagnosticDeadlineExceeded

    def stopped_lease(self) -> DiagnosticLease:
        """Issue read-only authority only while the pinned lock is free and residue absent."""
        self.check()
        if self.held:
            raise ControlFault("runtime_ownership_unconfirmed")
        if self._endpoint is not None or self._receipt is not None:
            raise ControlFault("runtime_endpoint_collision")
        if self._issued_lease is None:
            self._issued_lease = DiagnosticLease(self._store, self._fd, self._root, self._deadline)
        return self._issued_lease

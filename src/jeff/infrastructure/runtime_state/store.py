"""Checked runtime namespace traversal, acquisition/adoption and existing credential policy."""

import fcntl
import os
import secrets
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import cast

from jeff.application.diagnostics import DiagnosticDeadlineExceeded
from jeff.application.projects import Failure
from jeff.application.runtime import ControlFault, ControlIssueCode, RuntimeLayout
from jeff.infrastructure.runtime_paths import inspect_layout

from .diagnostics import DiagnosticLease, DiagnosticObservation
from .identity import _FILE_FLAGS, _identity, _private_file, _stable
from .ownership import Ownership
from .proofs import _receipt_entry


class RuntimeStateStore:
    """Operate on one checked namespace; constructor metadata is not lifetime authority.

    Ownership, diagnostic leases and existing-key reads are separate capabilities.
    Missing state never authorizes reset, key rotation or a guessed-PID cleanup.
    """

    def __init__(self, layout: RuntimeLayout) -> None:
        if sys.platform != "darwin":
            raise ControlFault("unsupported_runtime_platform")
        if layout.uid != os.geteuid():
            raise ControlFault("unsafe_runtime_path")
        files = (
            layout.database,
            layout.settings,
            layout.credential,
            layout.socket,
            layout.lock,
            layout.endpoint_receipt,
            layout.recovery_permit,
        )
        if any(
            root.is_relative_to(file)
            for root in (layout.data_dir, layout.runtime_dir)
            for file in files
        ):
            raise ControlFault("invalid_runtime_path")
        report = inspect_layout(layout)
        if isinstance(report, Failure):
            raise ControlFault(cast(ControlIssueCode, report.error.code))
        if any(entry.state == "unsafe" for entry in report.entries):
            raise ControlFault("unsafe_runtime_path")
        if any(entry.state == "unknown" for entry in report.entries):
            raise ControlFault("runtime_inspection_failed")
        self.layout = layout

    @contextmanager
    def directory(self, path: Path, *, create: bool) -> Iterator[int]:
        """Operation-time no-follow directory traversal; no arbitrary link resolution."""
        if path not in (self.layout.data_dir, self.layout.runtime_dir):
            raise ControlFault("invalid_runtime_path")
        try:
            with ExitStack() as stack:
                flags = os.O_RDONLY | os.O_DIRECTORY | _FILE_FLAGS
                fd = os.open("/", flags)
                stack.callback(os.close, fd)
                chain: list[tuple[int, str, os.stat_result]] = []
                current = Path("/")
                for name in path.parts[1:]:
                    current /= name
                    try:
                        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        _stable(chain)
                        if not create:
                            raise ControlFault("runtime_absent") from None
                        try:
                            os.mkdir(name, mode=0o700, dir_fd=fd)
                        except FileExistsError:
                            pass
                        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if before.st_uid not in (0, self.layout.uid) or (
                        stat.S_IMODE(before.st_mode) & 0o022 and not before.st_mode & stat.S_ISVTX
                    ):
                        # Private leaves are not sufficient if another user can rename
                        # their ancestors and replace the lifetime-lock namespace.
                        raise ControlFault("unsafe_runtime_path")
                    if not stat.S_ISDIR(before.st_mode) or (
                        current in (self.layout.data_dir, self.layout.runtime_dir)
                        and (
                            before.st_uid != self.layout.uid
                            or stat.S_IMODE(before.st_mode) != 0o700
                        )
                    ):
                        raise ControlFault("unsafe_runtime_path")
                    next_fd = os.open(name, flags, dir_fd=fd)
                    stack.callback(os.close, next_fd)
                    if _identity(before) != _identity(os.fstat(next_fd)):
                        raise ControlFault("runtime_ownership_unconfirmed")
                    chain.append((fd, name, before))
                    fd = next_fd
                info = os.fstat(fd)
                if info.st_uid != self.layout.uid or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ControlFault("unsafe_runtime_path")
                _stable(chain)
                yield fd
                _stable(chain)
        except OSError:
            raise ControlFault("runtime_inspection_failed") from None

    @contextmanager
    def diagnostic_observation(self, deadline: float) -> Iterator[DiagnosticObservation]:
        """Non-creating O_RDONLY held/free observation; not mutable lifetime authority."""
        if time.monotonic() >= deadline:
            raise DiagnosticDeadlineExceeded
        with self.directory(self.layout.runtime_dir, create=False) as root:
            try:
                fd = os.open("daemon.lock", os.O_RDONLY | _FILE_FLAGS, dir_fd=root)
            except FileNotFoundError:
                raise ControlFault("runtime_absent") from None
            except OSError:
                raise ControlFault("runtime_inspection_failed") from None
            observation: DiagnosticObservation | None = None
            try:
                info = os.fstat(fd)
                if not _private_file(info, self.layout.uid, 0):
                    raise ControlFault("unsafe_runtime_path")
                held = False
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    held = True
                observation = DiagnosticObservation(self, fd, root, deadline, held)
                observation.check()
                yield observation
                observation.check()
            except OSError:
                raise ControlFault("runtime_inspection_failed") from None
            finally:
                if observation is not None:
                    observation._fd = -1
                    if observation._issued_lease is not None:
                        observation._issued_lease._fd = -1
                os.close(fd)

    @contextmanager
    def diagnostic_lease(self, deadline: float) -> Iterator[DiagnosticLease]:
        """Non-creating shared lease retained continuously through stopped reads."""
        with self.diagnostic_observation(deadline) as observation:
            lease = observation.stopped_lease()
            try:
                yield lease
                lease.check()
            finally:
                lease._fd = -1

    def acquire(self, *, create: bool) -> Ownership | None:
        """None means a real conflicting flock; absence is a separate safe fault."""
        with ExitStack() as failed_ownership:
            with self.directory(self.layout.runtime_dir, create=create) as root:
                fd = -1
                try:
                    try:
                        fd = os.open("daemon.lock", os.O_RDWR | _FILE_FLAGS, dir_fd=root)
                    except FileNotFoundError:
                        if _receipt_entry(root, self.layout.uid) is not None:
                            raise ControlFault("runtime_endpoint_collision") from None
                        try:
                            os.stat("daemon.sock", dir_fd=root, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            raise ControlFault("runtime_endpoint_collision") from None
                        if not create:
                            raise ControlFault("runtime_absent") from None
                        try:
                            fd = os.open(
                                "daemon.lock",
                                os.O_RDWR | _FILE_FLAGS | os.O_CREAT | os.O_EXCL,
                                0o600,
                                dir_fd=root,
                            )
                        except FileExistsError:
                            fd = os.open("daemon.lock", os.O_RDWR | _FILE_FLAGS, dir_fd=root)
                    info = os.fstat(fd)
                    if not _private_file(info, self.layout.uid, 0):
                        raise ControlFault("unsafe_runtime_path")
                    if _identity(info) != _identity(
                        os.stat("daemon.lock", dir_fd=root, follow_symlinks=False)
                    ):
                        raise ControlFault("runtime_ownership_unconfirmed")
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return None
                    owner = Ownership(self, fd, os.dup(root))
                    failed_ownership.callback(owner.close)
                    fd = -1
                    owner.check()
                except OSError:
                    raise ControlFault("runtime_inspection_failed") from None
                finally:
                    if fd >= 0:
                        os.close(fd)
            # Transfer only after directory-context exit revalidation also succeeds.
            failed_ownership.pop_all()
            return owner

    def adopt(self, fd: int) -> Ownership:
        """Validate the inherited live lock, without creating any state or acquiring a new owner."""
        owner: Ownership | None = None
        try:
            with self.directory(self.layout.runtime_dir, create=False) as root:
                info = os.fstat(fd)
                if not _private_file(info, self.layout.uid, 0):
                    raise ControlFault("runtime_ownership_unconfirmed")
                owner = Ownership(self, fd, os.dup(root))
                owner.check()
                probe = self.acquire(create=False)
                if probe is not None:
                    probe.close()
                    raise ControlFault("runtime_ownership_unconfirmed")
                # An independent owner must conflict; our inherited description must not.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return owner
        except (OSError, ControlFault):
            if owner is not None:
                owner.close()
            raise ControlFault("runtime_ownership_unconfirmed") from None

    def credential(self, owner: Ownership) -> bytes:
        """Create only an absent key under ownership; never replace invalid existing bytes."""
        if owner.store is not self:
            raise ControlFault("runtime_ownership_unconfirmed")
        owner.require_no_endpoint()
        with self.directory(self.layout.data_dir, create=True) as root:
            try:
                fd = os.open(
                    "ipc.key",
                    os.O_WRONLY | _FILE_FLAGS | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=root,
                )
            except FileExistsError:
                return self.read_credential()
            except OSError:
                raise ControlFault("runtime_credentials_invalid") from None
            try:
                value = secrets.token_bytes(32)
                offset = 0
                while offset < len(value):
                    written = os.write(fd, value[offset:])
                    if written <= 0:
                        raise ControlFault("runtime_credentials_invalid")
                    offset += written
                os.fsync(fd)
            except OSError:
                raise ControlFault("runtime_credentials_invalid") from None
            finally:
                os.close(fd)
        owner.check()
        return self.read_credential()

    def read_credential(self) -> bytes:
        """Read the existing bounded private key without creating or repairing state."""
        try:
            with self.directory(self.layout.data_dir, create=False) as root:
                fd = os.open("ipc.key", os.O_RDONLY | _FILE_FLAGS, dir_fd=root)
                try:
                    before = os.fstat(fd)
                    if not _private_file(before, self.layout.uid, 32):
                        raise ControlFault("runtime_credentials_invalid")
                    value = os.read(fd, 33)
                    after = os.stat("ipc.key", dir_fd=root, follow_symlinks=False)
                    if (
                        len(value) != 32
                        or not _private_file(after, self.layout.uid, 32)
                        or (_identity(before) != _identity(after))
                    ):
                        raise ControlFault("runtime_credentials_invalid")
                    return value
                finally:
                    os.close(fd)
        except (OSError, ControlFault):
            raise ControlFault("runtime_credentials_invalid") from None
